"""Distilled Markdown export of Tyler Cowen's 'assorted links' posts.

Feedstock for authoring `taste_profile_v1.md` in Claude Fable (or any chat
interface with a decent context window). Deliberately preserves whole posts
rather than atomizing individual links — the taste signal is in how Tyler
juxtaposes items within a roundup, not just which items he picks.

Stratified sampling across years so no era dominates. Reproducible via `--seed`.

Usage:
    python -m src.export_taste_corpus                              # default: 10/year → data/taste_corpus_v1.md
    python -m src.export_taste_corpus --per-year 20 --seed 42
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path
from urllib.parse import urlsplit

from src.db import connect


def _pretty_host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "?"


def sample_posts(conn, per_year: int, seed: int, min_links: int = 2) -> list[dict]:
    """Return one dict per sampled post, with its ordered links attached.

    Only posts with at least `min_links` outbound links are eligible — early
    (2006-2008) assorted-links posts sometimes have almost no links and would
    contribute noise rather than signal to the taste profile.
    """
    rng = random.Random(seed)

    posts = conn.execute("""
        SELECT ap.id, ap.title, ap.published_at, ap.month, ap.url,
               COUNT(al.id) AS n_links
        FROM archive_posts ap
        JOIN archive_links al ON al.archive_post_id = ap.id
        GROUP BY ap.id
        HAVING n_links >= ?
        ORDER BY ap.published_at
    """, (min_links,)).fetchall()

    by_year: dict[str, list] = {}
    for p in posts:
        year = (p["published_at"] or p["month"])[:4]
        by_year.setdefault(year, []).append(p)

    picked_ids: list[int] = []
    for year in sorted(by_year):
        pool = by_year[year]
        n = min(per_year, len(pool))
        picked_ids.extend(p["id"] for p in rng.sample(pool, n))

    # One query per post keeps the code obvious; total sample size is small.
    result = []
    for post_id in picked_ids:
        post = conn.execute(
            "SELECT id, title, published_at, url FROM archive_posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        links = conn.execute(
            "SELECT position, href, anchor_text, surrounding_text "
            "FROM archive_links WHERE archive_post_id = ? ORDER BY position",
            (post_id,),
        ).fetchall()
        result.append({"post": post, "links": [dict(r) for r in links]})
    # Sort chronologically for readability
    result.sort(key=lambda x: x["post"]["published_at"] or "")
    return result


def render(sampled: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# Tyler Cowen — MR \"assorted links\" corpus sample")
    lines.append("")
    lines.append(
        f"{len(sampled)} posts sampled evenly across 2006–2026 from Tyler's "
        f"Marginal Revolution \"assorted links\" archive. Preserves whole posts "
        f"so the juxtaposition of items within each roundup is visible — Tyler's "
        f"taste is expressed by *which items sit next to which*, not just by "
        f"individual picks."
    )
    lines.append("")
    lines.append(
        "For each post: date, title, and Tyler's own ordered list of links, "
        "each rendered as `Tyler's framing sentence → host`."
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    current_year: str | None = None
    for entry in sampled:
        post = entry["post"]
        year = (post["published_at"] or "")[:4]
        if year != current_year:
            lines.append(f"\n## {year}\n")
            current_year = year
        date_short = (post["published_at"] or "")[:10]
        lines.append(f"### {date_short} — {post['title']}")
        lines.append("")
        # Collapse consecutive links that share the same surrounding sentence —
        # Tyler often puts two hosts in one bulleted item ("… and here is
        # a related paper"), and both links come out with identical context
        # but different hosts. Render as one bullet with hosts comma-joined.
        prev_context = None
        prev_hosts: list[str] = []
        for link in entry["links"]:
            context = (link["surrounding_text"] or link["anchor_text"] or "").strip()
            host = _pretty_host(link["href"])
            if context == prev_context:
                if host not in prev_hosts:
                    prev_hosts.append(host)
                continue
            if prev_context is not None:
                lines.append(f"- {prev_context}  →  `{', '.join(prev_hosts)}`")
            prev_context = context
            prev_hosts = [host]
        if prev_context is not None:
            lines.append(f"- {prev_context}  →  `{', '.join(prev_hosts)}`")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/taste_corpus_v1.md",
                        help="Output Markdown path (default: data/taste_corpus_v1.md)")
    parser.add_argument("--per-year", type=int, default=10,
                        help="Posts sampled per year (default: 10 → ~210 posts total)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility (default: 42)")
    parser.add_argument("--min-links", type=int, default=2,
                        help="Skip posts with fewer than this many outbound links")
    args = parser.parse_args()

    conn = connect()
    sampled = sample_posts(conn, per_year=args.per_year, seed=args.seed, min_links=args.min_links)
    conn.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(sampled))
    print(f"Wrote {len(sampled)} posts → {out} ({out.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
