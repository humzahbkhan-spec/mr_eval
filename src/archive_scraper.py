"""Harvest historical Marginal Revolution 'assorted links' posts from monthly archives.

MR is WordPress. There is no full RSS history, so we walk the monthly archive
pages (`marginalrevolution.com/marginalrevolution/YYYY/MM`) and filter posts
whose titles match `/assorted links/i`. Expect ~4,500+ posts, 2003–present.

Politeness: every request sleeps `archive_scraper_delay_seconds` (config.yaml)
before returning. HTTP 429/503 responses trigger exponential backoff. The
User-Agent identifies the project and contact address from config.

Storage:
  - Raw HTML → `data/raw/mr_archive/YYYY/MM/…` via `raw_store.LocalFSBackend`
    (gitignored — bulky, regeneratable from re-scrape).
  - Extracted rows → `archive_posts` and `archive_links` tables in `data/tyler.db`
    (committed — the single source of truth downstream jobs read from).
  - Progress → `data/archive_checkpoint.json` (committed, small — lets a fresh
    clone resume without re-fetching completed months).

Usage:
    python -m src.archive_scraper                       # resume from checkpoint
    python -m src.archive_scraper --start 2003-08 --end 2003-12
    python -m src.archive_scraper --month 2024-01       # one specific month
    python -m src.archive_scraper --dry-run --month 2024-01
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import httpx
import yaml

from src.archive_extract import (
    extract_archive_page,
    extract_post,
    is_assorted_links,
)
from src.db import connect, init_schema, transaction
from src.normalize import canonicalize
from src.raw_store import LocalFSBackend, RawStoreBackend


MR_MONTHLY_URL = "https://marginalrevolution.com/marginalrevolution/{year:04d}/{month:02d}/"
MR_FIRST_MONTH = "2003-08"      # MR launched August 2003

CONFIG_PATH = Path("config.yaml")
CHECKPOINT_PATH = Path("data/archive_checkpoint.json")


def is_tyler_authored(author: Optional[str]) -> bool:
    """True if the byline includes Tyler (solo or co-authored with Alex)."""
    return bool(author) and "tyler cowen" in author.lower()


def _has_substack_link(links) -> bool:
    """True if any of the post's outbound links resolves to *.substack.com."""
    return any(
        ".substack.com/" in canonicalize(ln.href) for ln in links
    )


@dataclass
class ScrapeStats:
    month: str
    archive_pages_fetched: int = 0
    assorted_links_posts_fetched: int = 0
    other_tyler_posts_fetched: int = 0     # only used with --include-all-posts
    posts_skipped_non_tyler: int = 0       # only used with --include-all-posts
    posts_skipped_already_present: int = 0
    errors: list[str] = field(default_factory=list)


# --- Config / checkpoint ---------------------------------------------------

def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


def load_checkpoint() -> dict:
    if not CHECKPOINT_PATH.exists():
        return {"completed_months": []}
    with CHECKPOINT_PATH.open() as f:
        return json.load(f)


