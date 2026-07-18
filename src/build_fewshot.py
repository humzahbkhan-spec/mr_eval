"""Generate per-track few-shot example files from Tyler's real archived picks.

The few-shot examples show the ranker concrete instances of what Tyler linked,
in *his own words* — the anchor text and surrounding gloss he wrote when linking
each item are a more authentic, more reproducible taste signal than any
LLM-written summary (and cost nothing). This is a deterministic data-selection
job, NOT a synthesis task: `taste_profile_v1.md` already carries the interpreted
taste; the few-shot set is the independent factual anchor beside it.

Selection (D-04, resolved 2026-07-18 — deterministic quality-filter + stratified):
  - Quality filter: real gloss present, non-trivial anchor, leading list numbers
    ("3. ") stripped, gloss not identical to the anchor.
  - Stratified across calendar years so examples span Tyler's durable range and
    don't over-index recent picks.
  - Seeded, so the exact set is reproducible.

One set per track (D-28 composition): the Substack ranker gets Substack picks,
the NBER ranker gets NBER picks — each calibrated to the candidate type it ranks.

Usage:
    python -m src.build_fewshot --track all --n 75 --seed 42
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

from src.db import connect
from src.normalize import canonicalize
from src.score import classify_track, load_substack_domains

PROMPTS_DIR = Path("prompts")
_LEADING_NUM = re.compile(r"^\s*\d+\.\s*")     # "3. Foo" -> "Foo"
_WS = re.compile(r"\s+")

MIN_ANCHOR = 8
MIN_GLOSS = 15
MAX_GLOSS = 240                       # keep the "one-line" spirit; truncate politely
# Substack infrastructure subdomains (email/click tracking), not publications.
_INTERNAL_SUBSTACK = {"email", "click", "link", "mg1", "mg2", "mg3", "mg4"}


@dataclass
class Example:
    publication: str
    anchor: str
    gloss: str
    date: str          # YYYY-MM-DD
    year: str


def clean_gloss(text: str | None) -> str:
    """Strip the leading MR list number, normalize whitespace, and cap length at
    a word boundary so a multi-sentence gloss stays a one-liner."""
    if not text:
        return ""
    g = _WS.sub(" ", _LEADING_NUM.sub("", text.strip())).strip()
    if len(g) > MAX_GLOSS:
        g = g[:MAX_GLOSS].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
    return g


def _is_internal_substack(host: str) -> bool:
    """True for Substack email/click-tracking subdomains, not real publications."""
    return host.endswith(".substack.com") and host.split(".")[0] in _INTERNAL_SUBSTACK


def is_quality(anchor: str | None, gloss: str) -> bool:
    """Keep only examples with a substantive anchor and an informative gloss."""
    a = (anchor or "").strip()
    return len(a) >= MIN_ANCHOR and len(gloss) >= MIN_GLOSS and gloss.lower() != a.lower()


def _publication_names(conn) -> dict[str, str]:
    return {
        r["canonical_domain"]: r["name"]
        for r in conn.execute(
            "SELECT canonical_domain, name FROM publications WHERE track='substack'"
        ).fetchall()
    }


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    return (urlsplit(url).hostname or "").lower()


def _candidates(conn, track: str, start_month: str) -> list[Example]:
    """All quality-passing archived picks for a track, as Examples."""
    substack_domains = load_substack_domains(conn)
    pub_names = _publication_names(conn)
    rows = conn.execute(
        "SELECT l.anchor_text a, l.surrounding_text s, l.canonical_href h, "
        "       p.published_at d "
        "FROM archive_links l JOIN archive_posts p ON p.id = l.archive_post_id "
        "WHERE p.author LIKE '%Tyler%' AND p.month >= ?",
        (start_month,),
    ).fetchall()
    out: list[Example] = []
    for r in rows:
        canon = canonicalize(r["h"])
        if classify_track(canon, substack_domains) != track:
            continue
        host = _host(canon)
        if track == "substack" and _is_internal_substack(host):
            continue                      # skip email/click-tracking subdomains
        gloss = clean_gloss(r["s"])
        if not is_quality(r["a"], gloss):
            continue
        date = (r["d"] or "")[:10]
        if not date:
            continue
        if track == "nber":
            publication = "NBER"
        else:
            publication = pub_names.get(host, host)
        out.append(Example(publication, r["a"].strip(), gloss, date, date[:4]))
    # Deterministic order so seeded sampling is reproducible.
    out.sort(key=lambda e: (e.date, e.anchor, e.gloss))
    return out


def stratified_sample(examples: list[Example], n: int, seed: int) -> list[Example]:
    """Sample ~n examples evenly across calendar years, seeded/reproducible."""
    import random
    rng = random.Random(seed)
    by_year: dict[str, list[Example]] = {}
    for e in examples:
        by_year.setdefault(e.year, []).append(e)
    years = sorted(by_year)
    base, rem = divmod(n, len(years))
    picked: list[Example] = []
    # Years with the most availability absorb the remainder, deterministically.
    remainder_years = sorted(years, key=lambda y: (-len(by_year[y]), y))[:rem]
    for y in years:
        want = base + (1 if y in remainder_years else 0)
        pool = sorted(by_year[y], key=lambda e: (e.date, e.anchor))
        picked.extend(rng.sample(pool, min(want, len(pool))))
    picked.sort(key=lambda e: (e.date, e.anchor))
    return picked


def format_example(e: Example) -> str:
    return f'- {e.publication} ({e.date}): {e.gloss}'


def render_file(track: str, examples: list[Example], seed: int) -> str:
    header = (
        f"# Few-shot examples — {track} track (v1)\n\n"
        f"{len(examples)} real items Tyler Cowen linked on Marginal Revolution, in "
        "his own words (anchor + surrounding gloss from the archive). Deterministic "
        f"quality-filtered, stratified-by-year sample; seed={seed}. Regenerate with "
        "`python -m src.build_fewshot`. See DECISIONS.md D-04/D-30.\n\n"
    )
    return header + "\n".join(format_example(e) for e in examples) + "\n"


def build(conn, track: str, n: int, seed: int,
          start_month: str = "2022-01") -> list[Example]:
    cands = _candidates(conn, track, start_month)
    return stratified_sample(cands, n, seed)


def _write(track: str, examples: list[Example], seed: int) -> Path:
    PROMPTS_DIR.mkdir(exist_ok=True)
    path = PROMPTS_DIR / f"few_shot_{track}_v1.md"
    path.write_text(render_file(track, examples, seed))
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--track", choices=["substack", "nber", "all"], default="all")
    parser.add_argument("--n", type=int, default=75)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    conn = connect()
    tracks = ["substack", "nber"] if args.track == "all" else [args.track]
    for track in tracks:
        examples = build(conn, track, args.n, args.seed)
        path = _write(track, examples, args.seed)
        years = sorted({e.year for e in examples})
        print(f"[{track}] wrote {len(examples)} examples to {path} "
              f"(years {years[0]}–{years[-1]})")


if __name__ == "__main__":
    main()
