# Ranker output instructions — v1.0

You will be given (1) a taste profile describing what a specific expert curator likes, (2) a set of few-shot examples of prior selections by that curator, and (3) today's candidate posts — one line per post in the format `[candidate_id] | publication | author | title | subtitle`. Your task is to rank the candidates.

## Output format

Return **only** a single JSON object with this exact shape:

```json
{
  "no_confident_picks": false,
  "picks": [
    {"candidate_id": 41, "rank": 1, "score_0_100": 87, "rationale": "One-line reason grounded in the taste profile."},
    {"candidate_id": 12, "rank": 2, "score_0_100": 81, "rationale": "…"}
  ]
}
```

- **Depth: exactly 50 items** (or all candidates, if fewer than 50 exist). Ranking depth matters — the eval scores recall at multiple K, so we need to see where the eventually-linked post landed even if it wasn't your top pick.
- **`rank`** is 1-indexed, strictly increasing, one per position. No ties.
- **`score_0_100`** is your calibrated confidence that the curator will link this post today. Reserve high scores; on typical days most items should score in the 20-60 range, not 80+.
- **`rationale`** is one sentence citing which properties from the taste profile this post embodies. Not marketing copy for the post.
- **`candidate_id`** must exactly match an ID from the input pool. IDs not in the pool are hallucinations and will be dropped and logged.

## The escape hatch: `no_confident_picks: true`

If, after honest consideration, no candidate clears the taste profile's stated bar for what makes a post link-worthy, set `"no_confident_picks": true` and return `"picks": []`. This is often the correct answer — the curator we're predicting frequently produces roundups where zero Substack posts appear. A ranker that confidently promotes a merely-good post every day is *miscalibrated*, not helpful.

Do not use the escape hatch just because you find ranking difficult. Use it only when the honest answer is "nothing here clears the bar."

## Nothing else in the output

No preamble ("Here is the ranking:"), no trailing prose, no code fences, no markdown. Just the JSON object. Downstream parsing is strict.

---

*This file specifies the ranker's output contract. Content of the taste (what to weight, what to reject) lives in `taste_profile_v1.md`. Changes here bump `prompt_version`; see METHODOLOGY.md and DECISIONS.md.*