def save_checkpoint(state: dict) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CHECKPOINT_PATH.open("w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# --- Month iteration -------------------------------------------------------

def iter_months(start_ym: str, end_ym: str) -> Iterator[tuple[int, int]]:
    """Yield (year, month) inclusive, in ascending order. Args are 'YYYY-MM'."""
    y, m = _parse_ym(start_ym)
    end_y, end_m = _parse_ym(end_ym)
    while (y, m) <= (end_y, end_m):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def _parse_ym(s: str) -> tuple[int, int]:
    y, m = s.split("-")
    return int(y), int(m)


def current_month_ym() -> str:
    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"


# --- HTTP fetching ---------------------------------------------------------

def fetch(url: str, client: httpx.Client, delay: float) -> Optional[str]:
    """Polite GET with 429/503 backoff. Returns text on 2xx, None on 404.

    Sleeps `delay` seconds *after* each request so caller-loop pacing is uniform.
    On 429/503, retries up to 3 times with exponential backoff (2s → 4s → 8s)
    before raising.
    """
    backoff = 2.0
    for attempt in range(4):
        resp = client.get(url, follow_redirects=True)
        if resp.status_code == 404:
            time.sleep(delay)
            return None
        if resp.status_code in (429, 503):
            if attempt == 3:
                resp.raise_for_status()
            time.sleep(backoff)
            backoff *= 2
            continue
        resp.raise_for_status()
        time.sleep(delay)
        return resp.text
    return None  # unreachable


# --- Per-month scrape ------------------------------------------------------

def slug_from_post_url(url: str) -> str:
    """Filesystem-safe identifier for a post URL — last non-empty path segment."""
    parts = [p for p in url.rstrip("/").split("/") if p]
    return parts[-1] if parts else "unknown"


def scrape_month(
    year: int,
    month: int,
    client: httpx.Client,
    raw: RawStoreBackend,
    conn,
    delay: float,
    dry_run: bool = False,
    include_all_posts: bool = False,
) -> ScrapeStats:
    """Fetch and extract all 'assorted links' posts from one calendar month.

    Idempotent: posts already present in `archive_posts` are skipped. Pagination
    is handled by URL enumeration (`.../YYYY/MM/`, `.../YYYY/MM/page/2/`, ...)
    rather than by parsing pagination markup — MR's custom pagination widget
    doesn't emit the standard WordPress `<link rel="next">` and the URL scheme
    is far more stable than any theme's markup.
    """
    ym = f"{year:04d}-{month:02d}"
    stats = ScrapeStats(month=ym)
    base = MR_MONTHLY_URL.format(year=year, month=month)
    page_num = 1

    while True:
        page_url = base if page_num == 1 else f"{base}page/{page_num}/"
        try:
            html = fetch(page_url, client, delay)
        except httpx.HTTPError as e:
            stats.errors.append(f"archive page {page_url}: {type(e).__name__}: {e}")
            break
        if html is None:
            break  # 404 — past the last page

        if not dry_run:
            raw.put(
                f"mr_archive/{year:04d}/{month:02d}/_index_page_{page_num}.html",
                html.encode("utf-8"),
            )
        stats.archive_pages_fetched += 1

        posts, _ = extract_archive_page(html, page_url)
        if not posts:
            # 200 but no posts found — defensive: stop rather than loop forever
            # if MR ever returns a soft-empty page for out-of-range pagination.
            break

        for post in posts:
            is_al = is_assorted_links(post.title)
            if not include_all_posts and not is_al:
                # Legacy mode: only fetch assorted-links posts.
                continue
            _fetch_and_store_post(
                post_url=post.url,
                year=year,
                month=month,
                client=client,
                raw=raw,
                conn=conn,
                delay=delay,
                dry_run=dry_run,
                stats=stats,
                is_assorted=is_al,
            )
        page_num += 1

    return stats


def _fetch_and_store_post(
    post_url: str,
    year: int,
    month: int,
    client: httpx.Client,
    raw: RawStoreBackend,
    conn,
    delay: float,
    dry_run: bool,
    stats: ScrapeStats,
    is_assorted: bool,
) -> None:
    if _post_already_stored(conn, post_url):
        stats.posts_skipped_already_present += 1
        return

    try:
        html = fetch(post_url, client, delay)
    except httpx.HTTPError as e:
        stats.errors.append(f"post {post_url}: {type(e).__name__}: {e}")
        return
    if html is None:
        stats.errors.append(f"post {post_url}: 404")
        return

    extracted = extract_post(html, post_url)

    # Tyler filter — includes co-authored posts (Tyler & Alex).
    if not is_tyler_authored(extracted.author):
        stats.posts_skipped_non_tyler += 1
        return

    slug = slug_from_post_url(post_url)
    if not dry_run:
        raw.put(
            f"mr_archive/{year:04d}/{month:02d}/{slug}.html",
            html.encode("utf-8"),
        )

    if is_assorted:
        stats.assorted_links_posts_fetched += 1
    else:
        stats.other_tyler_posts_fetched += 1

    if not dry_run:
        _insert_post(conn, extracted, month_ym=f"{year:04d}-{month:02d}")


def _post_already_stored(conn, post_url: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM archive_posts WHERE url = ?", (post_url,)
    ).fetchone()
    return row is not None


def _insert_post(conn, post, month_ym: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    has_ss = 1 if _has_substack_link(post.links) else 0
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO archive_posts "
            "(url, title, published_at, month, scraped_at, author, has_substack_link) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (post.url, post.title, post.published_at, month_ym, now,
             post.author, has_ss),
        )
        post_id = cur.lastrowid
        for link in post.links:
            conn.execute(
                "INSERT INTO archive_links "
                "(archive_post_id, position, href, canonical_href, anchor_text, surrounding_text) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    post_id,
                    link.position,
                    link.href,
                    canonicalize(link.href),
                    link.anchor_text,
                    link.surrounding_text,
                ),
            )


# --- Range driver ----------------------------------------------------------

