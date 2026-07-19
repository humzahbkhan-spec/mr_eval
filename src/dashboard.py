"""Predicting Tyler — read-only dashboard over tyler.db.

Editorial aesthetic (warm paper, oxblood accent, serif masthead), built from
Streamlit theming + streamlit-shadcn-ui components + a little CSS to strip the
default chrome — deliberately not default-Streamlit. Read-only against the DB.

Run:  streamlit run src/dashboard.py
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import streamlit as st
import streamlit_shadcn_ui as ui

DB = Path("data/tyler.db")
ACCENT = "#8C3A2B"
INK = "#211C17"
MUTED = "#6B6255"
RULE = "#DED6C6"

TRACKS = {"Substack": "substack", "NBER papers": "nber"}
MODEL_NAMES = {"anthropic/claude-opus-4.8": "Claude Opus 4.8",
               "openai/gpt-5.6-sol": "GPT-5.6 Sol",
               "moonshotai/kimi-k2.6": "Kimi K2.6"}


# --- data -----------------------------------------------------------------

def _secret(key: str, default: str = "") -> str:
    import os
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.environ.get(key, default)


@st.cache_resource
def ensure_db():
    """Fetch tyler.db from the GitHub Release asset when there's no local copy.

    Locally the DB is present (dev) and this is a no-op. On Streamlit Cloud the
    DB is gitignored, so we download the rolling `data-latest` release asset that
    the daily job publishes. A private repo needs GH_TOKEN as a Streamlit secret;
    a public repo works without one.
    """
    if DB.exists():
        return
    import httpx
    repo = _secret("GH_REPO", "humzahbkhan-spec/mr_eval")
    tag = _secret("DB_RELEASE_TAG", "data-latest")
    token = _secret("GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with st.spinner("Loading the latest data…"):
        rel = httpx.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
                        headers=headers, timeout=30.0).json()
        asset = next((a for a in rel.get("assets", []) if a["name"] == "tyler.db"), None)
        if not asset:
            st.error("No data release found yet — the daily job hasn't published one.")
            st.stop()
        blob = httpx.get(asset["url"], follow_redirects=True, timeout=180.0,
                         headers={**headers, "Accept": "application/octet-stream"})
        DB.parent.mkdir(parents=True, exist_ok=True)
        DB.write_bytes(blob.content)


@st.cache_resource
def _conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def latest_run(track: str, prompt_version: str | None = None):
    q = "SELECT * FROM runs WHERE track = ? AND kind = 'live'"
    args = [track]
    if prompt_version:
        q += " AND prompt_version = ?"
        args.append(prompt_version)
    q += " ORDER BY started_at DESC LIMIT 1"
    return _conn().execute(q, args).fetchone()


def picks(run_id: str, model: str, limit: int = 6):
    return _conn().execute(
        "SELECT p.rank, p.score, p.rationale, c.title, pub.name AS publication "
        "FROM predictions p JOIN candidates c ON c.id = p.candidate_id "
        "JOIN publications pub ON pub.id = c.publication_id "
        "WHERE p.run_id = ? AND p.model = ? ORDER BY p.rank LIMIT ?",
        (run_id, model, limit),
    ).fetchall()


def models_in(run_id: str):
    return [r[0] for r in _conn().execute(
        "SELECT DISTINCT model FROM predictions WHERE run_id = ? ORDER BY model", (run_id,))]


def launch_date(track: str):
    """Day 1 for this track = when we first ingested candidates for it."""
    return _conn().execute(
        "SELECT MIN(substr(ingested_at, 1, 10)) FROM candidates WHERE track = ?",
        (track,)).fetchone()[0]


def tyler_links_since(track: str, since: str):
    """Tyler's actual in-track links on/after `since`, with match status."""
    if not since:
        return []
    return _conn().execute(
        "SELECT mr_post_date, canonical_url, match_type, matched_candidate_id "
        "FROM ground_truth WHERE track = ? AND mr_post_date >= ? "
        "AND match_type != 'out_of_scope' ORDER BY mr_post_date DESC",
        (track, since),
    ).fetchall()


def candidate_best_ranks(candidate_id: int):
    return _conn().execute(
        "SELECT model, MIN(rank) AS r FROM predictions WHERE candidate_id = ? GROUP BY model",
        (candidate_id,),
    ).fetchall()


