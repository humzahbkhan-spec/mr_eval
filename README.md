# Predicting Tyler

A daily, live evaluation of AI **taste**: which frontier (and open-weight) LLM best predicts what Tyler Cowen links on [Marginal Revolution](https://marginalrevolution.com)?

Every day, several models read a description of Tyler's taste plus that day's fresh candidates and rank what he's most likely to feature. When he actually posts, we score each model's predictions against his real picks. Over weeks this produces a leaderboard of *whose model of a specific expert curator is best* — a measurable proxy for taste, not capability.

**Status:** pipeline is code-complete and running live. First real prediction runs are in; ground truth accrues as Tyler posts. Backtesting is deliberately deferred (see below). Full brief: `predicting-tyler-master-prompt.md`. Every design/editorial call is dated in `DECISIONS.md`; the eval rules live in `METHODOLOGY.md`.

## Two parallel tracks

The eval runs as two independent tracks, scored and reported **separately** (never blended):

- **Substack** — a frozen watchlist of **538 publications** (`corpus_version` v1.0) derived from every Substack Tyler has linked since 2022. Each day's new posts are the candidate pool.
- **NBER** — Tyler links NBER working papers constantly, and NBER is a self-defining corpus (one authoritative feed, no watchlist needed). Each week's new papers are the candidate pool.

Ground truth is **every** Tyler-authored MR post (co-authored with Alex counts), not only the numbered "assorted links" roundups — his dedicated write-ups are the same taste signal.

## The models

All models are called through a single [OpenRouter](https://openrouter.ai) client — one integration, models are config strings (`config.yaml`):

| Model | Tier |
|---|---|
| Claude Fable 5 | frontier |
| Claude Opus 4.8 | frontier |
| GPT-5.6 Sol | frontier |
| Kimi K2.6 | open-weights |

Including a cheap open-weights model turns the question into: *can an open model match the frontier at predicting Tyler's taste?*

## The pipeline

```
ingest.py ──▶ candidates ──▶ rank.py ──▶ predictions ──▶ score.py ──▶ metrics
 (free)                     (LLM calls,              (local, free:
                             the only cost)           matching + recall/MRR)
```

- **`ingest.py`** — source-agnostic; pluggable adapters poll the Substack watchlist and the NBER feed, dedup, and store candidates.
- **`rank.py`** — assembles one prompt (taste profile + per-track few-shot examples + candidates), calls each model, parses defensively, stores predictions with cost. A free `--estimate` mode prices a run without spending.
- **`score.py`** — harvests Tyler's real picks (live from MR's RSS, or historically from the archive), matches them against candidates per track, and computes recall@20/@50 and MRR per model.

Supporting one-offs: `archive_scraper.py` (20-year MR archive), `build_watchlist.py` (derive the Substack corpus), `build_fewshot.py` (per-track few-shot examples from real picks).

### Reproducibility

Every prediction is stamped with `prompt_version` and `corpus_version`; the leaderboard filters on both. Live and backtest runs are stored with `kind` and are **never** aggregated together. URL canonicalization (`normalize.py`) is the highest-risk correctness component — a silent bug there deflates every model's score — so it carries an exhaustive test suite.

### Why no backtest yet

The taste profile and few-shot examples were built from the *full* archive, so any historical replay has look-ahead leakage. The live-forward eval has none — a genuinely future post can't have leaked into a profile written today. Live is the clean measurement; a proper backtest would need a temporal holdout.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env       # add OPENROUTER_API_KEY
```

Set the contact email in the `user_agent` string in `config.yaml` before running any scraper.

## Running

```bash
python -m src.ingest --track nber          # or substack, or omit for both (free)
python -m src.rank   --track nber --estimate   # price a run, no API call
python -m src.rank   --track nber           # the ranking run (costs money)
pytest                                       # test suite
```

## License

MIT. See `LICENSE`.
