"""Daily pipeline — the single entrypoint the scheduler runs each day.

Chains the steps we otherwise run by hand, in order and idempotently:

  1. ingest    — poll the Substack watchlist + NBER feed for new posts (free)
  2. rank      — each model ranks the freshly-ingested pool (PAID; OpenRouter)
  3. harvest   — pull Tyler's actual recent links from MR's RSS (free)
  4. match     — link his picks to our candidates and score them (free)
  5. prune     — null full_text past its window so the DB stays small (free)

The ranker is the only step that costs money and the only one needing
`OPENROUTER_API_KEY`. Everything writes to `data/tyler.db`.

    python -m src.daily              # full run (ranks — costs ~$2-3/day)
    python -m src.daily --no-rank    # everything except the paid ranking
"""

from __future__ import annotations

import argparse

import httpx

from src.db import connect, init_schema
from src.ingest import run as ingest_run
from src.rank import OpenRouterClient, _load_config, run_ranking
from src.score import harvest_from_rss, match_unscored, prune_full_text

MR_FEED = "https://marginalrevolution.com/feed"
TRACKS = ("substack", "nber")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-rank", action="store_true",
                        help="run everything except the paid ranking step")
    args = parser.parse_args()

    config = _load_config()
    ua = config.get("user_agent", "PredictingTylerBot/0.1")

    # 1. Ingest (free) — both tracks, into data/tyler.db
    print("[daily] 1/5 ingest")
    ingest_run(track="all", config=config)

    conn = connect()
    init_schema(conn)

    # 2. Rank (paid) — each track's freshly-ingested pool
    if args.no_rank:
        print("[daily] 2/5 rank — SKIPPED (--no-rank)")
    else:
        print("[daily] 2/5 rank")
        client = OpenRouterClient(
            base_url=config.get("openrouter_base_url", "https://openrouter.ai/api/v1"))
        for track in TRACKS:
            run_ranking(conn, track, client, config, kind="live")

    # 3. Harvest Tyler's actual recent links from MR (free)
    print("[daily] 3/5 harvest MR feed")
    xml = httpx.get(MR_FEED, headers={"User-Agent": ua},
                    timeout=30.0, follow_redirects=True).content
    hs = harvest_from_rss(conn, xml)
    print(f"       harvested {hs.posts} Tyler posts, {hs.links} links "
          f"({hs.by_track})")

    # 4. Match his picks against candidates + score (free)
    print("[daily] 4/5 match")
    ms = match_unscored(conn)
    print(f"       exact={ms.exact} same_publication={ms.same_publication} "
          f"still_unmatched={ms.still_unmatched}")

    # 5. Prune bodies past their window (free)
    print("[daily] 5/5 prune")
    pruned = prune_full_text(conn)
    print(f"       nulled full_text on {pruned} past-window candidates")

    print("[daily] done")


if __name__ == "__main__":
    main()