def latest_data_date():
    return _conn().execute(
        "SELECT MAX(run_date) FROM runs WHERE kind = 'live'").fetchone()[0]


def corpus_domains() -> set[str]:
    return {r[0] for r in _conn().execute("SELECT canonical_domain FROM publications")}


def corpus_health(track: str):
    c = _conn()
    pubs = c.execute("SELECT COUNT(*) FROM publications WHERE track='substack' AND active=1").fetchone()[0]
    cands = c.execute("SELECT COUNT(*) FROM candidates WHERE track=?", (track,)).fetchone()[0]
    hits = c.execute("SELECT COUNT(*) FROM ground_truth WHERE track=? AND match_type IN ('exact','content_match')",
                     (track,)).fetchone()[0]
    return {"publications": pubs, "candidates": cands, "hits": hits}


def short(model: str) -> str:
    return MODEL_NAMES.get(model, model.split("/")[-1])


def url_label(u: str) -> tuple[str, str]:
    p = urlsplit(u)
    host = (p.hostname or "").replace("www.", "")
    slug = (p.path.strip("/").split("/")[-1] or "").replace("-", " ")
    return host, slug


def pretty_date(iso: str | None) -> str:
    try:
        return date.fromisoformat(iso).strftime("%B %-d, %Y")
    except (TypeError, ValueError):
        return iso or "—"


# --- chrome + styling -----------------------------------------------------

st.set_page_config(page_title="Predicting Tyler", page_icon="📖",
                   layout="wide", initial_sidebar_state="collapsed")

ensure_db()   # no-op locally; downloads the release asset on Streamlit Cloud

st.markdown(f"""
<style>
  #MainMenu, footer, [data-testid="stToolbar"], [data-testid="stDecoration"],
  [data-testid="stStatusWidget"] {{ display: none !important; }}
  .block-container {{ max-width: 1180px; padding-top: 4rem; }}
  a {{ color: {ACCENT}; }}
  .masthead {{ border-bottom: 2px solid {INK}; padding-bottom: .7rem; margin-bottom: .3rem; }}
  .masthead h1 {{ font-family: Georgia, 'Times New Roman', serif; font-weight: 700;
    font-size: 3.1rem; letter-spacing: -.02em; margin: .1rem 0 0; color: {INK}; }}
  .kicker {{ font-family: ui-monospace, 'SF Mono', Menlo, monospace; text-transform: uppercase;
    letter-spacing: .22em; font-size: .68rem; color: {ACCENT}; }}
  .dek {{ color: {MUTED}; font-size: 1.02rem; margin-top: .5rem; max-width: 62ch; }}
  .more {{ margin-top: .55rem; margin-bottom: 1.6rem; font-size: .82rem; }}
  .more a {{ font-family: ui-monospace, monospace; letter-spacing: .04em; }}
  .section-rule {{ font-family: ui-monospace, monospace; text-transform: uppercase;
    letter-spacing: .18em; font-size: .72rem; color: {MUTED}; border-top: 1px solid {RULE};
    padding-top: .5rem; margin: 1.7rem 0 .5rem; }}
  .model-h {{ font-family: Georgia, serif; font-weight: 700; font-size: 1.12rem; color: {INK};
    border-bottom: 2px solid {ACCENT}; padding-bottom: .25rem; margin-bottom: .5rem; }}
  .pick {{ border: 1px solid {RULE}; border-radius: 8px; padding: .6rem .7rem; margin-bottom: .55rem;
    background: #FFFDF9; }}
  .pick .rk {{ font-family: ui-monospace, monospace; color: {ACCENT}; font-weight: 700; font-size: .8rem; }}
  .pick .sc {{ float: right; font-family: ui-monospace, monospace; font-size: .74rem; color: {MUTED};
    border: 1px solid {RULE}; border-radius: 999px; padding: .02rem .45rem; }}
  .pick .ti {{ font-family: Georgia, serif; font-weight: 600; font-size: .98rem; color: {INK};
    line-height: 1.25; margin: .15rem 0 .1rem; }}
  .pick .pub {{ font-size: .78rem; color: {ACCENT}; }}
  .pick .ra {{ font-size: .84rem; color: {MUTED}; line-height: 1.35; margin-top: .3rem; }}
  .note {{ color: {MUTED}; font-size: .9rem; }}
  .gt {{ border: 1px solid {RULE}; border-left: 3px solid {ACCENT}; border-radius: 6px;
    padding: .5rem .7rem; margin-bottom: .45rem; background: #FFFDF9; }}
  .gt .d {{ font-family: ui-monospace, monospace; font-size: .72rem; color: {MUTED}; }}
  .gt .h {{ font-family: Georgia, serif; font-weight: 600; color: {INK}; }}
  .gt .st {{ float: right; font-family: ui-monospace, monospace; font-size: .72rem; }}
  .miss {{ color: {MUTED}; }}  .hit {{ color: {ACCENT}; font-weight: 700; }}
  .about p {{ color: {INK}; font-size: 1.0rem; line-height: 1.6; max-width: 68ch; }}
  .about h2 {{ font-family: Georgia, serif; font-size: 1.6rem; color: {INK}; margin-bottom: .3rem; }}
</style>
""", unsafe_allow_html=True)


