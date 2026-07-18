"""SQLite schema and connection helpers.

Portable SQL — no SQLite-only tricks. The DB may migrate to Postgres if this
prototype outgrows a single machine. Timestamps stored as ISO-8601 TEXT.
Booleans stored as INTEGER 0/1. JSON blobs stored as TEXT (not JSONB) so the
schema is source-agnostic.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    feed_url TEXT NOT NULL UNIQUE,
    canonical_domain TEXT NOT NULL,
    added_date TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('archive_derived', 'manual')),
    corpus_version TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    -- Which eval track this publication feeds. Substack watchlist vs. the
    -- parallel NBER working-paper track (D-24). Tracks are scored and reported
    -- separately and MUST NOT blend into one leaderboard.
    track TEXT NOT NULL DEFAULT 'substack',
    -- Consecutive failed fetches; reset to 0 on any success. A feed is marked
    -- inactive once this reaches `inactive_after_consecutive_failures` (config).
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_publications_active
    ON publications(active);
CREATE INDEX IF NOT EXISTS idx_publications_canonical_domain
    ON publications(canonical_domain);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    publication_id INTEGER NOT NULL REFERENCES publications(id),
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    title TEXT,
    subtitle TEXT,
    author TEXT,
    published_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    full_text TEXT,
    raw_entry_json TEXT NOT NULL,
    track TEXT NOT NULL DEFAULT 'substack'   -- denormalized from publications (D-24)
);

CREATE INDEX IF NOT EXISTS idx_candidates_publication
    ON candidates(publication_id);
CREATE INDEX IF NOT EXISTS idx_candidates_published
    ON candidates(published_at);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    run_date TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('live', 'backtest')),
    models_json TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    corpus_version TEXT NOT NULL,
    candidates_count INTEGER NOT NULL,
    errors_json TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    track TEXT NOT NULL DEFAULT 'substack'   -- a run scores exactly one track (D-24)
);

CREATE INDEX IF NOT EXISTS idx_runs_date_kind
    ON runs(run_date, kind);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    run_date TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    corpus_version TEXT NOT NULL,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    rank INTEGER NOT NULL,
    score INTEGER NOT NULL,
    rationale TEXT,
    created_at TEXT NOT NULL,
    track TEXT NOT NULL DEFAULT 'substack',   -- denormalized so leaderboards filter without a join (D-24)
    UNIQUE(run_id, model, candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_predictions_run
    ON predictions(run_id);
CREATE INDEX IF NOT EXISTS idx_predictions_model
    ON predictions(model);
CREATE INDEX IF NOT EXISTS idx_predictions_candidate
    ON predictions(candidate_id);
CREATE INDEX IF NOT EXISTS idx_predictions_date
    ON predictions(run_date);

CREATE TABLE IF NOT EXISTS ground_truth (
    id INTEGER PRIMARY KEY,
    mr_post_url TEXT NOT NULL,
    mr_post_date TEXT NOT NULL,
    link_position INTEGER NOT NULL,
    raw_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    is_substack INTEGER NOT NULL,
    -- Which eval track scores this link (D-24): 'substack', 'nber', or 'other'
    -- (a link belonging to no track — nytimes, twitter — kept for the
    -- link-share statistic but never scored).
    track TEXT NOT NULL DEFAULT 'substack',
    matched_candidate_id INTEGER REFERENCES candidates(id),
    match_type TEXT NOT NULL CHECK (match_type IN (
        'exact', 'same_publication', 'content_match',
        'unmatched', 'out_of_corpus',
        -- NBER classic-paper resurfacing: released > window before the link
        -- (D-27). Counted and logged, excluded from the recall denominator.
        'out_of_scope'
    )),
    match_lag_days INTEGER,
    scored_at TEXT NOT NULL,
    UNIQUE(mr_post_url, link_position)
);

CREATE INDEX IF NOT EXISTS idx_gt_track ON ground_truth(track);

CREATE INDEX IF NOT EXISTS idx_gt_date
    ON ground_truth(mr_post_date);
CREATE INDEX IF NOT EXISTS idx_gt_matched
    ON ground_truth(matched_candidate_id);

-- Historical MR "assorted links" posts harvested by src/archive_scraper.py.
-- Raw HTML stays on disk (gitignored); structured extractions live here so
-- downstream jobs (watchlist builder, few-shot generator, ground-truth
-- validation) query a single committed source of truth.
CREATE TABLE IF NOT EXISTS archive_posts (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    published_at TEXT,
    month TEXT NOT NULL,       -- YYYY-MM, indexed for range queries
    scraped_at TEXT NOT NULL,
    author TEXT,               -- verbatim byline from meta[name="author"]
    has_substack_link INTEGER NOT NULL DEFAULT 0   -- 0/1, computed at insert
);

CREATE INDEX IF NOT EXISTS idx_archive_posts_month
    ON archive_posts(month);

CREATE TABLE IF NOT EXISTS archive_links (
    id INTEGER PRIMARY KEY,
    archive_post_id INTEGER NOT NULL REFERENCES archive_posts(id),
    position INTEGER NOT NULL,
    href TEXT NOT NULL,
    canonical_href TEXT NOT NULL,
    anchor_text TEXT,
    surrounding_text TEXT,
    UNIQUE(archive_post_id, position)
);

CREATE INDEX IF NOT EXISTS idx_archive_links_canonical
    ON archive_links(canonical_href);
CREATE INDEX IF NOT EXISTS idx_archive_links_post
    ON archive_links(archive_post_id);

-- NBER working-paper release dates (day-granular), from each paper page's
-- `citation_publication_date` meta tag. Needed to compute Tyler's link-lag and
-- to classify a pick as fresh vs. out-of-scope "classic paper" (D-27). Seeded
-- from the 501 papers Tyler linked; live ingestion / backtest can enrich it.
CREATE TABLE IF NOT EXISTS nber_paper_dates (
    paper_id TEXT PRIMARY KEY,          -- e.g. 'w35373'
    release_date TEXT NOT NULL,         -- ISO-8601 date
    fetched_at TEXT NOT NULL
);
"""


