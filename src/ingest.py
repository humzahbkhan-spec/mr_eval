"""Daily ingestion: poll source feeds, store new candidates.

Pluggable by source type. Each `SourceAdapter` owns three things:

  - `discover()` — which feed targets to poll (rows from `publications`).
  - `fetch()`    — retrieve raw feed bytes (preserved verbatim before parsing).
  - `parse()`    — turn raw bytes into `CandidateRecord`s, applying its *own*
                   freshness rule.

The orchestrator is deliberately source-agnostic: for every target it saves the
raw response, dedups parsed records against the DB (`candidates.canonical_url`
is UNIQUE), inserts the new ones, tracks per-feed failures (marking a feed
inactive after N consecutive misses), and prints an end-of-run summary. Adding a
new source type (e.g. an SSRN track) is a new adapter class, not a rewrite.

Two adapters today:

  - `SubstackAdapter` — polls the frozen watchlist (`track='substack'`). Entries
    carry `pubDate`, so freshness is *time-windowed*: keep only entries
    published within the trailing `ingest_lookback_hours`.

  - `NberAdapter` — polls NBER's single working-paper feed (`track='nber'`). The
    feed is "the latest ~40 papers" and carries NO per-item dates, so freshness
    is *dedup-driven* (a paper already in `candidates` is skipped) and
    `published_at` is recorded as the first-seen date. See DECISIONS.md D-24.

Tracks never blend: a candidate/prediction/run carries its `track`, and the two
leaderboards are reported separately.

Usage:
    python -m src.ingest --track substack
    python -m src.ingest --track nber
    python -m src.ingest              # both (default)
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import httpx
import yaml
from bs4 import BeautifulSoup

from src.db import connect, init_schema, transaction
from src.normalize import canonicalize
from src.raw_store import LocalFSBackend

CONFIG_PATH = Path("config.yaml")

# NBER working-paper URLs look like https://www.nber.org/papers/w35373 . We only
# ingest those (the feed is all working papers, but this guards against the odd
# digest/announcement item and lets ground-truth matching stay exact).
import re

_NBER_PAPER_RE = re.compile(r"/papers/w\d+", re.IGNORECASE)
# NBER packs authors into the title as "Paper Title -- by A, B, and C".
_NBER_TITLE_SPLIT = " -- by "


# --- Value types ----------------------------------------------------------

@dataclass
class FeedTarget:
    """One feed to poll, bound to the publication row it belongs to."""
    publication_id: int
    name: str
    feed_url: str
    track: str


@dataclass
class CandidateRecord:
    """A parsed feed entry, ready to insert into `candidates`."""
    publication_id: int
    track: str
    url: str
    canonical_url: str
    title: Optional[str]
    subtitle: Optional[str]
    author: Optional[str]
    published_at: str          # ISO-8601
    full_text: Optional[str]
    raw_entry_json: str


# --- Small parsing helpers ------------------------------------------------

def _text(node) -> Optional[str]:
    """Return stripped text of a BeautifulSoup node, or None."""
    if node is None:
        return None
    t = node.get_text(strip=True)
    return t or None


def _parse_feed_date(raw: Optional[str]) -> Optional[datetime]:
    """Parse an RSS (RFC-822) or Atom (ISO-8601) date into an aware datetime.

    Returns None if absent or unparseable. Naive results are assumed UTC so the
    trailing-window comparison never crashes on a missing timezone.
    """
    if not raw:
        return None
    raw = raw.strip()
    dt: Optional[datetime] = None
    try:                                    # RSS: "Mon, 14 Jul 2026 12:00:00 GMT"
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        try:                                # Atom: "2026-07-14T12:00:00Z"
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --- Adapters -------------------------------------------------------------

class SubstackAdapter:
    """Polls the frozen Substack watchlist; time-windowed freshness."""

    track = "substack"

    def __init__(self, lookback_hours: float) -> None:
        self.lookback_hours = lookback_hours

    def discover(self, conn) -> list[FeedTarget]:
        rows = conn.execute(
            "SELECT id, name, feed_url FROM publications "
            "WHERE track = ? AND active = 1 ORDER BY id",
            (self.track,),
        ).fetchall()
        return [
            FeedTarget(r["id"], r["name"], r["feed_url"], self.track) for r in rows
        ]

    def fetch(self, target: FeedTarget, client: httpx.Client) -> bytes:
        resp = client.get(target.feed_url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content

    def parse(
        self, raw: bytes, target: FeedTarget, now: datetime
    ) -> list[CandidateRecord]:
        soup = BeautifulSoup(raw, "lxml-xml")
        cutoff = now - timedelta(hours=self.lookback_hours)
        records: list[CandidateRecord] = []
        for item in soup.find_all("item"):
            link = _text(item.find("link"))
            if not link:
                continue
            published = _parse_feed_date(_text(item.find("pubDate")))
            # Time-windowed: skip anything older than the lookback. Undated
            # entries are rare for Substack; keep them (better a stray dup the
            # UNIQUE index rejects than a silently dropped fresh post).
            if published is not None and published < cutoff:
                continue
            published_iso = (published or now).astimezone(timezone.utc).isoformat()
            # Substack RSS carries the author in <dc:creator>; description is a
            # short subtitle/teaser, content:encoded the full HTML body.
            author = _text(item.find("creator")) or _text(item.find("author"))
            subtitle = _text(item.find("description"))
            full_text = _text(item.find("encoded"))  # content:encoded, ns-stripped
            records.append(self._record(
                target, link, _text(item.find("title")), subtitle, author,
                published_iso, full_text, item,
            ))
        return records

    def _record(self, target, link, title, subtitle, author, published_iso,
                full_text, item) -> CandidateRecord:
        return CandidateRecord(
            publication_id=target.publication_id,
            track=self.track,
            url=link,
            canonical_url=canonicalize(link),
            title=title,
            subtitle=subtitle,
            author=author,
            published_at=published_iso,
            full_text=full_text,
            raw_entry_json=json.dumps({"raw_xml": str(item)}, ensure_ascii=False),
        )


class NberAdapter:
    """Polls NBER's single working-paper feed; dedup-driven freshness.

    NBER *is* the corpus — no watchlist to derive. The feed has no per-item
    dates, so we treat every `/papers/w#####` item as a candidate and let the
    orchestrator's dedup (canonical_url UNIQUE) decide what's new. `published_at`
    is the first-seen date until real release dates are backfilled for backtest.
    """

    track = "nber"

    def __init__(self, cfg: dict) -> None:
        self.feed_url = cfg["feed_url"]
        self.publication_name = cfg.get("publication_name", "NBER Working Papers")
        self.corpus_version = cfg.get("corpus_version", "nber-v1.0")

    def discover(self, conn) -> list[FeedTarget]:
        pub_id = self._ensure_publication(conn)
        return [FeedTarget(pub_id, self.publication_name, self.feed_url, self.track)]

    def _ensure_publication(self, conn) -> int:
        """Idempotently seed the single NBER publication row; return its id."""
        row = conn.execute(
            "SELECT id FROM publications WHERE feed_url = ?", (self.feed_url,)
        ).fetchone()
        if row:
            return row["id"]
        today = datetime.now(timezone.utc).date().isoformat()
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO publications "
                "(name, feed_url, canonical_domain, added_date, source, "
                " corpus_version, active, track) "
                "VALUES (?, ?, 'nber.org', ?, 'manual', ?, 1, ?)",
                (self.publication_name, self.feed_url, today,
                 self.corpus_version, self.track),
            )
        return cur.lastrowid

    def fetch(self, target: FeedTarget, client: httpx.Client) -> bytes:
        resp = client.get(target.feed_url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content

    def parse(
        self, raw: bytes, target: FeedTarget, now: datetime
    ) -> list[CandidateRecord]:
        soup = BeautifulSoup(raw, "lxml-xml")
        published_iso = now.astimezone(timezone.utc).isoformat()  # first-seen
        records: list[CandidateRecord] = []
        for item in soup.find_all("item"):
            link = _text(item.find("link")) or _text(item.find("guid"))
            if not link or not _NBER_PAPER_RE.search(link):
                continue
            raw_title = _text(item.find("title")) or ""
            title, author = self._split_title_authors(raw_title)
            abstract = _text(item.find("description"))
            records.append(CandidateRecord(
                publication_id=target.publication_id,
                track=self.track,
                url=link,
                canonical_url=canonicalize(link),
                title=title,
                subtitle=abstract,      # the abstract is the ranker's main signal
                author=author,
                published_at=published_iso,
                full_text=abstract,
                raw_entry_json=json.dumps({"raw_xml": str(item)}, ensure_ascii=False),
            ))
        return records

    @staticmethod
    def _split_title_authors(raw_title: str) -> tuple[str, Optional[str]]:
        """"Title -- by A, B, and C" → ("Title", "A, B, and C")."""
        if _NBER_TITLE_SPLIT in raw_title:
            title, authors = raw_title.split(_NBER_TITLE_SPLIT, 1)
            return title.strip(), authors.strip() or None
        return raw_title.strip(), None


# --- Orchestration --------------------------------------------------------

@dataclass
class TrackSummary:
    track: str
    feeds_polled: int = 0
    feed_failures: int = 0
    marked_inactive: int = 0
    new_candidates: int = 0
    duplicates: int = 0


def _insert_new(conn, records: list[CandidateRecord], now_iso: str) -> tuple[int, int]:
    """Insert records whose canonical_url isn't already present. (new, dup)."""
    new = dup = 0
    for rec in records:
        exists = conn.execute(
            "SELECT 1 FROM candidates WHERE canonical_url = ?", (rec.canonical_url,)
        ).fetchone()
        if exists:
            dup += 1
            continue
        conn.execute(
            "INSERT INTO candidates "
            "(publication_id, url, canonical_url, title, subtitle, author, "
            " published_at, ingested_at, full_text, raw_entry_json, track) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rec.publication_id, rec.url, rec.canonical_url, rec.title,
             rec.subtitle, rec.author, rec.published_at, now_iso,
             rec.full_text, rec.raw_entry_json, rec.track),
        )
        new += 1
    return new, dup


