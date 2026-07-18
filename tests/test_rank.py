"""Tests for src/rank.py — prompt assembly, defensive parsing, retries, storage,
and cost estimation. A fake LLM client stands in for OpenRouter: zero spend, no
network."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.db import connect, init_schema
from src.rank import (
    Candidate,
    LLMResponse,
    body_text,
    build_prompt,
    cost_usd,
    estimate_run,
    format_candidate,
    load_candidates,
    parse_ranking,
    rank_one_model,
    run_ranking,
)

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


class FakeClient:
    """Returns a canned response (or raises a preset number of times first)."""

    def __init__(self, text="", fail_times=0, exc=RuntimeError("boom"),
                 prompt_tokens=1000, completion_tokens=200):
        self.text = text
        self.fail_times = fail_times
        self.exc = exc
        self.calls = 0
        self._pt, self._ct = prompt_tokens, completion_tokens

    def complete(self, model, system, user):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.exc
        return LLMResponse(self.text, self._pt, self._ct)


# --- prompt assembly ------------------------------------------------------

def test_format_candidate_includes_body():
    c = Candidate(41, "The Zvi", "Zvi", "A title", "A subtitle", body="the full body text")
    block = format_candidate(c)
    assert "### [41] A title" in block
    assert "Publication: The Zvi · Author: Zvi" in block
    assert "the full body text" in block          # body preferred over subtitle


def test_format_candidate_falls_back_to_subtitle():
    c = Candidate(41, "The Zvi", "Zvi", "A title", "A subtitle")   # no body
    assert "A subtitle" in format_candidate(c)


def test_body_text_strips_html_and_truncates():
    html = "<p>One two three</p><p>four five six seven</p>"
    assert body_text(html, 0) == "One two three four five six seven"   # no truncation
    assert body_text(html, 3) == "One two three […]"                   # truncated
    assert body_text(None, 10) == ""


def test_build_prompt_puts_candidates_in_user_message(tmp_path):
    (tmp_path / "ranker_instructions_v1.md").write_text("INSTR")
    (tmp_path / "taste_profile_v1.md").write_text("TASTE")
    (tmp_path / "few_shot_substack_v1.md").write_text("FEWSHOT")
    cands = [Candidate(1, "P", "A", "T", "S", body="BODYTEXT")]
    system, user = build_prompt("substack", cands, prompts_dir=tmp_path)
    # static content in system (cacheable), candidates in user
    assert "INSTR" in system and "TASTE" in system and "FEWSHOT" in system
    assert "### [1] T" in user and "BODYTEXT" in user
    assert "TASTE" not in user


# --- parsing --------------------------------------------------------------

def test_parse_ranking_happy_path():
    text = ('{"no_confident_picks": false, "picks": ['
            '{"candidate_id": 2, "rank": 2, "score_0_100": 70, "rationale": "b"},'
            '{"candidate_id": 1, "rank": 1, "score_0_100": 90, "rationale": "a"}]}')
    rankings, no_picks, warns = parse_ranking(text, {1, 2})
    assert not no_picks and not warns
    # re-sorted by model rank, densely re-ranked 1..n
    assert [(r.candidate_id, r.rank) for r in rankings] == [(1, 1), (2, 2)]


def test_parse_ranking_drops_hallucinated_and_duplicate_ids():
    text = ('{"picks": ['
            '{"candidate_id": 1, "rank": 1, "score_0_100": 90, "rationale": "a"},'
            '{"candidate_id": 999, "rank": 2, "score_0_100": 50, "rationale": "x"},'
            '{"candidate_id": 1, "rank": 3, "score_0_100": 40, "rationale": "dup"}]}')
    rankings, _, warns = parse_ranking(text, {1, 2})
    assert [r.candidate_id for r in rankings] == [1]
    assert any("hallucinated" in w for w in warns)
    assert any("duplicate" in w for w in warns)


def test_parse_ranking_no_confident_picks():
    rankings, no_picks, _ = parse_ranking('{"no_confident_picks": true, "picks": []}', {1})
    assert no_picks and rankings == []


def test_parse_ranking_clamps_score_and_survives_fences():
    text = '```json\n{"picks": [{"candidate_id": 1, "rank": 1, "score_0_100": 250}]}\n```'
    rankings, _, _ = parse_ranking(text, {1})
    assert rankings[0].score == 100


def test_parse_ranking_unparseable():
    rankings, no_picks, warns = parse_ranking("I cannot help with that.", {1})
    assert rankings == [] and not no_picks and warns


# --- retries --------------------------------------------------------------

def test_rank_one_model_retries_then_succeeds():
    client = FakeClient('{"picks": [{"candidate_id": 1, "rank": 1, "score_0_100": 80}]}',
                        fail_times=2)
    res = rank_one_model(client, "m", "s", "u", {1}, {}, retries=3, sleep=lambda s: None)
    assert client.calls == 3 and res.error is None and len(res.rankings) == 1


def test_rank_one_model_gives_up_after_retries():
    client = FakeClient(fail_times=5)
    res = rank_one_model(client, "m", "s", "u", {1}, {}, retries=3, sleep=lambda s: None)
    assert client.calls == 3 and res.error is not None and res.rankings == []


# --- cost -----------------------------------------------------------------

def test_cost_usd():
    pricing = {"input_per_mtok": 1.0, "output_per_mtok": 3.0}
    assert cost_usd(1_000_000, 1_000_000, pricing) == pytest.approx(4.0)


# --- end-to-end run (fake client, real DB) --------------------------------

def _seed_pool(conn, track, n):
    conn.execute("INSERT INTO publications (id, name, feed_url, canonical_domain, "
                 "added_date, source, corpus_version, track) VALUES "
                 "(1,'P','u','x.com','2026-01-01','manual','v1',?)", (track,))
    for i in range(1, n + 1):
        conn.execute("INSERT INTO candidates (id, publication_id, url, canonical_url, "
                     "published_at, ingested_at, raw_entry_json, track) VALUES "
                     "(?,1,?,?, '2026-07-18','2026-07-18','{}',?)",
                     (i, f"u{i}", f"u{i}", track))
    conn.commit()


def test_run_ranking_stores_predictions_and_run():
    conn = connect(":memory:")
    init_schema(conn)
    _seed_pool(conn, "substack", 3)
    client = FakeClient('{"picks": ['
                        '{"candidate_id": 1, "rank": 1, "score_0_100": 90, "rationale": "a"},'
                        '{"candidate_id": 3, "rank": 2, "score_0_100": 60, "rationale": "c"}]}')
    config = {"ranker_models": [{"model": "test/model",
                                 "pricing": {"input_per_mtok": 1, "output_per_mtok": 1}}],
              "prompt_version": "v1.0", "corpus_version": "v1.0"}
    results = run_ranking(conn, "substack", client, config, kind="live", now=NOW)
    assert len(results) == 1 and results[0].cost_usd > 0
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    preds = conn.execute("SELECT candidate_id, rank, track FROM predictions "
                         "ORDER BY rank").fetchall()
    assert [(p["candidate_id"], p["rank"]) for p in preds] == [(1, 1), (3, 2)]
    assert all(p["track"] == "substack" for p in preds)


def test_run_ranking_no_candidates_is_noop():
    conn = connect(":memory:")
    init_schema(conn)
    config = {"ranker_models": [{"model": "m"}]}
    assert run_ranking(conn, "nber", FakeClient(), config, now=NOW) == []
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_load_candidates_since_hours_filters_old():
    from datetime import datetime, timedelta, timezone
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute("INSERT INTO publications (id, name, feed_url, canonical_domain, "
                 "added_date, source, corpus_version, track) VALUES "
                 "(1,'P','u','x.com','2026-01-01','manual','v1','substack')")
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(days=30)).isoformat()
    for cid, ing in ((1, recent), (2, old)):
        conn.execute("INSERT INTO candidates (id, publication_id, url, canonical_url, "
                     "published_at, ingested_at, raw_entry_json, track) VALUES "
                     "(?,1,?,?,?,?,'{}','substack')", (cid, f"u{cid}", f"u{cid}", ing, ing))
    conn.commit()
    assert len(load_candidates(conn, "substack")) == 2                    # no filter
    fresh = load_candidates(conn, "substack", since_hours=48)             # last 48h only
    assert [c.id for c in fresh] == [1]


def test_estimate_run_is_free_and_reports_cost():
    conn = connect(":memory:")
    init_schema(conn)
    _seed_pool(conn, "nber", 5)
    config = {"ranker_models": [
        {"model": "cheap", "pricing": {"input_per_mtok": 0.5, "output_per_mtok": 2.0}}]}
    est = estimate_run(conn, "nber", config)
    assert est["candidates"] == 5
    assert est["prompt_tokens_est"] > 0
    assert est["per_model"][0]["cost_usd"] >= 0
