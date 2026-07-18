"""Tests for src/ingest.py — feed parsing (offline, fixture-driven) and the
source-agnostic orchestration (dedup, failure counting, inactivation).

No network: adapter parsing runs against fixture XML, and the orchestrator is
exercised with a stub adapter whose fetch() returns canned bytes or raises.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.db import connect, init_schema
from src import ingest
from src.ingest import (
    CandidateRecord,
    FeedTarget,
    NberAdapter,
    SubstackAdapter,
    TrackSummary,
    _parse_feed_date,
    ingest_track,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


# --- date parsing ---------------------------------------------------------

def test_parse_feed_date_rss_rfc822():
    dt = _parse_feed_date("Tue, 14 Jul 2026 09:00:00 GMT")
    assert dt == datetime(2026, 7, 14, 9, 0, 0, tzinfo=timezone.utc)


def test_parse_feed_date_atom_iso():
    dt = _parse_feed_date("2026-07-14T09:00:00Z")
    assert dt == datetime(2026, 7, 14, 9, 0, 0, tzinfo=timezone.utc)


def test_parse_feed_date_naive_assumed_utc():
    assert _parse_feed_date("2026-07-14T09:00:00").tzinfo is timezone.utc


def test_parse_feed_date_missing_or_garbage():
    assert _parse_feed_date(None) is None
    assert _parse_feed_date("not a date") is None


# --- SubstackAdapter parsing ---------------------------------------------

def _substack_records():
    raw = (FIXTURES / "substack_feed.xml").read_bytes()
    target = FeedTarget(1, "Example", "https://example.substack.com/feed", "substack")
    return SubstackAdapter(lookback_hours=48).parse(raw, target, NOW)


def test_substack_drops_entries_older_than_lookback():
    recs = _substack_records()
    # Fresh post (2026-07-14) kept; stale post (2026-05-01) outside 48h dropped.
    assert [r.title for r in recs] == ["The Fresh Post"]


def test_substack_extracts_fields_and_canonicalizes():
    rec = _substack_records()[0]
    assert rec.author == "Jane Writer"
    assert rec.subtitle == "A short subtitle for the fresh post."
    assert rec.full_text == "<p>The full body of the fresh post.</p>"
    assert rec.track == "substack"
    # utm_source + Substack `r` referrer stripped by canonicalize().
    assert rec.canonical_url == "https://example.substack.com/p/the-fresh-post"


def test_substack_undated_entry_is_kept():
    # An item with no pubDate can't be windowed; keep it (UNIQUE index guards dups).
    xml = (
        b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b"<item><title>No Date</title>"
        b"<link>https://example.substack.com/p/no-date</link></item>"
        b"</channel></rss>"
    )
    target = FeedTarget(1, "Example", "url", "substack")
    recs = SubstackAdapter(lookback_hours=48).parse(xml, target, NOW)
    assert [r.title for r in recs] == ["No Date"]


# --- NberAdapter parsing --------------------------------------------------

def _nber_records():
    raw = (FIXTURES / "nber_feed.xml").read_bytes()
    target = FeedTarget(2, "NBER", "https://back.nber.org/rss/new.xml", "nber")
    return NberAdapter({"feed_url": "https://back.nber.org/rss/new.xml"}).parse(
        raw, target, NOW
    )


def test_nber_splits_title_and_authors():
    rec = _nber_records()[0]
    assert rec.title == "How Do State “Auto-IRA” Policies Affect Household Balance Sheets?"
    assert rec.author.startswith("Adam Bloomfield")
    assert rec.track == "nber"


def test_nber_canonical_matches_archive_form():
    # Live feed link (https, #fromrss anchor) must canonicalize to the same form
    # ground-truth NBER links resolve to, so matching is exact.
    rec = _nber_records()[0]
    assert rec.canonical_url == "https://nber.org/papers/w35373"


def test_nber_abstract_becomes_subtitle():
    rec = _nber_records()[0]
    assert rec.subtitle and rec.subtitle == rec.full_text
    assert "Auto-IRA" in rec.subtitle


def test_nber_first_seen_date_used():
    rec = _nber_records()[0]
    assert rec.published_at == NOW.isoformat()


def test_nber_skips_non_paper_items():
    xml = (
        b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b"<item><title>NBER Digest -- by Staff</title>"
        b"<link>https://www.nber.org/digest/jul26</link></item>"
        b"<item><title>Real Paper -- by A. Economist</title>"
        b"<link>https://www.nber.org/papers/w99999</link></item>"
        b"</channel></rss>"
    )
    target = FeedTarget(2, "NBER", "url", "nber")
    recs = NberAdapter({"feed_url": "url"}).parse(xml, target, NOW)
    assert [r.canonical_url for r in recs] == ["https://nber.org/papers/w99999"]


# --- Orchestration (stub adapter, no network) -----------------------------

class StubAdapter:
    """Adapter whose fetch() returns canned bytes (or raises) — no network."""

    def __init__(self, track, targets, records_by_pub=None, fail_pubs=()):
        self.track = track
        self._targets = targets
        self._records = records_by_pub or {}
        self._fail = set(fail_pubs)

    def discover(self, conn):
        return self._targets

    def fetch(self, target, client):
        if target.publication_id in self._fail:
            raise RuntimeError("boom")
        return b"<rss/>"

    def parse(self, raw, target, now):
        return self._records.get(target.publication_id, [])


def _mem_db():
    conn = connect(":memory:")
    init_schema(conn)
    return conn


def _rec(pub_id, canonical, track="substack"):
    return CandidateRecord(
        publication_id=pub_id, track=track, url=canonical, canonical_url=canonical,
        title="T", subtitle="S", author="A", published_at=NOW.isoformat(),
        full_text=None, raw_entry_json="{}",
    )


def _seed_pub(conn, pub_id, feed_url, track="substack"):
    conn.execute(
        "INSERT INTO publications (id, name, feed_url, canonical_domain, "
        "added_date, source, corpus_version, active, track) "
        "VALUES (?, 'P', ?, 'example.com', '2026-01-01', 'archive_derived', "
        "'v1.0', 1, ?)",
        (pub_id, feed_url, track),
    )
    conn.commit()


def test_ingest_inserts_and_dedups(tmp_path):
    conn = _mem_db()
    _seed_pub(conn, 1, "u1")
    targets = [FeedTarget(1, "P", "u1", "substack")]
    adapter = StubAdapter("substack", targets, {
        1: [_rec(1, "https://x.com/a"), _rec(1, "https://x.com/b")],
    })
    store = ingest.LocalFSBackend(root=tmp_path)

    s = ingest_track(conn, adapter, store, delay_seconds=0,
                     inactive_threshold=7, user_agent="ua", now=NOW)
    assert (s.new_candidates, s.duplicates) == (2, 0)

    # Re-running sees both as duplicates (canonical_url UNIQUE).
    s2 = ingest_track(conn, adapter, store, delay_seconds=0,
                      inactive_threshold=7, user_agent="ua", now=NOW)
    assert (s2.new_candidates, s2.duplicates) == (0, 2)
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 2


def test_ingest_saves_raw_verbatim(tmp_path):
    conn = _mem_db()
    _seed_pub(conn, 1, "https://example.substack.com/feed")
    targets = [FeedTarget(1, "P", "https://example.substack.com/feed", "substack")]
    adapter = StubAdapter("substack", targets, {1: []})
    store = ingest.LocalFSBackend(root=tmp_path)

    ingest_track(conn, adapter, store, delay_seconds=0,
                 inactive_threshold=7, user_agent="ua", now=NOW)
    key = f"2026-07-15/feeds/substack/{ingest._safe_slug(targets[0])}.xml"
    assert store.exists(key)
    assert store.get(key) == b"<rss/>"


def test_ingest_failure_counts_and_inactivates(tmp_path):
    conn = _mem_db()
    _seed_pub(conn, 1, "u1")
    targets = [FeedTarget(1, "P", "u1", "substack")]
    adapter = StubAdapter("substack", targets, fail_pubs={1})
    store = ingest.LocalFSBackend(root=tmp_path)

    # Threshold 2: first failure counts but stays active, second inactivates.
    s1 = ingest_track(conn, adapter, store, delay_seconds=0,
                      inactive_threshold=2, user_agent="ua", now=NOW)
    assert s1.feed_failures == 1 and s1.marked_inactive == 0
    row = conn.execute(
        "SELECT consecutive_failures, active FROM publications WHERE id=1"
    ).fetchone()
    assert (row["consecutive_failures"], row["active"]) == (1, 1)

    s2 = ingest_track(conn, adapter, store, delay_seconds=0,
                      inactive_threshold=2, user_agent="ua", now=NOW)
    assert s2.marked_inactive == 1
    row = conn.execute("SELECT active FROM publications WHERE id=1").fetchone()
    assert row["active"] == 0


def test_ingest_success_resets_failure_counter(tmp_path):
    conn = _mem_db()
    _seed_pub(conn, 1, "u1")
    conn.execute("UPDATE publications SET consecutive_failures = 3 WHERE id=1")
    conn.commit()
    targets = [FeedTarget(1, "P", "u1", "substack")]
    adapter = StubAdapter("substack", targets, {1: []})
    store = ingest.LocalFSBackend(root=tmp_path)

    ingest_track(conn, adapter, store, delay_seconds=0,
                 inactive_threshold=7, user_agent="ua", now=NOW)
    row = conn.execute(
        "SELECT consecutive_failures FROM publications WHERE id=1"
    ).fetchone()
    assert row["consecutive_failures"] == 0


# --- NBER publication seeding --------------------------------------------

def test_nber_ensure_publication_idempotent():
    conn = _mem_db()
    cfg = {"feed_url": "https://back.nber.org/rss/new.xml",
           "publication_name": "NBER Working Papers", "corpus_version": "nber-v1.0"}
    a = NberAdapter(cfg)
    t1 = a.discover(conn)
    t2 = a.discover(conn)
    assert t1[0].publication_id == t2[0].publication_id
    rows = conn.execute(
        "SELECT track, source, corpus_version FROM publications WHERE canonical_domain='nber.org'"
    ).fetchall()
    assert len(rows) == 1
    assert (rows[0]["track"], rows[0]["source"], rows[0]["corpus_version"]) == (
        "nber", "manual", "nber-v1.0")