DEFAULT_DB_PATH = Path("data/tyler.db")


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection with foreign keys enabled and dict-like row access."""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes; run column-add migrations for existing DBs.

    `CREATE TABLE IF NOT EXISTS` doesn't add missing columns to an already-created
    table, so we also run a small idempotent migration pass that ADD COLUMNs
    against the current schema when they aren't present.
    """
    _pre_migrate(conn)
    conn.executescript(SCHEMA_SQL)
    _apply_migrations(conn)
    conn.commit()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _pre_migrate(conn: sqlite3.Connection) -> None:
    """Rebuild tables whose *shape* changed (new CHECK/columns an ALTER can't
    do) BEFORE `executescript` runs — otherwise SCHEMA_SQL's indexes on the new
    columns would fail against the still-old table. Only drops empty tables so
    real data is never lost.
    """
    gt_cols = _columns(conn, "ground_truth")
    if gt_cols and "track" not in gt_cols:   # exists, old shape
        n = conn.execute("SELECT COUNT(*) FROM ground_truth").fetchone()[0]
        if n:
            raise RuntimeError(
                f"ground_truth has {n} rows; refusing to auto-rebuild for the "
                "track/out_of_scope migration — migrate the data manually."
            )
        conn.execute("DROP TABLE ground_truth")   # executescript recreates it


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE for columns added after initial schema."""
    archive_cols = _columns(conn, "archive_posts")
    if "author" not in archive_cols:
        conn.execute("ALTER TABLE archive_posts ADD COLUMN author TEXT")
    if "has_substack_link" not in archive_cols:
        conn.execute(
            "ALTER TABLE archive_posts ADD COLUMN "
            "has_substack_link INTEGER NOT NULL DEFAULT 0"
        )

    # `track` splits the Substack and NBER eval tracks (D-24). Added to every
    # table whose rows belong to one track. Default 'substack' back-fills the
    # pre-existing Substack-only rows correctly.
    for table in ("publications", "candidates", "runs", "predictions"):
        if "track" not in _columns(conn, table):
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN "
                "track TEXT NOT NULL DEFAULT 'substack'"
            )

    # Per-feed failure counter for the inactive-after-N rule (brief §5.3).
    if "consecutive_failures" not in _columns(conn, "publications"):
        conn.execute(
            "ALTER TABLE publications ADD COLUMN "
            "consecutive_failures INTEGER NOT NULL DEFAULT 0"
        )



@contextmanager
def transaction(conn: sqlite3.Connection):
    """Commit-or-rollback around a block; re-raises on failure."""
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
