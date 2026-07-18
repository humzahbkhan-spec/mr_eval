"""Ground-truth harvesting, matching, and scoring metrics.

Ground truth = the links Tyler actually posts on Marginal Revolution. Per D-19
this is EVERY Tyler-authored post (co-authored with Alex counts), not just the
numbered "assorted links" roundups — his dedicated single-post writeups ("Robin
Hanson on X") are the same taste signal (Humzah, 2026-07-18: score all Tyler
posts as one pool). Two entry points populate the `ground_truth` table:

  - `harvest_from_archive()` — backtest: reuse the already-extracted
    `archive_links` for historical Tyler posts (no network).
  - `harvest_from_rss()`     — live: parse `marginalrevolution.com/feed`,
    keeping items whose `dc:creator` is Tyler.

Each harvested link is classified into a `track` (`substack` / `nber` /
`other`) and given a `match_type`. Matching against candidates
(`exact` / `same_publication` / `unmatched`) is a *separate* pass
(`match_unscored`) run once candidates and predictions exist — so harvesting
works before the ranker does. Provisional (candidate-free) classifications set
at harvest time:

  - `other`  links               → `out_of_corpus` (kept for the link-share
                                    statistic, never scored).
  - `nber`   released > window    → `out_of_scope` (classic-paper resurfacing,
    before the link                 D-27; counted, excluded from recall).
  - `substack` / fresh `nber`     → `unmatched` (until a candidate is matched).

Metrics (`recall_at_k`, `mrr`) join scored ground truth to predictions, per
model × track, and NEVER blend `live` and `backtest` (filter on `runs.kind`).
The model's effective rank for a pick is its best (lowest) rank across the
trailing matching window (`cross_day_rank_aggregation: best`, D-01).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from src.archive_scraper import is_tyler_authored
from src.normalize import canonicalize

_NBER_PAPER_RE = re.compile(r"/papers/(w\d+)", re.IGNORECASE)


# --- Track classification -------------------------------------------------

def load_substack_domains(conn) -> set[str]:
    """Canonical domains of every Substack-track publication (folds custom
    domains, e.g. `slowboring.com`, so they classify as Substack)."""
    return {
        r[0] for r in conn.execute(
            "SELECT canonical_domain FROM publications WHERE track = 'substack'"
        ).fetchall()
    }


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def classify_track(canonical_url: str, substack_domains: set[str]) -> str:
    """Return the eval track a link belongs to: substack / nber / other."""
    host = _host(canonical_url)
    if host.endswith(".substack.com") or host == "substack.com" or host in substack_domains:
        return "substack"
    if host.endswith("nber.org") and _NBER_PAPER_RE.search(canonical_url):
        return "nber"
    return "other"


def nber_paper_id(canonical_url: str) -> str | None:
    m = _NBER_PAPER_RE.search(canonical_url)
    return m.group(1).lower() if m else None


def _days_between(later: str, earlier: str) -> int:
    return (date.fromisoformat(later[:10]) - date.fromisoformat(earlier[:10])).days


def _provisional_match_type(
    conn, track: str, canonical_url: str, mr_post_date: str, nber_window: int
) -> str:
    """Classification that needs no candidate: everything else stays 'unmatched'
    until `match_unscored` finds (or fails to find) a candidate."""
    if track == "other":
        return "out_of_corpus"
    if track == "nber":
        pid = nber_paper_id(canonical_url)
        row = conn.execute(
            "SELECT release_date FROM nber_paper_dates WHERE paper_id = ?", (pid,)
        ).fetchone() if pid else None
        if row and _days_between(mr_post_date, row[0]) > nber_window:
            return "out_of_scope"
    return "unmatched"


# --- Harvest value types --------------------------------------------------

@dataclass
class HarvestStats:
    posts: int = 0
    links: int = 0
    by_track: dict = field(default_factory=dict)
    by_match_type: dict = field(default_factory=dict)

    def _bump(self, track: str, match_type: str) -> None:
        self.links += 1
        self.by_track[track] = self.by_track.get(track, 0) + 1
        self.by_match_type[match_type] = self.by_match_type.get(match_type, 0) + 1


@dataclass
class HarvestedPost:
    url: str
    post_date: str                     # ISO date
    links: list[tuple[int, str]]       # (position, raw_href)


# --- Harvesting -----------------------------------------------------------

def _insert_gt(conn, stats, post_url, post_date, position, raw_url,
               substack_domains, nber_window, now_iso) -> None:
    canonical = canonicalize(raw_url)
    track = classify_track(canonical, substack_domains)
    match_type = _provisional_match_type(conn, track, canonical, post_date, nber_window)
    cur = conn.execute(
        "INSERT OR IGNORE INTO ground_truth "
        "(mr_post_url, mr_post_date, link_position, raw_url, canonical_url, "
        " is_substack, track, matched_candidate_id, match_type, match_lag_days, "
        " scored_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?)",
        (post_url, post_date, position, raw_url, canonical,
         1 if track == "substack" else 0, track, match_type, now_iso),
    )
    if cur.rowcount:                    # not an INSERT-OR-IGNORE dup
        stats._bump(track, match_type)


def harvest_from_archive(conn, start_month: str = "2022-01",
                         dry_run: bool = False, now=None) -> HarvestStats:
    """Populate ground_truth from historical Tyler-authored posts already in
    `archive_posts`/`archive_links` (all posts, not just assorted-links; D-19).
    Idempotent."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    substack_domains = load_substack_domains(conn)
    nber_window = _nber_window(conn)
    stats = HarvestStats()

    posts = conn.execute(
        "SELECT id, url, published_at FROM archive_posts "
        "WHERE author LIKE '%Tyler%' AND month >= ? "
        "ORDER BY published_at",
        (start_month,),
    ).fetchall()
    for post in posts:
        post_date = (post["published_at"] or "")[:10]
        if not post_date:
            continue
        links = conn.execute(
            "SELECT position, href FROM archive_links WHERE archive_post_id = ? "
            "ORDER BY position",
            (post["id"],),
        ).fetchall()
        if not links:
            continue
        stats.posts += 1
        for lk in links:
            _insert_gt(conn, stats, post["url"], post_date, lk["position"],
                       lk["href"], substack_domains, nber_window, now_iso)
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return stats


