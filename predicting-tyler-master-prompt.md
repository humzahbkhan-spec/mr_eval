# PROJECT BRIEF: "Predicting Tyler" — an AI taste evaluation

You are building a research prototype from scratch. Read this entire document before writing any code. This brief is the source of truth; when you face a decision it doesn't cover, follow the Design Principles section and record the decision in `DECISIONS.md`.

## 1. Vision and context

**What this is.** A daily, live evaluation of AI "taste." Tyler Cowen publishes a daily "assorted links" roundup on Marginal Revolution (marginalrevolution.com). Every morning, this system collects all new posts from a fixed watchlist of Substack publications, asks several frontier LLMs to rank which posts Tyler is most likely to link, then checks the AIs' predictions against Tyler's actual picks once he posts. Over weeks, this produces a leaderboard of which models best predict a specific, expert human curator — a measurable proxy for taste.

**Who it's for.** Built by Humzah (technically capable, not a professional software engineer — code must be readable and well-commented so he can maintain it). The project is developed in collaboration with Andy Hall (Stanford GSB), who may fund its ongoing operation and may involve students or collaborators. Therefore:

- The repo must be **shareable on GitHub from day one**: clean structure, README that lets a stranger run it, no secrets in code (API keys via environment variables / GitHub Actions secrets, with `.env.example` provided), MIT license.
- The methodology must be **credible as research**: every prediction reproducible, every scoring rule written down, all versioning explicit. A skeptical academic should be able to audit any number on the leaderboard back to raw data.
- Costs matter but are not the binding constraint; **data hygiene is**. When hygiene and convenience conflict, choose hygiene.

**The one-sentence quality bar:** in 30 days, we should be able to publish a post saying "Model X placed Tyler's actual picks in its top 20 on Y% of opportunities, under frozen corpus v1 and prompt v1" and defend every word of that sentence.

## 2. Design principles (apply when the brief is silent)

1. **Boring technology.** Python 3.11+, SQLite, `feedparser`, `httpx`, `pydantic` for schemas, Streamlit for the dashboard, GitHub Actions for scheduling. No frameworks, no Docker, no microservices, no queues. This is a batch pipeline that runs twice a day.
2. **Everything is versioned and logged.** Corpus, taste prompt, scoring rules, and code all carry version identifiers stored alongside every prediction. Raw inputs (feed XML, LLM responses, scraped HTML) are preserved verbatim before any parsing.
3. **Fail loudly in logs, never fatally in runs.** One broken feed or one malformed LLM response must never kill a daily run. Log, skip, continue, and surface a summary of failures at the end of each run.
4. **Re-runnable and idempotent.** Every script can be re-run for the same date without duplicating rows (use upserts keyed on natural keys). Assume runs will occasionally fail halfway.
5. **The human owns editorial judgment.** The taste profile, the watchlist, and the scoring thresholds are Humzah's decisions. Build the machinery; where an editorial choice is needed, implement a sensible default, flag it clearly in `DECISIONS.md`, and make it a config value, not a hardcode.
6. **Ask before spending.** Never run anything that makes paid API calls at scale (backtests, multi-model runs) without explicitly telling Humzah the estimated cost and getting confirmation. Single-call smoke tests are fine.

## 3. Repository structure

```
predicting-tyler/
├── README.md              # setup, architecture overview, how to run each script
├── METHODOLOGY.md         # the eval rules: corpus version, scoring tiers, matching window
├── DECISIONS.md           # dated log of design decisions and their rationale
├── LICENSE                # MIT
├── .env.example           # ANTHROPIC_API_KEY=, OPENAI_API_KEY=, GOOGLE_API_KEY=
├── pyproject.toml         # deps pinned
├── config.yaml            # models to run, matching window days, ranker settings
├── prompts/
│   ├── taste_profile_v1.md    # provided separately by Humzah — do not write this yourself
│   └── ranker_instructions_v1.md
├── data/
│   ├── tyler.db               # SQLite database (committed after each run; it will stay small)
│   └── raw/                   # raw feed XML, LLM responses, scraped MR HTML, by date
├── src/
│   ├── archive_scraper.py     # one-time: harvest historical MR assorted-links posts
│   ├── build_watchlist.py     # one-time: derive Substack watchlist from archive
│   ├── ingest.py              # daily: poll feeds, store new candidates
│   ├── rank.py                # daily: build prompt, call models, store predictions
│   ├── ground_truth.py        # daily: scrape MR, extract links, normalize, match, score
│   ├── normalize.py           # URL canonicalization (shared, heavily tested)
│   ├── db.py                  # schema + connection helpers
│   └── backtest.py            # replay historical days through the pipeline
├── dashboard/
│   └── app.py                 # Streamlit leaderboard
├── tests/
│   └── test_normalize.py      # URL normalization has real unit tests; rest can be lighter
└── .github/workflows/
    ├── morning.yml            # ingest + rank, 10:00 UTC daily
    └── scoring.yml            # ground truth check, 16:30 UTC + retry 18:00 UTC
```

