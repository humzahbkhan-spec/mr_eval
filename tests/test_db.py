"""Smoke tests for the schema: creates cleanly, is idempotent, enforces FKs."""

from __future__ import annotations

import sqlite3

import pytest

from src.db import connect, init_schema


EXPECTED_TABLES = {
    "publications", "candidates", "runs", "predictions", "ground_truth",
    "archive_posts", "archive_links",
}


def _fresh_in_memory() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    return conn


def test_all_tables_created():
    conn = _fresh_in_memory()
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert EXPECTED_TABLES <= tables, f"missing tables: {EXPECTED_TABLES - tables}"


def test_init_schema_is_idempotent():
    conn = _fresh_in_memory()
    init_schema(conn)  # second call must not fail


def test_foreign_key_enforced(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO candidates "
            "(publication_id, url, canonical_url, published_at, ingested_at, raw_entry_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (999, "https://x.com/y", "https://x.com/y", "2026-01-01", "2026-01-01", "{}"),
        )
        conn.commit()


def test_check_constraint_on_run_kind(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO runs "
            "(run_id, run_date, kind, models_json, prompt_version, corpus_version, "
            " candidates_count, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r1", "2026-07-12", "wrong-kind", "[]", "v1", "v1", 0, "2026-07-12T00:00:00"),
        )
        conn.commit()


def test_check_constraint_on_match_type(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ground_truth "
            "(mr_post_url, mr_post_date, link_position, raw_url, canonical_url, "
            " is_substack, match_type, scored_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("https://mr/x", "2026-07-12", 1, "https://x.com/y", "https://x.com/y",
             0, "bogus", "2026-07-12T00:00:00"),
        )
        conn.commit()