def parse_tyler_posts_rss(xml: bytes | str) -> list[HarvestedPost]:
    """Parse MR's RSS; return every Tyler-authored post (by `dc:creator`) with
    its ordered outbound links. Skips anchor/mailto/javascript and MR-internal
    links."""
    soup = BeautifulSoup(xml, "lxml-xml")
    out: list[HarvestedPost] = []
    for item in soup.find_all("item"):
        creator = item.find("creator")
        author = creator.get_text(strip=True) if creator else None
        if not is_tyler_authored(author):
            continue
        link_el = item.find("link")
        post_url = link_el.get_text(strip=True) if link_el else ""
        pub = item.find("pubDate")
        post_date = _rss_date(pub.get_text(strip=True) if pub else "")
        content = item.find("encoded")     # content:encoded, ns-stripped
        body_html = content.get_text() if content else ""
        links = _extract_body_links(body_html, post_url)
        out.append(HarvestedPost(post_url, post_date, links))
    return out


def _extract_body_links(body_html: str, base_url: str) -> list[tuple[int, str]]:
    """Ordered outbound links from a post-body HTML fragment (RSS content)."""
    soup = BeautifulSoup(body_html, "lxml")
    links: list[tuple[int, str]] = []
    pos = 0
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        if "marginalrevolution.com" in _host(href):   # skip MR-internal
            continue
        pos += 1
        links.append((pos, href))
    return links