## 4. Database schema

SQLite, but write portable SQL (no SQLite-only tricks) — this may migrate to Postgres. Tables:

- **publications**: id, name, feed_url, canonical_domain, added_date, source ("archive_derived" | "manual"), corpus_version, active (bool).
- **candidates**: id, publication_id, url, canonical_url (via normalize.py), title, subtitle, author, published_at, ingested_at, full_text (nullable), raw_entry_json. Unique on canonical_url.
- **predictions**: id, run_id, run_date, model (exact API model string), prompt_version, corpus_version, candidate_id, rank, score (0–100), rationale, created_at. One row per (run, model, ranked candidate).
- **ground_truth**: id, mr_post_url, mr_post_date, link_position, raw_url, canonical_url, is_substack (bool), matched_candidate_id (nullable), match_type ("exact" | "same_publication" | "near_miss" | "unmatched" | "out_of_corpus"), match_lag_days, scored_at.
- **runs**: run_id, run_date, kind ("live" | "backtest"), models_json, prompt_version, corpus_version, candidates_count, errors_json, started_at, finished_at.

Key point: `prompt_version` and `corpus_version` on every prediction row are non-negotiable. The leaderboard must be filterable by both.

## 5. Component specifications and key decisions

### 5.1 Archive scraper (build first)
Harvest all historical MR "assorted links" posts. MR is WordPress with predictable archives and a full RSS history is not available, so crawl the monthly archive pages (marginalrevolution.com/marginalrevolution/YYYY/MM) and filter posts whose titles match `/assorted links/i`. Be polite: 1 request/second, identify with a custom User-Agent naming the project and a contact. Store each post's HTML in `data/raw/` and extract: date, title, and the ordered list of outbound links in the post body (anchor href + anchor text + surrounding sentence for context). Expect ~4,500+ posts, 2003–present. This is a one-time job; make it resumable (track which months are done).

### 5.2 Watchlist builder
From the archive links, identify Substack publications: (a) any `*.substack.com` host; (b) `open.substack.com/pub/{name}` links → resolve to the publication; (c) custom domains — detect by fetching `https://{domain}/feed` and checking the generator tag / Substack fingerprints in the response. Output the publications table. **Decision made for you:** corpus v1 = every Substack Tyler linked ≥1 time since 2022-01-01, plus a `manual_additions.txt` file Humzah can edit (each addition logged with date). Report the count; if it's under 100 or over 600, flag it and discuss before proceeding.

### 5.3 Daily ingestion
Poll each active publication's `/feed`. New entries (published within trailing 48h, not already in candidates) get inserted. Save each raw feed response to `data/raw/{date}/feeds/`. Handle: feeds that 404 (mark publication inactive after 7 consecutive failures, log it), entries with no subtitle, truncated content, and Substack podcast/video posts (ingest them; the ranker can see the title). Emit an end-of-run summary: feeds polled, failures, new candidates.

### 5.4 URL normalization (`normalize.py`) — highest-risk correctness component
Canonical form: lowercase scheme+host, strip `utm_*`/`ref`/`source` and all tracking params, strip trailing slashes and anchors, resolve `open.substack.com/pub/{pub}/p/{slug}` → `https://{pub}.substack.com/p/{slug}`, follow redirects (with caching) for shortened/redirected URLs, treat custom-domain and substack.com forms of the same publication as identical where discoverable. Write thorough unit tests with real-world messy examples before wiring it into anything. A false non-match here silently deflates every model's score — this is the module where bugs corrupt the research.

