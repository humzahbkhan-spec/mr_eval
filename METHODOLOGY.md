# Methodology

This document specifies the evaluation such that leaderboard numbers are defensible. When a rule below changes, the corresponding version tag (`prompt_version` or `corpus_version`) increments and a `DECISIONS.md` entry is added.

## Versioning

### Corpus version

The corpus is the set of active publications whose feeds we ingest. Every prediction row stamps the `corpus_version` in effect at prediction time.

Bumps `corpus_version`:
- A publication is added, whether via a re-run of the archive-derived list or via `manual_additions.txt`.
- A publication is intentionally removed (editorial decision).

Does **not** bump `corpus_version`:
- Automatic deactivation of a feed after `inactive_after_consecutive_failures` fetch failures. The corpus is unchanged; the feed is just temporarily unreachable.
- Reactivation of a previously-dead feed.

Corpus v1 is defined by DECISIONS.md and is frozen as of the date recorded there.

### Prompt version

`prompt_version` covers `prompts/taste_profile_v1.md`, `prompts/ranker_instructions_v1.md`, and the few-shot example set. Any content change to any of these bumps the version.

## Ranker output

Each ranker call returns a top-K list (default K = 50) of `{candidate_id, rank, score_0_100, rationale_one_line}`. Both rank and score are preserved — not collapsed to binary — so we can compute rank-sensitive metrics (recall@K at multiple K, MRR). If a run finds no confident picks the response may set `no_confident_picks: true` instead of a list.

## Ground-truth scope

Ground truth is **every Tyler-authored MR post** (co-authored with Alex counts), not only the numbered "assorted links" roundups (D-19; Humzah 2026-07-18). Tyler's dedicated single-post writeups ("Robin Hanson on X") are the same taste signal, and scoring all of them as one pool roughly doubles the ground truth (2,197 scoreable picks vs. 1,345 assorted-links-only over the archive). Live harvesting keys on the post's `dc:creator` being Tyler, not on the title.

## Matching

For every link in a Tyler post, we search our candidate pool from the trailing `matching_window_days` days. The window is **per track**: Substack 4 days, NBER 14 days (`nber.matching_window_days`, calibrated in D-27).

Match types, in precedence order:

1. **`exact`** — canonical URLs identical after `normalize.canonicalize()`. This subsumes same-post-different-URL-shape cases (`open.substack.com/pub/foo/p/bar` ≡ `foo.substack.com/p/bar` ≡ custom-domain equivalent), which are handled inside canonicalization or via the publication alias map.
2. **`same_publication`** — same publication, different post. Recorded; not counted as a hit for recall.
3. **`content_match`** — same underlying article via a different route (e.g., Tyler links Substack A's coverage of a paper, we ingested Substack B's coverage of the same paper). Detected via embedding similarity on `title + subtitle`. **Not yet implemented.** Until it is, some genuine matches will fall to `unmatched` and depress strict recall.
4. **`unmatched`** — an in-track (Substack or NBER) link we didn't have as a candidate.
5. **`out_of_corpus`** — a link belonging to no eval track (nytimes, twitter, …). Recorded because the Substack/NBER share of Tyler's links is itself a headline stat.
6. **`out_of_scope`** — an NBER paper released more than the NBER window before the link (classic-paper resurfacing, D-27). Counted and logged but **excluded from the NBER recall denominator** — not scored as a miss.

Matching runs as a separate pass (`score.match_unscored`) after candidates and predictions exist, so ground truth can be harvested before the ranker runs.

## Cross-day rank aggregation

A candidate may be ranked on multiple days before Tyler picks it (e.g., ranked Mon and Tue, linked Wed). When computing a metric, the model's effective rank for that candidate is the **best (lowest) rank across the trailing matching window** (`cross_day_rank_aggregation: best` in config).

Rationale: rewards the model for ever having believed in the pick. Alternate rules (`latest`, `mean`) are options if we later disagree with this default; the raw per-day ranks are always retained in the DB so metrics can be recomputed.

## Metrics

Per model, filterable by `(prompt_version, corpus_version)`:

- **Recall@20** and **Recall@50** over Substack-matchable links, rolling 7-day and 30-day.
- **Mean Reciprocal Rank (MRR)** of matched links (using the aggregated rank).
- **Calibration**: predicted score decile vs. observed hit rate.

Recall is reported in two variants side-by-side:
- **Strict recall**: `exact` matches only.
- **Generous recall**: `exact` + `content_match`.

Until `content_match` is implemented, strict == generous. This is flagged on the dashboard.

## Live / backtest separation

`runs.kind` is one of `live` or `backtest`. The leaderboard **never** aggregates them. Every leaderboard query filters `WHERE runs.kind = 'live'`. Backtest predictions are shown in a separate view.

## Opportunity accounting

The unit of opportunity is one Tyler-linked Substack post that was already in our candidate pool at ranking time. A post first ingested *after* Tyler linked it is excluded from recall (not counted as a miss). A post Tyler doesn't link is not an opportunity for any model.

## NBER paper track

A second, **parallel** eval track predicts which NBER working papers Tyler will link (DECISIONS.md D-24). It is scored and reported entirely separately from the Substack track — the two **never** blend into one leaderboard. Every candidate/prediction/run carries a `track` (`substack` | `nber`) for this reason.

NBER is a self-defining corpus: one authoritative feed (`back.nber.org/rss/new.xml`), no watchlist to derive. The candidate pool each period is simply the new working papers; the model decides which Tyler will feature.

- **Matching window: 14 days** (`nber.matching_window_days`). Calibrated from 511 real link events (D-27): median link-lag is 1 day, p90 is 6, p95 is 10; a 14-day window captures 97.1% of picks and the distribution is flat beyond it. (Substack's window stays at 4.)
- **Out-of-scope "classic papers."** A pick whose paper was released more than `nber.classic_paper_after_days` (14) before the link is Tyler resurfacing an old paper (e.g. a 1998 working paper), not a fresh-pick prediction. These are classified `out_of_scope`, **counted and logged but excluded from the NBER recall denominator** — they are not scored as misses. They are ~2–3% of NBER links and no practical window catches them.
- **Polling cadence: daily.** Median link-lag is one day, so weekly polling would routinely ingest a paper after Tyler already linked it, making the prediction impossible.
- **Release dates** come from each paper page's `citation_publication_date` meta tag, stored in `nber_paper_dates`. Used to compute lag and the fresh/out-of-scope split.
