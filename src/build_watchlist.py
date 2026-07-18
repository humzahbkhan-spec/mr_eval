"""Derive corpus v1 Substack watchlist from the archive.

Candidates come from `archive_links` restricted to Tyler-authored posts on or
after 2022-01-01. Two kinds of candidates:

1. **Direct Substack hosts** — anything on `*.substack.com` (minus internal
   subdomains like `email.mgN.substack.com`). Verified by fetching `/feed`.

2. **Custom-domain Substacks** — non-`*.substack.com` hosts Tyler linked
   ≥ threshold times. Identified by fingerprinting `/feed` for
   `<generator>Substack</generator>` in the channel element (or a link/atom-link
   pointing at `*.substack.com`).

Manual additions in `data/manual_additions.txt` (one feed URL per line, blank
lines and `#`-comments ignored) are folded in with `source='manual'`.

Two-phase workflow: `--dry-run` reports the resulting list for editorial
review; `--commit` writes to the `publications` table and (with your OK)
bumps `corpus_version`.

Usage:
    python -m src.build_watchlist --dry-run
    python -m src.build_watchlist --commit
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx
import yaml
from bs4 import BeautifulSoup

from src.db import connect, init_schema, transaction


CONFIG_PATH = Path("config.yaml")
MANUAL_ADDITIONS_PATH = Path("data/manual_additions.txt")
PREVIEW_PATH = Path("data/watchlist_preview.json")

# The Substack corpus start-date. See D-05 / the master brief.
CORPUS_START = "2022-01"


# --- Host filters ---------------------------------------------------------

def is_direct_substack_host(host: str) -> bool:
    """foo.substack.com — a publication, not an internal service."""
    if not host.endswith(".substack.com"):
        return False
    return not is_internal_substack_host(host)


_INTERNAL_EMAIL_TRACKER = re.compile(r"^email\.mg\d+\.substack\.com$")


def is_internal_substack_host(host: str) -> bool:
    """Substack-owned subdomains that aren't publications.

    - `substack.com`, `www.substack.com`: the root marketing site.
    - `open.substack.com`: universal share URLs (canonicalizer already rewrites
      these to the publication host, but defensive here anyway).
    - `on.substack.com`: another share/redirect subdomain.
    - `email.mgN.substack.com`: Mailgun-style email tracking redirect used in
      Substack's transactional email.
    """
    if host in ("substack.com", "www.substack.com",
                "open.substack.com", "on.substack.com"):
        return True
    return bool(_INTERNAL_EMAIL_TRACKER.match(host))


# --- Data classes ---------------------------------------------------------

@dataclass
class HostCandidate:
    host: str
    link_count: int


@dataclass
class PublicationCandidate:
    name: str
    feed_url: str
    canonical_domain: str  # host we canonicalize to for URL matching
    source: str            # 'archive_derived' | 'manual'
    link_count: Optional[int] = None
    substack_subdomain: Optional[str] = None  # for custom-domain sites, the .substack.com equivalent


# --- Candidate enumeration -----------------------------------------------

def enumerate_candidates(
    conn,
    corpus_start: str,
    custom_domain_min_links: int,
) -> tuple[list[HostCandidate], list[HostCandidate]]:
    """Return (direct_substack_candidates, custom_domain_candidates).

    `direct_substack_candidates` — every non-internal `*.substack.com` host
    Tyler linked at least once since `corpus_start`.

    `custom_domain_candidates` — every non-substack host Tyler linked
    ≥ `custom_domain_min_links` times since `corpus_start`. Most of these
    won't be Substacks; we fingerprint each one downstream.
    """
    rows = conn.execute("""
        SELECT al.canonical_href
        FROM archive_links al
        JOIN archive_posts ap ON al.archive_post_id = ap.id
        WHERE ap.month >= ?
          AND ap.author LIKE '%Tyler Cowen%'
    """, (corpus_start,)).fetchall()

    hosts = Counter()
    for r in rows:
        h = urlsplit(r["canonical_href"]).hostname
        if h:
            hosts[h] += 1

    direct: list[HostCandidate] = []
    custom: list[HostCandidate] = []
    for host, count in hosts.items():
        if is_internal_substack_host(host):
            continue
        if is_direct_substack_host(host):
            direct.append(HostCandidate(host=host, link_count=count))
        elif count >= custom_domain_min_links:
            custom.append(HostCandidate(host=host, link_count=count))

    direct.sort(key=lambda c: -c.link_count)
    custom.sort(key=lambda c: -c.link_count)
    return direct, custom


# --- Feed fetch + fingerprint --------------------------------------------

def fetch_feed(url: str, client: httpx.Client, delay: float) -> Optional[str]:
    """GET a feed URL. Return body text on 2xx, None on 4xx/error.

    Substack sometimes 403s bots without a friendly UA — the caller should
    already have set a `User-Agent` header on the client.
    """
    try:
        resp = client.get(url, follow_redirects=True)
    except httpx.HTTPError:
        time.sleep(delay)
        return None
    time.sleep(delay)
    if 200 <= resp.status_code < 300:
        return resp.text
    return None


_SUBSTACK_GENERATOR = re.compile(r"substack", re.IGNORECASE)


def parse_substack_feed(body: str) -> Optional[tuple[str, Optional[str]]]:
    """If `body` is a Substack RSS feed, return (channel_title, substack_subdomain_or_None).

    A Substack feed is identified by:
      - `<generator>` element in the channel containing "Substack", OR
      - an `<atom:link rel="self">` or `<link>` pointing at `*.substack.com`.

    `substack_subdomain` is the underlying `.substack.com` host if we can find
    it — matters for custom-domain publications so we can populate the alias
    map used by URL canonicalization later.
    """
    if not body or "<rss" not in body[:1024].lower() and "<feed" not in body[:1024].lower():
        return None

    soup = BeautifulSoup(body, "lxml-xml")
    channel = soup.find("channel") or soup.find("feed")
    if channel is None:
        return None

    is_substack = False

    gen = channel.find("generator")
    if gen and _SUBSTACK_GENERATOR.search(gen.get_text() or ""):
        is_substack = True

    substack_sub: Optional[str] = None
    for link in channel.find_all(["link", "atom:link"]):
        href = link.get("href") or link.get_text(strip=True) or ""
        host = urlsplit(href).hostname or ""
        if host.endswith(".substack.com") and not is_internal_substack_host(host):
            substack_sub = host
            is_substack = True
            break

    if not is_substack:
        return None

    title_el = channel.find("title")
    title = title_el.get_text(strip=True) if title_el else ""
    return title or "(untitled)", substack_sub


# --- Verification passes -------------------------------------------------

def verify_direct_substack(
    host: str, link_count: int, client: httpx.Client, delay: float,
) -> Optional[PublicationCandidate]:
    body = fetch_feed(f"https://{host}/feed", client, delay)
    if body is None:
        return None
    parsed = parse_substack_feed(body)
    if parsed is None:
        return None
    name, _ = parsed
    return PublicationCandidate(
        name=name,
        feed_url=f"https://{host}/feed",
        canonical_domain=host,
        source="archive_derived",
        link_count=link_count,
    )


def verify_custom_domain(
    host: str, link_count: int, client: httpx.Client, delay: float,
) -> Optional[PublicationCandidate]:
    body = fetch_feed(f"https://{host}/feed", client, delay)
    if body is None:
        return None
    parsed = parse_substack_feed(body)
    if parsed is None:
        return None
    name, substack_sub = parsed
    return PublicationCandidate(
        name=name,
        feed_url=f"https://{host}/feed",
        canonical_domain=host,
        source="archive_derived",
        link_count=link_count,
        substack_subdomain=substack_sub,
    )


# --- Manual additions ----------------------------------------------------

def read_manual_additions() -> list[PublicationCandidate]:
    """Read one URL per line from data/manual_additions.txt (create if missing)."""
    if not MANUAL_ADDITIONS_PATH.exists():
        MANUAL_ADDITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANUAL_ADDITIONS_PATH.write_text(
            "# One Substack feed URL per line. Blank lines and #-comments ignored.\n"
            "# Example: https://noahpinion.substack.com/feed\n"
        )
        return []

    additions: list[PublicationCandidate] = []
    for line in MANUAL_ADDITIONS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Accept either a feed URL or a bare host
        if not line.startswith("http"):
            line = f"https://{line}"
        if not line.endswith("/feed"):
            line = line.rstrip("/") + "/feed"
        host = urlsplit(line).hostname or ""
        if not host:
            continue
        additions.append(PublicationCandidate(
            name=host,  # will be replaced with real title on verification below
            feed_url=line,
            canonical_domain=host,
            source="manual",
        ))
    return additions


# --- DB write ------------------------------------------------------------

def insert_publications(
    conn, publications: list[PublicationCandidate], corpus_version: str,
) -> tuple[int, int]:
    """Insert or ignore rows in `publications`. Returns (n_inserted, n_skipped_dup)."""
    today = date.today().isoformat()
    n_ins = 0
    n_dup = 0
    with transaction(conn):
        for pub in publications:
            row = conn.execute(
                "SELECT id FROM publications WHERE feed_url = ?", (pub.feed_url,)
            ).fetchone()
            if row:
                n_dup += 1
                continue
            conn.execute(
                "INSERT INTO publications "
                "(name, feed_url, canonical_domain, added_date, source, corpus_version, active) "
                "VALUES (?, ?, ?, ?, ?, ?, 1)",
                (pub.name, pub.feed_url, pub.canonical_domain, today, pub.source, corpus_version),
            )
            n_ins += 1
    return n_ins, n_dup


# --- Orchestration -------------------------------------------------------

def build_watchlist(
    dry_run: bool = True,
    corpus_start: str = CORPUS_START,
    custom_domain_min_links: int = 2,
) -> list[PublicationCandidate]:
    conn = connect()
    init_schema(conn)

    # Commit-from-cache path: if the user already ran --dry-run and reviewed
    # the resulting preview file, --commit can skip the network entirely.
    if not dry_run:
        cached = load_preview()
        if cached is not None:
            print(f"[commit] using cached preview from {PREVIEW_PATH} ({len(cached)} publications)")
            n_ins, n_dup = insert_publications(conn, cached, corpus_version="v1.0")
            print(f"[commit] inserted {n_ins}, skipped {n_dup} already-present rows")
            conn.close()
            return cached

    config = _load_config()
    delay = float(config.get("archive_scraper_delay_seconds", 0.25))
    user_agent = config.get("user_agent", "PredictingTylerBot/0.1")

    direct, custom = enumerate_candidates(
        conn, corpus_start=corpus_start,
        custom_domain_min_links=custom_domain_min_links,
    )
    print(f"[enumerate] {len(direct)} direct Substack hosts; "
          f"{len(custom)} custom-domain candidates (≥{custom_domain_min_links} links)")

    verified: list[PublicationCandidate] = []
    dead: list[str] = []
    non_substack: list[str] = []

    with httpx.Client(
        headers={"User-Agent": user_agent, "Accept": "application/rss+xml, application/xml, text/xml"},
        timeout=20.0,
    ) as client:

        print(f"[verify] direct Substack feeds ({len(direct)})…")
        for i, c in enumerate(direct, 1):
            pub = verify_direct_substack(c.host, c.link_count, client, delay)
            if pub is None:
                dead.append(c.host)
            else:
                verified.append(pub)
            if i % 50 == 0:
                print(f"  {i}/{len(direct)} verified={len(verified)} dead={len(dead)}")

        print(f"[verify] custom-domain candidates ({len(custom)})…")
        for i, c in enumerate(custom, 1):
            pub = verify_custom_domain(c.host, c.link_count, client, delay)
            if pub is None:
                non_substack.append(c.host)
            else:
                verified.append(pub)
            if i % 100 == 0:
                print(f"  {i}/{len(custom)} verified={len(verified)} not-substack={len(non_substack)}")

        # Manual additions
        manual = read_manual_additions()
        for pub in manual:
            body = fetch_feed(pub.feed_url, client, delay)
            if body is None:
                dead.append(pub.canonical_domain + " (manual)")
                continue
            parsed = parse_substack_feed(body)
            if parsed is not None:
                pub.name, pub.substack_subdomain = parsed
            verified.append(pub)

    _print_detailed_summary(verified, dead, non_substack)

    if dry_run:
        _save_preview(verified, dead)
        print(f"\n(dry-run — nothing written to DB. Preview saved to {PREVIEW_PATH}.)")
        print("Re-run with --commit to insert (will read preview if present).")
    else:
        n_ins, n_dup = insert_publications(conn, verified, corpus_version="v1.0")
        print(f"\n[commit] inserted {n_ins}, skipped {n_dup} already-present rows")

    conn.close()
    return verified


def _print_detailed_summary(
    verified: list[PublicationCandidate],
    dead: list[str],
    non_substack: list[str],
) -> None:
    direct = [p for p in verified if p.canonical_domain.endswith('.substack.com') and p.source == 'archive_derived']
    custom = [p for p in verified if not p.canonical_domain.endswith('.substack.com') and p.source == 'archive_derived']
    manual = [p for p in verified if p.source == 'manual']

    print()
    print("=== Summary ===")
    print(f"  Total publications derived: {len(verified)}")
    print(f"    from *.substack.com hosts: {len(direct)}")
    print(f"    from custom domains:       {len(custom)}")
    print(f"    from manual_additions.txt: {len(manual)}")
    print(f"  Dead / unreachable direct Substack feeds: {len(dead)}")
    print(f"  Custom-domain candidates that weren't Substacks: {len(non_substack)}")
    if len(verified) < 100:
        print(f"\n⚠️  Only {len(verified)} publications — below brief's floor of 100. Review before committing.")
    elif len(verified) > 600:
        print(f"\n⚠️  {len(verified)} publications — above brief's ceiling of 600. Review before committing.")
    else:
        print(f"\n✓ Watchlist size {len(verified)} is within the brief's expected range (100–600).")

    # Top 40 direct Substacks by Tyler-link count — a scan sanity-checks the roster.
    direct_ranked = sorted(direct, key=lambda p: -(p.link_count or 0))
    print(f"\n=== Top 40 direct Substacks by Tyler-link count ===")
    for p in direct_ranked[:40]:
        print(f"  {p.link_count:4d}  {p.canonical_domain:<45} {p.name}")

    # Every custom-domain Substack — the interesting new signal.
    print(f"\n=== All {len(custom)} custom-domain Substacks discovered ===")
    for p in sorted(custom, key=lambda p: -(p.link_count or 0)):
        sub = f"  (→ {p.substack_subdomain})" if p.substack_subdomain else ""
        print(f"  {p.link_count:4d}  {p.canonical_domain:<45} {p.name}{sub}")

    # Dead feeds — Substack subdomains Tyler linked but that don't resolve now.
    if dead:
        print(f"\n=== Dead / unreachable Substack feeds ({len(dead)}) ===")
        for h in dead:
            print(f"  - {h}")


def _save_preview(verified: list[PublicationCandidate], dead: list[str]) -> None:
    """Persist the dry-run result so `--commit` doesn't have to re-fetch."""
    PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREVIEW_PATH.write_text(json.dumps({
        "generated_at": date.today().isoformat(),
        "publications": [asdict(p) for p in verified],
        "dead_hosts": dead,
    }, indent=2, sort_keys=True))


def load_preview() -> Optional[list[PublicationCandidate]]:
    """Return the last dry-run's verified list, or None if no preview exists."""
    if not PREVIEW_PATH.exists():
        return None
    payload = json.loads(PREVIEW_PATH.read_text())
    return [PublicationCandidate(**p) for p in payload["publications"]]


def _load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


# --- CLI -----------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Report the derived list but don't write to DB (default)")
    parser.add_argument("--commit", action="store_true",
                        help="Actually insert into the publications table")
    parser.add_argument("--corpus-start", default=CORPUS_START,
                        help=f"Only include publications Tyler linked on/after this month (default: {CORPUS_START})")
    parser.add_argument("--custom-domain-min-links", type=int, default=2,
                        help="Minimum link count to probe a non-Substack host for custom-domain Substack fingerprint (default: 2)")
    args = parser.parse_args()

    build_watchlist(
        dry_run=not args.commit,
        corpus_start=args.corpus_start,
        custom_domain_min_links=args.custom_domain_min_links,
    )


if __name__ == "__main__":
    main()