# --- header ---------------------------------------------------------------

st.markdown(
    '<div class="masthead"><div class="kicker">A live eval of machine taste'
    f'&nbsp;&nbsp;·&nbsp;&nbsp;Data as of {pretty_date(latest_data_date())}</div>'
    '<h1>Predicting Tyler</h1></div>'
    '<div class="dek">Each day, three models read the same fresh posts and rank what '
    'Tyler Cowen is most likely to link on Marginal Revolution. When he posts, we score '
    'them against his real picks.</div>'
    '<div class="more"><a href="#about">How this works</a></div>',
    unsafe_allow_html=True)

track_label = ui.tabs(list(TRACKS), default_value="Substack", key="track")
track = TRACKS[track_label]
run = latest_run(track, "v2.0") or latest_run(track)
launch = launch_date(track)


# --- today's picks (the models' predictions) ------------------------------

st.markdown('<div class="section-rule">Today\'s picks</div>', unsafe_allow_html=True)

if not run:
    st.info("No ranking run yet for this track.")
else:
    ms = models_in(run["run_id"])
    cols = st.columns(len(ms))
    for col, m in zip(cols, ms):
        with col:
            st.markdown(f'<div class="model-h">{short(m)}</div>', unsafe_allow_html=True)
            for p in picks(run["run_id"], m, limit=6):
                title = (p["title"] or "").replace("<", "&lt;")
                ra = (p["rationale"] or "").replace("<", "&lt;")
                st.markdown(
                    f'<div class="pick"><span class="rk">#{p["rank"]}</span>'
                    f'<span class="sc">{p["score"]}</span>'
                    f'<div class="ti">{title}</div>'
                    f'<div class="pub">{p["publication"]}</div>'
                    f'<div class="ra">{ra}</div></div>',
                    unsafe_allow_html=True)
    st.markdown(
        f'<div class="note">Run <code>{run["run_id"]}</code> · prompt {run["prompt_version"]} '
        f'· corpus {run["corpus_version"]} · {run["candidates_count"]} candidates.</div>',
        unsafe_allow_html=True)


# --- what Tyler actually links (ground truth, since launch) ----------------

st.markdown('<div class="section-rule">What Tyler actually links</div>', unsafe_allow_html=True)

actual = tyler_links_since(track, launch)
if not actual:
    st.markdown(
        f'<div class="note">Tracking began {pretty_date(launch)}. As Tyler links a '
        f'{track_label} item from here on, it appears here, marked in or out of corpus '
        'and scored against the models above.</div>', unsafe_allow_html=True)
else:
    domains = corpus_domains()
    n_hit = sum(1 for r in actual if r["match_type"] in ("exact", "content_match"))
    st.markdown(
        f'<div class="note">Since {pretty_date(launch)}, Tyler has linked <b>{len(actual)}</b> '
        f'{track_label} item(s); <b>{n_hit}</b> scored against the models.</div>',
        unsafe_allow_html=True)
    for r in actual:
        host, slug = url_label(r["canonical_url"])
        if r["match_type"] in ("exact", "content_match"):
            ranks = candidate_best_ranks(r["matched_candidate_id"])
            rk = " · ".join(f"{short(x['model'])} #{x['r']}" for x in ranks) or "unranked"
            status = f'<span class="st hit">✓ ranked — {rk}</span>'
        elif host in domains:
            status = '<span class="st miss">in corpus</span>'
        else:
            status = '<span class="st miss">outside the corpus</span>'
        st.markdown(
            f'<div class="gt">{status}<span class="d">{r["mr_post_date"]}</span> · '
            f'<span class="h">{host}</span> <span class="miss">/ {slug[:60]}</span></div>',
            unsafe_allow_html=True)


