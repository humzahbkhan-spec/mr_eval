"""Ranking engine: ask each model to rank today's candidates for one track.

Per run, per track, the pipeline assembles ONE prompt (taste profile + per-track
few-shot examples + ranker instructions + today's candidates) and sends it to
each configured model through a single OpenAI-compatible client (OpenRouter) —
one API call PER MODEL. Each model returns a JSON ranking that is parsed
defensively, validated against the candidate pool (hallucinated ids dropped and
logged), and stored as `predictions` rows under a `run`.

Ground-truth matching and metrics are NOT here — that's `score.py`, and it makes
no API calls.

Design notes:
  - Static content (instructions, taste profile, few-shot) goes first in the
    prompt and today's candidates last, to exploit provider prompt caching
    (brief §5.5).
  - The LLM client is injected (`LLMClient` protocol) so the whole engine is
    unit-tested with a fake model and zero spend. `OpenRouterClient` is the real
    implementation.
  - `estimate_run()` assembles the real prompt and reports token/cost estimates
    WITHOUT calling any model — the free pre-flight before a paid run.

Usage:
    python -m src.rank --track substack --estimate      # free: prompt + cost
    python -m src.rank --track substack                 # live: real API calls
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Protocol

import yaml

from src.db import connect, init_schema, transaction

CONFIG_PATH = Path("config.yaml")
PROMPTS_DIR = Path("prompts")


# --- Value types ----------------------------------------------------------

@dataclass
class Candidate:
    id: int
    publication: str
    author: str
    title: str
    subtitle: str
    body: str = ""          # (truncated) post body — what the model actually reads (D-33)


@dataclass
class Ranking:
    candidate_id: int
    rank: int
    score: int
    rationale: str


@dataclass
class LLMResponse:
    text: str
    prompt_tokens: int
    completion_tokens: int


@dataclass
class ModelResult:
    model: str
    rankings: list[Ranking]
    no_confident_picks: bool
    raw_text: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    error: Optional[str] = None


# --- LLM client -----------------------------------------------------------

class LLMClient(Protocol):
    def complete(self, model: str, system: str, user: str) -> LLMResponse: ...


class OpenRouterClient:
    """Real client: OpenRouter via the OpenAI-compatible SDK. One key, any model.

    Model strings are OpenRouter ids (e.g. 'anthropic/claude-opus-4.8',
    'moonshotai/kimi-k2.6'). The API key comes from OPENROUTER_API_KEY.
    """

    def __init__(self, base_url: str, api_key: Optional[str] = None) -> None:
        from openai import OpenAI     # lazy: keeps import/network out of tests
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        self._client = OpenAI(base_url=base_url, api_key=key)

    def complete(self, model: str, system: str, user: str) -> LLMResponse:
        resp = self._client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        usage = resp.usage
        return LLMResponse(
            text=resp.choices[0].message.content or "",
            prompt_tokens=getattr(usage, "prompt_tokens", 0),
            completion_tokens=getattr(usage, "completion_tokens", 0),
        )


# --- Prompt assembly ------------------------------------------------------

def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def format_candidate(c: Candidate) -> str:
    """A candidate block: an id-tagged header, a metadata line, then the
    (truncated) body — so the model reads the actual content, not just the title
    (D-33). Falls back to the subtitle when no body is present."""
    content = c.body or c.subtitle or ""
    return (
        f"### [{c.id}] {c.title or '(untitled)'}\n"
        f"Publication: {c.publication} · Author: {c.author or 'unknown'}\n"
        f"{content}"
    )


def build_prompt(track: str, candidates: list[Candidate],
                 prompts_dir: Path = PROMPTS_DIR) -> tuple[str, str]:
    """Return (system, user). Static content (instructions + taste profile +
    few-shot) is the system message so it caches across runs; today's candidates
    are the user message."""
    instructions = _read(prompts_dir / "ranker_instructions_v1.md")
    taste = _read(prompts_dir / "taste_profile_v1.md")
    few_shot = _read(prompts_dir / f"few_shot_{track}_v1.md")
    system = (
        f"{instructions}\n\n"
        f"# Taste profile\n\n{taste}\n\n"
        f"# Examples of prior selections\n\n{few_shot}"
    )
    lines = "\n\n".join(format_candidate(c) for c in candidates)
    user = (
        f"# Today's candidates ({track} track, {len(candidates)} posts)\n\n"
        f"{lines}\n\n"
        "Return the ranking JSON now."
    )
    return system, user


# --- Response parsing -----------------------------------------------------

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def _extract_json(text: str) -> Optional[dict]:
    """Best-effort: parse the response as JSON, tolerating code fences / prose."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # last resort: first '{' to last '}'
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(text[i:j + 1])
        except json.JSONDecodeError:
            return None
    return None