def scrape_range(
    start_ym: str,
    end_ym: str,
    dry_run: bool = False,
    ignore_checkpoint: bool = False,
    include_all_posts: bool = False,
) -> list[ScrapeStats]:
    config = load_config()
    delay = float(config.get("archive_scraper_delay_seconds", 0.5))
    user_agent = config.get("user_agent", "PredictingTylerBot/0.1")

    # Always load prior progress; `--ignore-checkpoint` only bypasses the
    # per-month skip check below. Discarding the historical set on save would
    # silently erase records of earlier runs — resumability should survive
    # a `--ignore-checkpoint` re-run without amnesia.
    checkpoint = load_checkpoint()
    completed = set(checkpoint.get("completed_months", []))

    raw = LocalFSBackend(root="data/raw")
    conn = connect()
    init_schema(conn)

    all_stats: list[ScrapeStats] = []
    try:
        with httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=30.0,
        ) as client:
            for y, m in iter_months(start_ym, end_ym):
                ym = f"{y:04d}-{m:02d}"
                if ym in completed and not ignore_checkpoint:
                    print(f"[skip] {ym} already completed")
                    continue
                print(f"[scrape] {ym}")
                stats = scrape_month(
                    y, m, client, raw, conn, delay,
                    dry_run=dry_run, include_all_posts=include_all_posts,
                )
                all_stats.append(stats)
                extra = ""
                if include_all_posts:
                    extra = (
                        f", other_tyler={stats.other_tyler_posts_fetched}"
                        f", non_tyler={stats.posts_skipped_non_tyler}"
                    )
                print(
                    f"  → pages={stats.archive_pages_fetched}, "
                    f"assorted={stats.assorted_links_posts_fetched}"
                    f"{extra}, "
                    f"skipped={stats.posts_skipped_already_present}, "
                    f"errors={len(stats.errors)}"
                )
                # A month only counts as complete if it fetched cleanly.
                # A partial month gets retried on the next resume.
                if not dry_run and not stats.errors:
                    completed.add(ym)
                    save_checkpoint({"completed_months": sorted(completed)})
    finally:
        conn.close()

    return all_stats


def _summarize(stats_list: list[ScrapeStats]) -> None:
    total_al = sum(s.assorted_links_posts_fetched for s in stats_list)
    total_other = sum(s.other_tyler_posts_fetched for s in stats_list)
    total_non_tyler = sum(s.posts_skipped_non_tyler for s in stats_list)
    total_errors = sum(len(s.errors) for s in stats_list)
    total_skipped = sum(s.posts_skipped_already_present for s in stats_list)
    print(
        f"\n=== Summary: {len(stats_list)} month(s) processed, "
        f"{total_al} assorted-links posts, "
        f"{total_other} other Tyler posts, "
        f"{total_non_tyler} non-Tyler skipped, "
        f"{total_skipped} already-stored skipped, "
        f"{total_errors} errors ==="
    )
    for s in stats_list:
        if s.errors:
            print(f"  {s.month} errors ({len(s.errors)}):")
            for e in s.errors[:5]:
                print(f"    - {e}")
            if len(s.errors) > 5:
                print(f"    ... and {len(s.errors) - 5} more")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Harvest MR 'assorted links' posts from monthly archives."
    )
    parser.add_argument(
        "--start", default=MR_FIRST_MONTH,
        help=f"YYYY-MM inclusive (default: {MR_FIRST_MONTH}, MR's first month)",
    )
    parser.add_argument(
        "--end", default=current_month_ym(),
        help="YYYY-MM inclusive (default: current month)",
    )
    parser.add_argument(
        "--month",
        help="Shortcut for --start=YYYY-MM --end=YYYY-MM (single month scrape)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch but don't write anything to disk or DB",
    )
    parser.add_argument(
        "--ignore-checkpoint", action="store_true",
        help="Re-scrape months already marked complete",
    )
    parser.add_argument(
        "--include-all-posts", action="store_true",
        help=(
            "Fetch every Tyler-authored post (not just assorted-links). "
            "Byline is parsed from meta[name=author]; non-Tyler posts are skipped."
        ),
    )
    args = parser.parse_args()

    start_ym = args.month if args.month else args.start
    end_ym = args.month if args.month else args.end

    stats_list = scrape_range(
        start_ym, end_ym,
        dry_run=args.dry_run,
        ignore_checkpoint=args.ignore_checkpoint,
        include_all_posts=args.include_all_posts,
    )
    _summarize(stats_list)


if __name__ == "__main__":
    main()