def _record_success(conn, pub_id: int) -> None:
    conn.execute(
        "UPDATE publications SET consecutive_failures = 0 WHERE id = ?", (pub_id,)
    )


def _record_failure(conn, pub_id: int, inactive_threshold: int) -> bool:
    """Bump the failure counter; deactivate at the threshold. Returns True if
    this failure tipped the feed into inactive."""
    row = conn.execute(
        "SELECT consecutive_failures FROM publications WHERE id = ?", (pub_id,)
    ).fetchone()
    fails = (row["consecutive_failures"] if row else 0) + 1
    deactivated = fails >= inactive_threshold
    conn.execute(
        "UPDATE publications SET consecutive_failures = ?, "
        "active = CASE WHEN ? THEN 0 ELSE active END WHERE id = ?",
        (fails, 1 if deactivated else 0, pub_id),
    )
    return deactivated


def ingest_track(
    conn,
    adapter,
    raw_store: LocalFSBackend,
    *,
    delay_seconds: float,
    inactive_threshold: int,
    user_agent: str,
    now: Optional[datetime] = None,
) -> TrackSummary:
    """Poll every target for one adapter; save raw, dedup, insert; return stats."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.astimezone(timezone.utc).isoformat()
    today = now.astimezone(timezone.utc).date().isoformat()
    summary = TrackSummary(track=adapter.track)

    targets = adapter.discover(conn)
    headers = {"User-Agent": user_agent}
    with httpx.Client(timeout=30.0, headers=headers) as client:
        for i, target in enumerate(targets):
            if i > 0 and delay_seconds:
                time.sleep(delay_seconds)
            summary.feeds_polled += 1
            try:
                raw = adapter.fetch(target, client)
            except Exception as exc:                     # network / HTTP error
                summary.feed_failures += 1
                with transaction(conn):
                    if _record_failure(conn, target.publication_id, inactive_threshold):
                        summary.marked_inactive += 1
                print(f"  [{adapter.track}] FAIL {target.name}: {exc}")
                continue

            # Preserve the raw response verbatim before parsing (audit trail).
            slug = _safe_slug(target)
            raw_store.put(f"{today}/feeds/{adapter.track}/{slug}.xml", raw)

            records = adapter.parse(raw, target, now)
            with transaction(conn):
                _record_success(conn, target.publication_id)
                new, dup = _insert_new(conn, records, now_iso)
            summary.new_candidates += new
            summary.duplicates += dup
    return summary


def _safe_slug(target: FeedTarget) -> str:
    """Filesystem-safe key fragment for a raw-store path (sans any .xml suffix,
    since the raw-store key adds its own)."""
    base = target.feed_url.split("//", 1)[-1]
    base = re.sub(r"\.xml$", "", base, flags=re.IGNORECASE)
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)[:120]


# --- CLI ------------------------------------------------------------------

def _load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


def _build_adapters(track: str, config: dict) -> list:
    adapters = []
    if track in ("substack", "all"):
        adapters.append(SubstackAdapter(
            lookback_hours=float(config.get("ingest_lookback_hours", 48))
        ))
    if track in ("nber", "all"):
        adapters.append(NberAdapter(config["nber"]))
    return adapters


def run(track: str = "all", config: Optional[dict] = None,
        db_path=None) -> list[TrackSummary]:
    config = config or _load_config()
    conn = connect(db_path) if db_path else connect()
    init_schema(conn)
    raw_store = LocalFSBackend()
    delay = float(config.get("archive_scraper_delay_seconds", 0.25))
    ua = config.get("user_agent", "PredictingTylerBot/0.1")
    inactive = int(config.get("inactive_after_consecutive_failures", 7))

    summaries = []
    for adapter in _build_adapters(track, config):
        print(f"[ingest] track={adapter.track} …")
        s = ingest_track(
            conn, adapter, raw_store,
            delay_seconds=delay, inactive_threshold=inactive, user_agent=ua,
        )
        summaries.append(s)
        print(f"  polled={s.feeds_polled} failures={s.feed_failures} "
              f"inactivated={s.marked_inactive} "
              f"new={s.new_candidates} dup={s.duplicates}")
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--track", choices=["substack", "nber", "all"], default="all",
        help="Which source track(s) to ingest (default: all)",
    )
    args = parser.parse_args()
    run(track=args.track)


if __name__ == "__main__":
    main()