def parse_ranking(text: str, valid_ids: set[int]) -> tuple[list[Ranking], bool, list[str]]:
    """Parse a model response into (rankings, no_confident_picks, warnings).

    Defensive: drops picks with unknown/duplicate candidate_ids or malformed
    fields, clamps score to 0-100, and re-derives dense 1-indexed ranks from the
    model's ordering (so downstream rank metrics never see gaps or ties)."""
    warnings: list[str] = []
    obj = _extract_json(text)
    if obj is None:
        return [], False, ["unparseable response"]
    if obj.get("no_confident_picks") is True:
        return [], True, warnings

    picks = obj.get("picks")
    if not isinstance(picks, list):
        return [], False, ["no 'picks' array"]

    # Order by the model's stated rank (fallback: input order), then re-rank densely.
    def _rk(p):
        try:
            return int(p.get("rank"))
        except (TypeError, ValueError):
            return 10**9
    seen: set[int] = set()
    cleaned: list[Ranking] = []
    for p in sorted(picks, key=_rk):
        if not isinstance(p, dict):
            continue
        try:
            cid = int(p.get("candidate_id"))
        except (TypeError, ValueError):
            warnings.append("pick with non-integer candidate_id dropped")
            continue
        if cid not in valid_ids:
            warnings.append(f"hallucinated candidate_id {cid} dropped")
            continue
        if cid in seen:
            warnings.append(f"duplicate candidate_id {cid} dropped")
            continue
        seen.add(cid)
        try:
            score = int(p.get("score_0_100"))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(100, score))
        rationale = str(p.get("rationale") or "")[:500]
        cleaned.append(Ranking(cid, len(cleaned) + 1, score, rationale))
    return cleaned, False, warnings


# --- Cost -----------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for pre-flight cost, no tokenizer."""
    return max(1, len(text) // 4)


def cost_usd(prompt_tokens: int, completion_tokens: int, pricing: dict) -> float:
    """pricing = {'input_per_mtok': x, 'output_per_mtok': y} in USD."""
    return (prompt_tokens / 1_000_000) * pricing.get("input_per_mtok", 0.0) + \
           (completion_tokens / 1_000_000) * pricing.get("output_per_mtok", 0.0)


# --- Candidate loading ----------------------------------------------------

def body_text(full_text: str | None, max_words: int) -> str:
    """HTML-strip a stored post body and truncate to `max_words` (D-33). The
    models read this, so it must be plain prose, not markup."""
    if not full_text:
        return ""
    from bs4 import BeautifulSoup
    text = BeautifulSoup(full_text, "lxml").get_text(" ", strip=True)
    words = text.split()
    if max_words and len(words) > max_words:
        return " ".join(words[:max_words]) + " […]"
    return text


def load_candidates(conn, track: str, body_words: int = 0,
                    since_hours: int = 0) -> list[Candidate]:
    """Candidate pool for a track, joined to publication name. When
    `body_words > 0`, each candidate carries the first `body_words` words of its
    body (D-33); otherwise body is empty and the ranker falls back to subtitle.

    `since_hours > 0` restricts to candidates ingested within that trailing
    window — the daily job ranks only freshly-ingested posts so the pool stays
    bounded (full bodies for the entire accumulated pool would blow past the
    smallest model's context window). See D-38."""
    q = ("SELECT c.id, p.name AS publication, c.author, c.title, c.subtitle, c.full_text "
         "FROM candidates c JOIN publications p ON p.id = c.publication_id "
         "WHERE c.track = ?")
    args: list = [track]
    if since_hours:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
        q += " AND c.ingested_at >= ?"
        args.append(cutoff)
    q += " ORDER BY c.id"
    rows = conn.execute(q, args).fetchall()
    return [
        Candidate(r["id"], r["publication"] or "", r["author"] or "",
                  r["title"] or "", r["subtitle"] or "",
                  body=body_text(r["full_text"], body_words) if body_words else "")
        for r in rows
    ]


# --- Orchestration --------------------------------------------------------

def rank_one_model(client: LLMClient, model: str, system: str, user: str,
                   valid_ids: set[int], pricing: dict,
                   retries: int = 3, sleep=time.sleep) -> ModelResult:
    """Call one model with retries; parse and price the result. A model that
    fails every retry is returned with an error and skipped, never fatal."""
    last_err = None
    for attempt in range(retries):
        try:
            resp = client.complete(model, system, user)
        except Exception as exc:                       # network / provider error
            last_err = str(exc)
            if attempt < retries - 1:
                sleep(2 ** attempt)
            continue
        rankings, no_picks, warnings = parse_ranking(resp.text, valid_ids)
        for w in warnings:
            print(f"  [{model}] warn: {w}")
        return ModelResult(
            model=model, rankings=rankings, no_confident_picks=no_picks,
            raw_text=resp.text, prompt_tokens=resp.prompt_tokens,
            completion_tokens=resp.completion_tokens,
            cost_usd=cost_usd(resp.prompt_tokens, resp.completion_tokens, pricing),
        )
    return ModelResult(model, [], False, "", 0, 0, 0.0, error=last_err)


def _new_run_id(track: str, kind: str, now: datetime) -> str:
    return f"{kind}-{track}-{now.strftime('%Y%m%dT%H%M%S')}"


def store_run(conn, run_id: str, track: str, kind: str, models: list[str],
              prompt_version: str, corpus_version: str, candidates_count: int,
              results: list[ModelResult], now: datetime) -> None:
    """Write the run and every model's predictions in one transaction."""
    now_iso = now.isoformat()
    errors = {r.model: r.error for r in results if r.error}
    with transaction(conn):
        conn.execute(
            "INSERT INTO runs (run_id, run_date, kind, models_json, prompt_version, "
            "corpus_version, candidates_count, errors_json, started_at, finished_at, track) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, now_iso[:10], kind, json.dumps(models), prompt_version,
             corpus_version, candidates_count,
             json.dumps(errors) if errors else None, now_iso, now_iso, track),
        )
        for res in results:
            for rk in res.rankings:
                conn.execute(
                    "INSERT INTO predictions (run_id, run_date, model, prompt_version, "
                    "corpus_version, candidate_id, rank, score, rationale, created_at, track) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, now_iso[:10], res.model, prompt_version, corpus_version,
                     rk.candidate_id, rk.rank, rk.score, rk.rationale, now_iso, track),
                )