### 5.5 Ranking engine
Once daily after ingestion. For each model in `config.yaml` (start with: one Anthropic model, one OpenAI model, one Google model — exact strings in config):
- Build the prompt: (1) `taste_profile_v1.md`, (2) few-shot examples file (generated from archive: 75 real linked items with dates, formatted `title — publication — one-line summary`), (3) today's candidates as `[id] | publication | author | title | subtitle`, (4) `ranker_instructions_v1.md` asking for JSON: an array of `{candidate_id, rank, score_0_100, rationale_one_line}` covering the **top 50** (not 20 — we need depth for rank metrics), plus a `no_confident_picks: true` escape hatch.
- Order the prompt with all static content first to exploit provider prompt caching.
- Use structured output / JSON mode where the provider supports it; otherwise parse defensively and store the raw response regardless.
- Validate: every returned candidate_id must exist in today's pool; drop and log hallucinated IDs.
- Retries: 3 with exponential backoff; a model that fails all retries is logged and skipped, not fatal.
- Print estimated token counts and cost per run in the summary.

### 5.6 Ground truth and scoring
Poll MR's RSS (marginalrevolution.com/feed) around the scheduled times; detect posts titled `*assorted links*`. For each numbered link: normalize, then match against candidates from the trailing **4 days** (config value). Match types, in precedence order: `exact` (canonical URLs equal) → `same_publication` (same publication, different post) → `near_miss` (reserved for later embedding similarity; leave stubbed with a clear TODO) → `unmatched` (a Substack link we didn't have) → `out_of_corpus` (non-Substack link; still record it — the Substack share of Tyler's links is itself a headline statistic). For matched links, record where each model ranked that candidate (join against predictions across the lag window).

### 5.7 Metrics and dashboard
Compute per model, filterable by prompt/corpus version: recall@20 and recall@50 over Substack-matchable links (rolling 7/30-day), mean reciprocal rank of matched links, and a calibration view (predicted score vs. hit rate by score decile). Also show: today's top-20 per model with rationales, the running hit log with match types, and corpus health (active feeds, failures). Keep the dashboard read-only against the DB.

### 5.8 Backtesting
`backtest.py` replays a date range: reconstruct that day's candidate pool from already-ingested archive data where possible (Substack archive pages list historical posts with dates — scrape trailing history for watchlist publications once, politely, and store as ordinary candidates flagged by ingestion source), run the ranker with `kind="backtest"`, score against known ground truth. **Backtest predictions must never mix into live leaderboards** — always filter on `runs.kind`. Before running any multi-day backtest, print the estimated API cost and wait for confirmation.

## 6. Build order (follow strictly)

1. Repo scaffolding, schema, `normalize.py` + its tests.
2. Archive scraper → run it (no API costs, just time; be polite).
3. Watchlist builder → present the resulting list and stats to Humzah for editorial review before freezing corpus v1.
4. Ground-truth extractor → validate against the last 30 days of real MR posts (no LLM calls needed). This tests link extraction and normalization against reality early.
5. Daily ingestion → run live for 2–3 days while building the rest; verify candidate volumes look sane (expect roughly 100–500/day).
6. Few-shot example generator + ranking engine → single-model smoke test on one real day; review output quality with Humzah.
7. Scoring/matching join, metrics, dashboard.
8. GitHub Actions workflows; run end-to-end for a few days supervised.
9. Backtest harness last, once the live pipeline is trusted.

## 7. Things NOT to do

- Do not write or modify the taste profile content — that is Humzah's editorial document.
- Do not add publications to the corpus outside the defined derivation + manual file.
- Do not scrape X/Twitter, paywalled content, or anything requiring authentication.
- Do not introduce a hosted database, container setup, or web framework — resist the urge to "productionize."
- Do not let any secret touch the repo (pre-commit check for key patterns is worth adding).
- Do not silently change scoring rules, matching windows, or prompt text; any such change bumps a version and gets a `DECISIONS.md` entry.
- Do not aggregate live and backtest results anywhere.

## 8. Definition of done for the prototype

- One command (`make daily` or equivalent) runs ingest+rank; another runs scoring; both also run unattended on GitHub Actions.
- A stranger can clone the repo, add API keys, and reproduce a day's run from the README.
- METHODOLOGY.md fully specifies the eval such that the leaderboard numbers are defensible.
- 30 days of historical ground truth loaded; at least one validated live day end-to-end with 3 models.
- Streamlit dashboard renders leaderboard, daily picks, and hit log from the real DB.

Begin with step 1 of the build order. Before writing code, restate your understanding of the system in a few sentences and list any questions where this brief is ambiguous.
