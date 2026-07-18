"""Tests for src/build_fewshot.py — gloss cleaning, quality filtering,
reproducible stratified sampling, and formatting. Offline (synthetic DB)."""

from __future__ import annotations

from datetime import datetime, timezone

from src.db import connect, init_schema
from src import build_fewshot
from src.build_fewshot import (
    Example,
    build,
    clean_gloss,
    format_example,
    is_quality,
    stratified_sample,
)

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc).isoformat()


# --- cleaning + quality ---------------------------------------------------

def test_clean_gloss_strips_list_number_and_whitespace():
    assert clean_gloss("3.  Foo   bar ") == "Foo bar"
    assert clean_gloss("12. A study") == "A study"
    assert clean_gloss(None) == ""


def test_is_quality_rules():
    assert is_quality("A serious long anchor", "Tyler's fuller gloss here")
    assert not is_quality("short", "Tyler's fuller gloss here")          # anchor too short
    assert not is_quality("A serious long anchor", "tiny")               # gloss too short
    # gloss identical to anchor adds no signal
    assert not is_quality("Same text here now", "Same text here now")


# --- stratified sampling reproducibility ----------------------------------

def _mk(year, i):
    return Example("Pub", f"anchor {year}-{i}", f"gloss {year}-{i}",
                   f"{year}-01-{i:02d}", str(year))


def test_stratified_sample_is_seeded_and_reproducible():
    pool = [_mk(y, i) for y in (2022, 2023, 2024) for i in range(1, 21)]
    a = stratified_sample(pool, 9, seed=42)
    b = stratified_sample(pool, 9, seed=42)
    assert [e.anchor for e in a] == [e.anchor for e in b]      # deterministic
    assert len(a) == 9


def test_stratified_sample_spreads_across_years():
    pool = [_mk(y, i) for y in (2022, 2023, 2024) for i in range(1, 21)]
    picked = stratified_sample(pool, 9, seed=1)
    by_year = {}
    for e in picked:
        by_year[e.year] = by_year.get(e.year, 0) + 1
    assert set(by_year) == {"2022", "2023", "2024"}
    assert all(v == 3 for v in by_year.values())               # 9 / 3 years


def test_different_seed_gives_different_sample():
    pool = [_mk(2022, i) for i in range(1, 41)]
    a = [e.anchor for e in stratified_sample(pool, 10, seed=1)]
    b = [e.anchor for e in stratified_sample(pool, 10, seed=2)]
    assert a != b


def test_format_example():
    e = Example("The Zvi", "Some post", "Tyler's take on some post", "2025-03-04", "2025")
    assert format_example(e) == "- The Zvi (2025-03-04): Tyler's take on some post"


# --- build() end-to-end on synthetic archive ------------------------------

def _seed_link(conn, pid, pos, href, anchor, gloss, date_):
    conn.execute(
        "INSERT OR IGNORE INTO archive_posts (id, url, title, published_at, month, "
        "scraped_at, author) VALUES (?, ?, 'T', ?, ?, ?, 'Tyler Cowen')",
        (pid, f"https://mr.com/{pid}", date_, date_[:7], NOW),
    )
    conn.execute(
        "INSERT INTO archive_links (archive_post_id, position, href, canonical_href, "
        "anchor_text, surrounding_text) VALUES (?, ?, ?, ?, ?, ?)",
        (pid, pos, href, __import__("src.normalize", fromlist=["canonicalize"]).canonicalize(href),
         anchor, gloss),
    )


def test_build_selects_only_quality_track_examples():
    conn = connect(":memory:")
    init_schema(conn)
    # one good substack link, one thin substack link, one NBER link, one nyt link
    _seed_link(conn, 1, 1, "https://foo.substack.com/p/good",
               "A genuinely good anchor", "3. A genuinely good anchor, and Tyler's aside", "2024-05-01")
    _seed_link(conn, 2, 1, "https://foo.substack.com/p/thin", "hi", "hi", "2024-05-02")
    _seed_link(conn, 3, 1, "https://nber.org/papers/w100",
               "An NBER paper title", "An NBER paper title, with a note", "2024-05-03")
    _seed_link(conn, 4, 1, "https://nytimes.com/x", "NYT article headline here",
               "NYT article headline here, discussed", "2024-05-04")
    conn.commit()

    sub = build(conn, "substack", n=75, seed=42, start_month="2022-01")
    assert [e.anchor for e in sub] == ["A genuinely good anchor"]   # thin + non-substack excluded
    assert sub[0].publication in ("foo.substack.com",)             # no watchlist name -> host
    assert sub[0].gloss.startswith("A genuinely good anchor, and")  # list number stripped

    nber = build(conn, "nber", n=75, seed=42, start_month="2022-01")
    assert [e.publication for e in nber] == ["NBER"]