def run_ranking(conn, track: str, client: LLMClient, config: dict,
                kind: str = "live", now: Optional[datetime] = None) -> list[ModelResult]:
    """Full ranking run for a track: load candidates → build prompt → call each
    model → store predictions. Returns per-model results (with cost)."""
    now = now or datetime.now(timezone.utc)
    candidates = load_candidates(conn, track, int(config.get("candidate_body_words", 0)),
                                 int(config.get("rank_pool_hours", 0)))
    if not candidates:
        print(f"[rank] no candidates for track={track}; nothing to do")
        return []
    valid_ids = {c.id for c in candidates}
    system, user = build_prompt(track, candidates)
    models = config["ranker_models"]

    results: list[ModelResult] = []
    for m in models:
        res = rank_one_model(client, m["model"], system, user, valid_ids,
                             m.get("pricing", {}))
        results.append(res)
        status = res.error or (f"{len(res.rankings)} picks, ${res.cost_usd:.4f}")
        print(f"[rank] {m['model']}: {status}")

    run_id = _new_run_id(track, kind, now)
    store_run(conn, run_id, track, kind, [m["model"] for m in models],
              config.get("prompt_version", "v1.0"), config.get("corpus_version", "v1.0"),
              len(candidates), results, now)
    print(f"[rank] stored run {run_id}: {len(candidates)} candidates, "
          f"total ${sum(r.cost_usd for r in results):.4f}")
    return results


def estimate_run(conn, track: str, config: dict) -> dict:
    """Free pre-flight: assemble the real prompt, estimate tokens and per-model
    cost WITHOUT calling any model."""
    candidates = load_candidates(conn, track, int(config.get("candidate_body_words", 0)),
                                 int(config.get("rank_pool_hours", 0)))
    system, user = build_prompt(track, candidates)
    p_tok = estimate_tokens(system) + estimate_tokens(user)
    # Assume a full 50-pick JSON response (~60 tokens/pick) for the estimate.
    c_tok = 60 * min(50, len(candidates))
    per_model = []
    for m in config["ranker_models"]:
        per_model.append({
            "model": m["model"],
            "cost_usd": round(cost_usd(p_tok, c_tok, m.get("pricing", {})), 4),
        })
    return {"track": track, "candidates": len(candidates),
            "prompt_tokens_est": p_tok, "completion_tokens_est": c_tok,
            "per_model": per_model,
            "total_usd": round(sum(x["cost_usd"] for x in per_model), 4)}


# --- CLI ------------------------------------------------------------------

def _load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--track", choices=["substack", "nber"], required=True)
    parser.add_argument("--estimate", action="store_true",
                        help="Assemble the prompt and print token/cost estimate; no API calls")
    parser.add_argument("--kind", choices=["live", "backtest"], default="live")
    args = parser.parse_args()

    config = _load_config()
    conn = connect()
    init_schema(conn)

    if args.estimate:
        import pprint
        pprint.pprint(estimate_run(conn, args.track, config))
        return

    client = OpenRouterClient(
        base_url=config.get("openrouter_base_url", "https://openrouter.ai/api/v1"))
    run_ranking(conn, args.track, client, config, kind=args.kind)


if __name__ == "__main__":
    main()