def harvest_from_rss(conn, xml: bytes | str, dry_run: bool = False,
                     now=None) -> HarvestStats:
    """Live harvest: parse MR RSS and upsert assorted-links ground truth."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    substack_domains = load_substack_domains(conn)
    nber_window = _nber_window(conn)
    stats = HarvestStats()
    for post in parse_tyler_posts_rss(xml):
        if not post.post_date or not post.links:
            continue
        stats.posts += 1
        for position, raw_url in post.links:
            _insert_gt(conn, stats, post.url, post.post_date, position, raw_url,
                       substack_domains, nber_window, now_iso)
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return stats


def _rss_date(raw: str) -> str:
    """RFC-822 pubDate → ISO date (YYYY-MM-DD); '' if unparseable."""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError):
        return ""


# --- Matching (needs candidates) ------------------------------------------

@dataclass
class MatchStats:
    considered: int = 0
    exact: int = 0
    same_publication: int = 0
    still_unmatched: int = 0


def match_unscored(conn, substack_window: int | None = None,
                   nber_window: int | None = None) -> MatchStats:
    """Upgrade 'unmatched' ground-truth rows to 'exact'/'same_publication' when a
    candidate in the same track was in the pool within the trailing window.

    A candidate counts only if ingested on/before the link date and within the
    window (a post first ingested *after* Tyler linked it is not an opportunity).
    """
    substack_window = substack_window if substack_window is not None else _substack_window(conn)
    nber_window = nber_window if nber_window is not None else _nber_window(conn)
    stats = MatchStats()

    rows = conn.execute(
        "SELECT id, canonical_url, track, mr_post_date FROM ground_truth "
        "WHERE match_type = 'unmatched'"
    ).fetchall()
    for r in rows:
        stats.considered += 1
        window = nber_window if r["track"] == "nber" else substack_window
        lo = (date.fromisoformat(r["mr_post_date"][:10]) - timedelta(days=window)).isoformat()
        hi = r["mr_post_date"][:10]
        # exact: same canonical URL, same track, in the pool within window.
        cand = conn.execute(
            "SELECT id, published_at FROM candidates "
            "WHERE canonical_url = ? AND track = ? "
            "AND substr(ingested_at,1,10) BETWEEN ? AND ? "
            "ORDER BY ingested_at LIMIT 1",
            (r["canonical_url"], r["track"], lo, hi),
        ).fetchone()
        if cand:
            lag = _days_between(hi, cand["published_at"])
            conn.execute(
                "UPDATE ground_truth SET match_type='exact', matched_candidate_id=?, "
                "match_lag_days=? WHERE id=?",
                (cand["id"], lag, r["id"]),
            )
            stats.exact += 1
            continue
        # same_publication (Substack only): a different post from the same feed.
        if r["track"] == "substack":
            pub = conn.execute(
                "SELECT id FROM publications WHERE canonical_domain = ?",
                (_host(r["canonical_url"]),),
            ).fetchone()
            if pub:
                other = conn.execute(
                    "SELECT id FROM candidates WHERE publication_id = ? "
                    "AND substr(ingested_at,1,10) BETWEEN ? AND ? LIMIT 1",
                    (pub["id"], lo, hi),
                ).fetchone()
                if other:
                    conn.execute(
                        "UPDATE ground_truth SET match_type='same_publication' WHERE id=?",
                        (r["id"],),
                    )
                    stats.same_publication += 1
                    continue
        stats.still_unmatched += 1
    conn.commit()
    return stats


# --- Metrics --------------------------------------------------------------

def _opportunities(conn, track: str, kind: str):
    """Ground-truth rows that are genuine scoring opportunities: we had the
    candidate (exact/content_match) and it was ranked in a run of this kind."""
    return conn.execute(
        "SELECT DISTINCT g.id, g.matched_candidate_id, g.mr_post_date "
        "FROM ground_truth g "
        "WHERE g.track = ? AND g.match_type IN ('exact','content_match') "
        "AND g.matched_candidate_id IS NOT NULL",
        (track,),
    ).fetchall()


def _best_rank(conn, candidate_id: int, model: str, kind: str,
               mr_post_date: str, window: int) -> int | None:
    """Best (lowest) rank the model gave this candidate across the trailing
    window, within runs of `kind`. None if never ranked (D-01 aggregation)."""
    lo = (date.fromisoformat(mr_post_date[:10]) - timedelta(days=window)).isoformat()
    hi = mr_post_date[:10]
    row = conn.execute(
        "SELECT MIN(p.rank) FROM predictions p JOIN runs r ON r.run_id = p.run_id "
        "WHERE p.candidate_id = ? AND p.model = ? AND r.kind = ? "
        "AND substr(p.run_date,1,10) BETWEEN ? AND ?",
        (candidate_id, model, kind, lo, hi),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def recall_at_k(conn, model: str, track: str, k: int, kind: str = "live") -> tuple[int, int]:
    """Return (hits, opportunities): opportunities where the model ranked the
    picked candidate in its top-k. Rate = hits / opportunities (guard /0)."""
    window = _nber_window(conn) if track == "nber" else _substack_window(conn)
    hits = opps = 0
    for o in _opportunities(conn, track, kind):
        opps += 1
        rank = _best_rank(conn, o["matched_candidate_id"], model, kind,
                          o["mr_post_date"], window)
        if rank is not None and rank <= k:
            hits += 1
    return hits, opps


def mrr(conn, model: str, track: str, kind: str = "live") -> float:
    """Mean reciprocal rank over opportunities (0 contribution if unranked)."""
    window = _nber_window(conn) if track == "nber" else _substack_window(conn)
    total = 0.0
    opps = 0
    for o in _opportunities(conn, track, kind):
        opps += 1
        rank = _best_rank(conn, o["matched_candidate_id"], model, kind,
                          o["mr_post_date"], window)
        if rank is not None:
            total += 1.0 / rank
    return total / opps if opps else 0.0


# --- Config helpers -------------------------------------------------------

def _config() -> dict:
    import yaml
    from pathlib import Path
    with Path("config.yaml").open() as f:
        return yaml.safe_load(f)


def _substack_window(conn) -> int:
    return int(_config().get("matching_window_days", 4))


def _nber_window(conn) -> int:
    return int(_config().get("nber", {}).get("matching_window_days", 14))