# --- leaderboard (accruing) ----------------------------------------------

st.markdown('<div class="section-rule">Leaderboard</div>', unsafe_allow_html=True)
health = corpus_health(track)
if health["hits"] == 0:
    st.markdown(
        '<div class="note">No scored opportunities yet. The leaderboard '
        '(recall@20 / @50, MRR, and calibration per model) fills in as Tyler links '
        f'candidates within the {("14" if track=="nber" else "7")}-day matching window.</div>',
        unsafe_allow_html=True)
else:
    st.write("Coming online — hits recorded.")


# --- corpus health --------------------------------------------------------

st.markdown('<div class="section-rule">Corpus health</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="note">The <b>corpus</b> is the fixed set of sources we watch — the pool '
    'the models rank from each day. For Substack it\'s a frozen watchlist of every '
    'publication Tyler has linked since 2022; for NBER it\'s the working-paper feed itself.</div>',
    unsafe_allow_html=True)
c1, c2, c3, c4 = st.columns(4)
with c1:
    ui.metric_card("Watchlist publications", str(health["publications"]),
                   "frozen corpus v1.0", key="m1")
with c2:
    ui.metric_card(f"Candidates today · {track_label}", str(health["candidates"]),
                   "ranked this run", key="m2")
with c3:
    ui.metric_card("Models", "3", "Opus · GPT · Kimi", key="m3")
with c4:
    ui.metric_card("Ground-truth hits", str(health["hits"]),
                   "accruing as Tyler posts", key="m4")


# --- about ----------------------------------------------------------------

st.markdown("""
<div class="about" id="about">
<h2>How this works</h2>
<p><b>Predicting Tyler</b> is a daily test of whether a language model can anticipate
what Tyler Cowen links on Marginal Revolution. Tyler has previously discussed
<a href="https://marginalrevolution.com/marginalrevolution/2025/01/should-you-be-writing-for-the-ais.html" target="_blank" rel="noopener">"writing for the AIs"</a>.
This turns that around: can the AIs model <i>him</i>? Each morning several models see
the same fresh material and guess what he'll pick; we grade them against his real
choices.</p>

<p><b>Why only Substacks and NBER working papers?</b> Grading a prediction fairly means
knowing the full set of things a model <i>could</i> have picked that day — and that set
has to be one we can actually see. Substack feeds and NBER's working-paper listings are
open and scrapable: no paywalls, no paid APIs, no login. Most of Tyler's other links —
newspapers, journals, X — sit behind paywalls or have no clean, enumerable candidate
pool, so we record them but don't score them. The constraint is access, not editorial
preference.</p>

<p><b>Why do the models read only the first ~900 words of each post?</b> Ideally they'd
read every word. In practice, feeding the complete text of a full day's candidate pool
overruns the context window of the smallest model in the lineup and runs up the bill.
Capping each post at ~900 words keeps every model on equal footing at a workable cost.
It's a design constraint, not an ideal.</p>

<p><b>How scoring works.</b> This is a live, forward-looking eval. Each morning the
models rank; over the following days, when Tyler posts, we check whether he linked
anything they ranked, and how highly.</p>

<p><b>How it was built.</b> We scraped roughly two decades of Marginal Revolution posts
and every outbound link in them. From the links that pointed at Substack, we derived a
watchlist of every Substack publication Tyler has linked since 2022. To capture what
his taste actually is, we had Claude Fable read a
broad, two-decade sample of his past roundups (his own framing of each link) and write a
profile of his sensibilities, which each ranking model receives alongside the day's
candidates. Each day the
system polls those feeds (and NBER's) for new posts, has the
models rank them, then matches Tyler's actual new links back against both the candidate
pool and the predictions.</p>
</div>
""", unsafe_allow_html=True)
