"""Tests for src/score.py — track classification, harvesting (archive + RSS),
candidate matching, and the recall/MRR metrics. All offline (fixture RSS +
synthetic in-memory DB rows)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.db import connect, init_schema
from src import score
from src.score import (
    classify_track,
    harvest_from_archive,
    harvest_from_rss,
    match_unscored,
    mrr,
    prune_full_text,
    nber_paper_id,
    parse_tyler_posts_rss,
    recall_at_k,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


def _db():
    conn = connect(":memory:")
    init_schema(conn)
    return conn


# --- classification -------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("https://foo.substack.com/p/bar", "substack"),
    ("https://slowboring.com/p/hello", "substack"),          # custom domain
    ("https://nber.org/papers/w35373", "nber"),
    ("https://www.nytimes.com/2026/07/01/x.html", "other"),
    ("https://nber.org/digest/jul26", "other"),              # nber but not a paper
])
def test_classify_track(url, expected):
    substack_domains = {"slowboring.com"}
    assert classify_track(url, substack_domains) == expected


def test_nber_paper_id():
    assert nber_paper_id("https://nber.org/papers/w35373") == "w35373"
    assert nber_paper_id("https://nber.org/digest") is None


# --- RSS harvest ----------------------------------------------------------

def test_parse_tyler_posts_rss_filters_by_author():
    posts = parse_tyler_posts_rss((FIXTURES / "mr_feed.xml").read_bytes())
    # Fixture has 2 Tyler items + 1 Alex item; Alex is excluded.
    assert len(posts) == 2
    assert all(p.links for p in posts)


def test_rss_skips_internal_and_nonhttp_links():
    xml = (
        b'<?xml version="1.0"?>'
        b'<rss xmlns:dc="http://purl.org/dc/elements/1.1/" '
        b'xmlns:content="http://purl.org/rss/1.0/modules/content/" version="2.0"><channel>'
        b"<item><title>T</title><link>https://marginalrevolution.com/p1</link>"
        b"<dc:creator>Tyler Cowen</dc:creator>"
        b"<pubDate>Sat, 18 Jul 2026 09:00:00 GMT</pubDate>"
        b"<content:encoded><![CDATA["
        b'<a href="https://foo.substack.com/p/x">ok</a>'
        b'<a href="https://marginalrevolution.com/internal">skip</a>'
        b'<a href="mailto:t@x.com">skip</a>'
        b'<a href="#anchor">skip</a>'
        b'<a href="https://nber.org/papers/w1">ok2</a>'
        b"]]></content:encoded></item>"
        b"</channel></rss>"
    )
    posts = parse_tyler_posts_rss(xml)
    assert [href for _, href in posts[0].links] == [
        "https://foo.substack.com/p/x", "https://nber.org/papers/w1"]


# --- archive harvest ------------------------------------------------------

def _seed_archive_post(conn, pid, url, date_, links):
    conn.execute(
        "INSERT INTO archive_posts (id, url, title, published_at, month, "
        "scraped_at, author) VALUES (?, ?, 'T', ?, ?, ?, 'Tyler Cowen')",
        (pid, url, date_, date_[:7], NOW.isoformat()),
    )
    for pos, href in links:
        conn.execute(
            "INSERT INTO archive_links (archive_post_id, position, href, "
            "canonical_href, anchor_text) VALUES (?, ?, ?, ?, 'a')",
            (pid, pos, href, href),
        )
    conn.commit()


def test_harvest_from_archive_classifies_and_flags_out_of_scope():
    conn = _db()
    conn.execute("INSERT INTO nber_paper_dates (paper_id, release_date, fetched_at) "
                 "VALUES ('w900', '2020-01-01', ?)", (NOW.isoformat(),))
    _seed_archive_post(conn, 1, "https://mr.com/post1", "2026-07-10", [
        (1, "https://foo.substack.com/p/fresh"),      # substack -> unmatched
        (2, "https://nber.org/papers/w900"),          # released 2020 -> out_of_scope
        (3, "https://nytimes.com/a"),                 # other -> out_of_corpus
    ])
    s = harvest_from_archive(conn, start_month="2022-01", now=NOW)
    assert s.posts == 1 and s.links == 3
    rows = {r["canonical_url"]: r for r in conn.execute(
        "SELECT canonical_url, track, match_type, is_substack FROM ground_truth")}
    assert rows["https://foo.substack.com/p/fresh"]["match_type"] == "unmatched"
    assert rows["https://foo.substack.com/p/fresh"]["is_substack"] == 1
    assert rows["https://nber.org/papers/w900"]["match_type"] == "out_of_scope"
    assert rows["https://nytimes.com/a"]["match_type"] == "out_of_corpus"


def test_harvest_is_idempotent():
    conn = _db()
    _seed_archive_post(conn, 1, "https://mr.com/post1", "2026-07-10",
                       [(1, "https://foo.substack.com/p/x")])
    harvest_from_archive(conn, now=NOW)
    harvest_from_archive(conn, now=NOW)      # second run must not duplicate
    assert conn.execute("SELECT COUNT(*) FROM ground_truth").fetchone()[0] == 1


# --- matching -------------------------------------------------------------

def _seed_candidate(conn, cid, pub_id, canonical, track, ingested, published):
    conn.execute(
        "INSERT INTO candidates (id, publication_id, url, canonical_url, "
        "published_at, ingested_at, raw_entry_json, track) "
        "VALUES (?, ?, ?, ?, ?, ?, '{}', ?)",
        (cid, pub_id, canonical, canonical, published, ingested, track),
    )
    conn.commit()


def _seed_gt(conn, gid, canonical, track, date_, match_type="unmatched"):
    conn.execute(
        "INSERT INTO ground_truth (id, mr_post_url, mr_post_date, link_position, "
        "raw_url, canonical_url, is_substack, track, match_type, scored_at) "
        "VALUES (?, 'mr', ?, ?, ?, ?, ?, ?, ?, ?)",
        (gid, date_, gid, canonical, canonical, 1 if track == "substack" else 0,
         track, match_type, NOW.isoformat()),
    )
    conn.commit()


def test_match_unscored_exact_within_window():
    conn = _db()
    conn.execute("INSERT INTO publications (id, name, feed_url, canonical_domain, "
                 "added_date, source, corpus_version) VALUES "
                 "(1,'P','u','foo.substack.com','2026-01-01','manual','v1')")
    _seed_candidate(conn, 10, 1, "https://foo.substack.com/p/x", "substack",
                    ingested="2026-07-08", published="2026-07-08")
    _seed_gt(conn, 1, "https://foo.substack.com/p/x", "substack", "2026-07-10")
    stats = match_unscored(conn, substack_window=4, nber_window=14)
    assert stats.exact == 1
    row = conn.execute("SELECT match_type, matched_candidate_id, match_lag_days "
                       "FROM ground_truth WHERE id=1").fetchone()
    assert row["match_type"] == "exact"
    assert row["matched_candidate_id"] == 10
    assert row["match_lag_days"] == 2


def test_match_unscored_outside_window_stays_unmatched():
    conn = _db()
    conn.execute("INSERT INTO publications (id, name, feed_url, canonical_domain, "
                 "added_date, source, corpus_version) VALUES "
                 "(1,'P','u','foo.substack.com','2026-01-01','manual','v1')")
    _seed_candidate(conn, 10, 1, "https://foo.substack.com/p/x", "substack",
                    ingested="2026-07-01", published="2026-07-01")   # 9 days before
    _seed_gt(conn, 1, "https://foo.substack.com/p/x", "substack", "2026-07-10")
    stats = match_unscored(conn, substack_window=4, nber_window=14)
    assert stats.exact == 0 and stats.still_unmatched == 1


def test_prune_full_text_drops_only_past_window():
    conn = _db()
    conn.execute("INSERT INTO publications (id, name, feed_url, canonical_domain, "
                 "added_date, source, corpus_version) VALUES "
                 "(1,'P','u','a.substack.com','2026-01-01','manual','v1')")
    # substack window = 4 days; "now" = 2026-07-18
    now = NOW
    # old candidate (ingested 10 days ago) -> pruned; recent (today) -> kept
    _seed_candidate(conn, 1, 1, "https://a.substack.com/p/old", "substack",
                    ingested="2026-07-08", published="2026-07-08")
    _seed_candidate(conn, 2, 1, "https://a.substack.com/p/new", "substack",
                    ingested="2026-07-18", published="2026-07-18")
    conn.execute("UPDATE candidates SET full_text='<p>body</p>' WHERE id IN (1,2)")
    conn.commit()
    pruned = prune_full_text(conn, substack_window=4, nber_window=14, now=now)
    assert pruned == 1
    bodies = dict(conn.execute("SELECT id, full_text FROM candidates").fetchall())
    assert bodies[1] is None       # past window -> nulled
    assert bodies[2] == "<p>body</p>"   # still in window -> kept


def test_match_unscored_same_publication():
    conn = _db()
    conn.execute("INSERT INTO publications (id, name, feed_url, canonical_domain, "
                 "added_date, source, corpus_version) VALUES "
                 "(1,'P','u','foo.substack.com','2026-01-01','manual','v1')")
    # candidate is a DIFFERENT post from the same publication
    _seed_candidate(conn, 10, 1, "https://foo.substack.com/p/other", "substack",
                    ingested="2026-07-09", published="2026-07-09")
    _seed_gt(conn, 1, "https://foo.substack.com/p/x", "substack", "2026-07-10")
    stats = match_unscored(conn, substack_window=4, nber_window=14)
    assert stats.same_publication == 1
    assert conn.execute("SELECT match_type FROM ground_truth WHERE id=1"
                        ).fetchone()[0] == "same_publication"


# --- metrics --------------------------------------------------------------

def _seed_run_and_pred(conn, run_id, kind, model, candidate_id, rank, run_date):
    conn.execute(
        "INSERT OR IGNORE INTO runs (run_id, run_date, kind, models_json, "
        "prompt_version, corpus_version, candidates_count, started_at) "
        "VALUES (?, ?, ?, '[]', 'v1', 'v1', 1, ?)",
        (run_id, run_date, kind, NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO predictions (run_id, run_date, model, prompt_version, "
        "corpus_version, candidate_id, rank, score, created_at) "
        "VALUES (?, ?, ?, 'v1', 'v1', ?, ?, 50, ?)",
        (run_id, run_date, model, candidate_id, rank, NOW.isoformat()),
    )
    conn.commit()


def _min_candidate(conn, cid):
    """Seed publication 1 (once) and a minimal candidate with the given id, so
    ground_truth.matched_candidate_id / predictions.candidate_id FKs resolve."""
    conn.execute("INSERT OR IGNORE INTO publications (id, name, feed_url, "
                 "canonical_domain, added_date, source, corpus_version) "
                 "VALUES (1,'P','u','x.com','2026-01-01','manual','v1')")
    conn.execute("INSERT OR IGNORE INTO candidates (id, publication_id, url, "
                 "canonical_url, published_at, ingested_at, raw_entry_json) "
                 "VALUES (?, 1, ?, ?, '2026-07-09', '2026-07-09', '{}')",
                 (cid, f"c{cid}", f"c{cid}"))
    conn.commit()


def _matched_gt(conn, gid, cid, date_):
    _min_candidate(conn, cid)
    conn.execute(
        "INSERT INTO ground_truth (id, mr_post_url, mr_post_date, link_position, "
        "raw_url, canonical_url, is_substack, track, matched_candidate_id, "
        "match_type, scored_at) VALUES (?, 'mr', ?, ?, 'u', ?, 1, 'substack', ?, "
        "'exact', ?)",
        (gid, date_, gid, f"u{gid}", cid, NOW.isoformat()),
    )
    conn.commit()


def test_recall_and_mrr_with_best_rank_aggregation():
    conn = _db()
    # Two opportunities; model M ranks pick #1 at rank 3 (twice: r5 then r3 -> best 3),
    # and pick #2 at rank 25.
    _matched_gt(conn, 1, 100, "2026-07-10")
    _matched_gt(conn, 2, 200, "2026-07-10")
    _seed_run_and_pred(conn, "r-08", "live", "M", 100, 5, "2026-07-08")
    _seed_run_and_pred(conn, "r-09", "live", "M", 100, 3, "2026-07-09")   # best
    _seed_run_and_pred(conn, "r-10", "live", "M", 200, 25, "2026-07-09")

    hits20, opps = recall_at_k(conn, "M", "substack", 20, kind="live")
    assert (hits20, opps) == (1, 2)          # only pick #1 (rank 3) is <=20
    hits50, _ = recall_at_k(conn, "M", "substack", 50, kind="live")
    assert hits50 == 2                        # both <=50
    # MRR = mean(1/3, 1/25)
    assert mrr(conn, "M", "substack", kind="live") == pytest.approx((1/3 + 1/25) / 2)


def test_backtest_predictions_excluded_from_live_metrics():
    conn = _db()
    _matched_gt(conn, 1, 100, "2026-07-10")
    _seed_run_and_pred(conn, "bt", "backtest", "M", 100, 1, "2026-07-09")
    # A backtest rank-1 must not count as a live hit.
    assert recall_at_k(conn, "M", "substack", 20, kind="live") == (0, 1)
    assert recall_at_k(conn, "M", "substack", 20, kind="backtest") == (1, 1)
