"""Predicting Tyler — styled as a close homage to Marginal Revolution's layout.

Mirrors MR's chrome (mint-green masthead, black nav bar, left sidebar with a
vertical rule, serif blue-link post body) but is clearly OUR project: the black
nav bar is the editor toggle, and the sidebar/footer — where MR runs promos and
comments — explains the experiment. The toggle swaps whose "assorted links" you
read: Claude Opus, ChatGPT, Kimi, or Tyler's actual picks. Read-only over
data/tyler.db.

    streamlit run src/dashboard.py
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import streamlit as st

DB = Path("data/tyler.db")

# --- MR palette (sampled from the real MR page; tune the vars) ------------
GREEN = "#56bd9b"       # masthead band (sampled + region-averaged from the real MR page)
NAVBG = "#000000"       # nav bar
INK = "#1b1b1b"         # body text
LINK = "#1a3ecc"        # post links + byline name (MR's vivid royal blue, sampled)
RULE = "#dcdcdc"
MUTED = "#6b6b6b"
PAPER = "#ffffff"

EDITORS = {  # url key -> (menu label, model id or __tyler__)
    "opus":  ("Claude Opus 4.8",   "anthropic/claude-opus-4.8"),
    "gpt":   ("ChatGPT",           "openai/gpt-5.6-sol"),
    "kimi":  ("Kimi K2.6",         "moonshotai/kimi-k2.6"),
    "tyler": ("Tyler’s picks",     "__tyler__"),
}


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
    """Download tyler.db from the GitHub Release when missing or stale (D-41)."""
    from datetime import datetime
    import httpx
    repo = _secret("GH_REPO", "humzahbkhan-spec/mr_eval")
    tag = _secret("DB_RELEASE_TAG", "data-latest")
    token = _secret("GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        rel = httpx.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
                        headers=headers, timeout=30.0).json()
        asset = next((a for a in rel.get("assets", []) if a["name"] == "tyler.db"), None)
    except Exception:
        asset = None
    if asset is None:
        if DB.exists():
            return
        st.error("No data release reachable yet.")
        st.stop()
    rel_ts = datetime.fromisoformat(asset["updated_at"].replace("Z", "+00:00")).timestamp()
    if DB.exists() and DB.stat().st_mtime + 120 >= rel_ts:
        return
    with st.spinner("Loading the latest data…"):
        blob = httpx.get(asset["url"], follow_redirects=True, timeout=180.0,
                         headers={**headers, "Accept": "application/octet-stream"})
        DB.parent.mkdir(parents=True, exist_ok=True)
        DB.write_bytes(blob.content)


@st.cache_resource
def _conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def latest_run_ids():
    ids = []
    for track in ("substack", "nber"):
        r = _conn().execute(
            "SELECT run_id FROM runs WHERE track=? AND kind='live' AND prompt_version='v2.0' "
            "ORDER BY started_at DESC LIMIT 1", (track,)).fetchone()
        if r:
            ids.append(r["run_id"])
    return ids


def latest_data_date():
    return _conn().execute("SELECT MAX(run_date) FROM runs WHERE kind='live'").fetchone()[0]


def editor_roundup(model: str, limit: int = 12):
    ids = latest_run_ids()
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    return _conn().execute(
        f"SELECT p.score, p.rank, p.rationale, p.track, c.title, c.url, pub.name AS pub "
        f"FROM predictions p JOIN candidates c ON c.id=p.candidate_id "
        f"JOIN publications pub ON pub.id=c.publication_id "
        f"WHERE p.model=? AND p.run_id IN ({ph}) "
        f"ORDER BY p.score DESC, p.rank LIMIT ?",
        [model, *ids, limit]).fetchall()


def tyler_roundup(days: int = 6):
    since = (date.today() - timedelta(days=days)).isoformat()
    return _conn().execute(
        "SELECT g.mr_post_date, g.canonical_url, g.raw_url, g.track, g.match_type, "
        "       g.matched_candidate_id, cand.title AS ctitle, pub.name AS cpub "
        "FROM ground_truth g "
        "LEFT JOIN candidates cand ON cand.id=g.matched_candidate_id "
        "LEFT JOIN publications pub ON pub.id=cand.publication_id "
        "WHERE g.mr_post_date>=? AND g.track IN ('substack','nber') "
        "ORDER BY g.mr_post_date DESC, g.link_position", (since,)).fetchall()


def model_ranks(candidate_id: int):
    return _conn().execute(
        "SELECT model, MIN(rank) AS r FROM predictions WHERE candidate_id=? GROUP BY model",
        (candidate_id,)).fetchall()


def corpus_count():
    return _conn().execute(
        "SELECT COUNT(*) FROM publications WHERE track='substack' AND active=1").fetchone()[0]


def scored_hits(limit: int = 5):
    return _conn().execute(
        "SELECT g.matched_candidate_id cid, cand.title, pub.name pub "
        "FROM ground_truth g JOIN candidates cand ON cand.id=g.matched_candidate_id "
        "JOIN publications pub ON pub.id=cand.publication_id "
        "WHERE g.match_type='exact' AND g.mr_post_date>='2026-07-18' "
        "ORDER BY g.mr_post_date DESC LIMIT ?", (limit,)).fetchall()


def corpus_substack_domains() -> set:
    """Canonical domains of the active Substack watchlist — the publications we cover."""
    return {row[0] for row in _conn().execute(
        "SELECT canonical_domain FROM publications WHERE track='substack' AND active=1")}


def candidate_ingest_day(canonical_url: str, track: str):
    """First date (YYYY-MM-DD) we ingested a candidate at this exact URL, or None.

    Lets the Tyler view tell a *timing* miss (we did eventually ingest the post,
    just too late) apart from a genuine corpus gap (we never had it at all).
    """
    row = _conn().execute(
        "SELECT MIN(substr(ingested_at, 1, 10)) FROM candidates "
        "WHERE canonical_url = ? AND track = ?", (canonical_url, track)).fetchone()
    return row[0] if row and row[0] else None


def short(model: str) -> str:
    return {"anthropic/claude-opus-4.8": "Opus", "openai/gpt-5.6-sol": "GPT",
            "moonshotai/kimi-k2.6": "Kimi"}.get(model, model.split("/")[-1])


def esc(s) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clip(s, n) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def url_slug(u: str):
    p = urlsplit(u or "")
    host = (p.hostname or "").replace("www.", "")
    slug = (p.path.strip("/").split("/")[-1] or "").replace("-", " ")
    return host, slug


def pretty_date(iso) -> str:
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%A, %B %-d, %Y")
    except (TypeError, ValueError):
        return str(iso or "")


# --- page -----------------------------------------------------------------

st.set_page_config(page_title="Predicting Tyler", page_icon="📖", layout="wide",
                   initial_sidebar_state="collapsed")
ensure_db()

sel = st.query_params.get("editor", "opus")
if sel not in EDITORS:
    sel = "opus"
label, model = EDITORS[sel]


# ---- build the post body -------------------------------------------------

def build_post() -> str:
    if model == "__tyler__":
        head = ('<h1 class="ptitle">Assorted links</h1>'
                '<p class="byline"><i>by</i> <a class="who">Tyler Cowen</a></p>'
                '<hr class="short">')
        rows = tyler_roundup()
        if not rows:
            return head + ('<p class="empty">No Substack or NBER links from Tyler in the last '
                           'few days. His news and X links are recorded but not scored.</p>')
        domains = corpus_substack_domains()
        items = ""
        for r in rows:
            host, slug = url_slug(r["canonical_url"])
            title = esc(r["ctitle"]) if r["ctitle"] else esc(slug or host)
            src = esc(r["cpub"]) if r["cpub"] else esc(host)
            if r["match_type"] in ("exact", "content_match") and r["matched_candidate_id"]:
                rk = " · ".join(f"{short(x['model'])} #{x['r']}"
                                for x in sorted(model_ranks(r["matched_candidate_id"]),
                                                key=lambda x: x["r"]))
                note = f'<span class="hit">the models ranked it: {rk}</span>'
            else:
                # An unmatched link: distinguish a timing miss (we did ingest the
                # exact post, just after Tyler had already linked it — so it was
                # never a fair opportunity) from a real corpus gap.
                cday = candidate_ingest_day(r["canonical_url"], r["track"])
                if cday and cday > r["mr_post_date"][:10]:
                    note = ('<span class="late">in the corpus, but published after '
                            'our last scan</span>')
                elif r["track"] == "nber" or host in domains or cday:
                    note = ('<span class="incorp">in the corpus, but not in that '
                            'day’s candidate pool</span>')
                else:
                    note = '<span class="miss">outside the corpus</span>'
            gloss = f'{src} · {note}'
            items += (f'<li><a href="{r["raw_url"]}" target="_blank" rel="noopener">{title}</a>.'
                      f'<div class="gloss">{gloss} · linked {r["mr_post_date"]}</div></li>')
        return head + f'<ol class="links">{items}</ol>'

    head = (f'<h1 class="ptitle">Assorted links</h1>'
            f'<p class="byline"><i>by</i> <a class="who">{esc(label)}</a> '
            f'<i>{pretty_date(latest_data_date())}</i></p><hr class="short">')
    rows = editor_roundup(model)
    if not rows:
        return head + '<p class="empty">No ranking run available yet.</p>'
    items = ""
    for r in rows:
        src = "NBER Working Papers" if r["track"] == "nber" else esc(r["pub"])
        items += (f'<li><a href="{r["url"]}" target="_blank" rel="noopener">{esc(r["title"])}</a>.'
                  f'<div class="gloss">{src} — {esc(clip(r["rationale"], 150))}</div></li>')
    tail = (f'<p class="method">{esc(label)}’s guesses at what Tyler would link, ranked by '
            f'confidence. See <b>Tyler’s picks</b> above for what he actually chose.</p>')
    return head + f'<ol class="links">{items}</ol>' + tail


# ---- build the sidebar ---------------------------------------------------

def build_sidebar() -> str:
    res = ""
    for h in scored_hits():
        rk = " · ".join(f"{short(x['model'])} #{x['r']}"
                        for x in sorted(model_ranks(h["cid"]), key=lambda x: x["r"]))
        res += (f'<div class="res">{esc(clip(h["title"], 42))}'
                f'<span class="rk">{rk}</span></div>')
    if not res:
        res = '<p>No scored picks yet — they appear here as Tyler links things the models ranked.</p>'
    return f"""
      <div class="mark"><span class="m1">PREDICTING</span> <span class="m2">TYLER</span></div>
      <p class="sub">A live eval of machine taste.</p>

      <div class="wtitle">The experiment</div>
      <p class="wtext">Predicting Tyler is a daily test of whether a language model can anticipate
        what Tyler Cowen links on Marginal Revolution. Tyler has previously discussed
        <a href="https://marginalrevolution.com/marginalrevolution/2025/01/should-you-be-writing-for-the-ais.html"
           target="_blank" rel="noopener">“writing for the AIs”</a>. This turns that around: can
        the AIs model <i>him</i>? Each morning several models see the same fresh material and guess
        what he’ll pick. Then we grade them against his real choices.</p>

      <div class="wtitle">Latest results</div>
      {res}

      <div class="wtitle">Why Substack &amp; NBER?</div>
      <p class="wtext">Grading a prediction fairly means knowing the full set of things a model
        <i>could</i> have picked that day. Substack feeds and NBER’s working-paper listings are open
        and scrapable: no paywalls, no paid APIs, no login. Most of Tyler’s other links — newspapers,
        journals, X — sit behind paywalls or have no clean, enumerable candidate pool, so we record
        them but don’t score them.</p>

      <div class="wtitle">The corpus</div>
      <p class="wtext">A frozen watchlist of every Substack publication Tyler has linked since 2022
        ({corpus_count()} in all), plus NBER’s working-paper feed. To capture what his taste actually
        is, we had Claude Fable read a broad, two-decade sample of his past roundups and write a
        profile of his sensibilities, which each ranking model receives alongside the day’s
        candidates. The models read the first ~900 words of each post. Reading everything would
        overflow the smallest model’s context window and cost more.</p>
    """


# ---- nav (the editor toggle) ---------------------------------------------

nav = ""
for key, (lbl, _m) in EDITORS.items():
    cls = "navitem active" if key == sel else "navitem"
    nav += f'<a class="{cls}" href="?editor={key}" target="_self">{esc(lbl)}</a>'


# ---- one HTML render -----------------------------------------------------

st.html(f"""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700;800&family=PT+Sans:wght@400;700&family=PT+Serif:ital,wght@0,400;0,700;1,400&display=swap');
  #MainMenu, footer, [data-testid="stToolbar"], [data-testid="stDecoration"],
  [data-testid="stStatusWidget"], [data-testid="stHeader"],
  [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] {{ display:none !important; }}
  .stApp {{ background:{PAPER}; }}
  .block-container, [data-testid="stMainBlockContainer"] {{
    max-width:100% !important; padding:0 !important; margin:0 !important; }}
  [data-testid="stMarkdownContainer"] {{ font-family:'PT Serif',Georgia,'Times New Roman',serif; }}

  /* masthead */
  .mast {{ background:{GREEN}; padding:26px 40px 20px 60px; position:relative; }}
  .mast .wm {{ text-align:left; color:#111; font-family:'Montserrat','Helvetica Neue',Arial,sans-serif;
    font-size:2.9rem; line-height:1; letter-spacing:.02em; }}
  .mast .wm b {{ font-weight:800; }} .mast .wm span {{ font-weight:400; }}
  .mast .tag {{ text-align:left; color:#111; font-family:'PT Sans','Helvetica Neue',Arial,sans-serif;
    font-weight:700; font-size:.72rem; letter-spacing:.16em; margin-top:8px; }}

  /* nav = editor toggle */
  .nav {{ background:{NAVBG}; display:flex; justify-content:center; flex-wrap:wrap; }}
  .navitem {{ color:#fff !important; font-family:'PT Sans','Helvetica Neue',Arial,sans-serif; font-weight:700;
    font-size:.98rem; padding:15px 26px; text-decoration:none !important; border-left:1px solid #333; }}
  .navitem:first-child {{ border-left:none; }}
  .navitem:hover {{ color:{GREEN} !important; }}
  .navitem.active {{ color:{GREEN} !important; }}

  /* body layout: left sidebar + rule + main, hugging the left like MR */
  .wrap {{ display:flex; max-width:1240px; margin:0; padding:0 30px; }}
  .side {{ width:250px; flex:none; padding:36px 28px 40px 0; border-right:1px solid {RULE}; }}
  .main {{ flex:1; max-width:730px; padding:36px 0 50px 44px; min-width:0; }}

  /* sidebar widgets */
  .side .mark {{ font-family:'Montserrat','Helvetica Neue',Arial,sans-serif; font-size:1.5rem; color:#111;
    letter-spacing:.01em; }}
  .side .mark .m2 {{ font-weight:800; }} .side .mark .m1 {{ font-weight:400; }}
  .side .sub {{ color:{MUTED}; font-size:.86rem; line-height:1.45; margin:.4rem 0 .9rem; }}
  .side .gbtn {{ display:block; text-align:center; border:1px solid {GREEN}; color:{GREEN} !important;
    background:#fff; border-radius:5px; padding:9px 12px; font-family:'PT Sans','Helvetica Neue',Arial,sans-serif;
    font-weight:600; font-size:.86rem; text-decoration:none !important; margin-bottom:1.6rem; }}
  .side .gbtn:hover {{ background:{GREEN}; color:#fff !important; }}
  .side .wtitle {{ font-family:'PT Sans','Helvetica Neue',Arial,sans-serif; font-weight:700; font-size:.78rem;
    text-transform:uppercase; letter-spacing:.1em; color:#222; border-bottom:1px solid {RULE};
    padding-bottom:5px; margin:1.5rem 0 .7rem; }}
  .side .wtext {{ color:#3a3a3a; font-size:.86rem; line-height:1.5; margin:0; }}
  .side .res {{ font-size:.82rem; color:{INK}; line-height:1.35; margin-bottom:.55rem; }}
  .side .res .rk {{ display:block; font-family:'PT Sans','Helvetica Neue',Arial,sans-serif; font-size:.7rem;
    color:{MUTED}; }}
  .side a {{ color:{LINK}; }}

  /* post */
  .ptitle {{ font-family:'PT Sans','Helvetica Neue',Arial,sans-serif; font-weight:700; font-size:1.9rem;
    color:{INK}; margin:0 0 .35rem; }}
  .byline {{ color:{INK}; font-size:1.02rem; margin:0 0 .3rem; }}
  .byline .who {{ color:{LINK}; font-style:normal; }}
  hr.short {{ border:none; border-top:1px solid {RULE}; width:130px; margin:.6rem 0 1.3rem; }}
  ol.links {{ padding-left:1.7rem; margin:0; }}
  ol.links li {{ font-size:1.2rem; line-height:1.45; color:{INK}; margin-bottom:1.5rem; }}
  ol.links li a {{ color:{LINK}; text-decoration:underline; }}
  ol.links .gloss {{ font-size:.86rem; color:{MUTED}; margin-top:.15rem; line-height:1.4; }}
  ol.links .gloss .hit {{ color:{LINK}; }} ol.links .gloss .miss {{ color:#a06a2b; }}
  ol.links .gloss .late {{ color:#5a6b8c; }} ol.links .gloss .incorp {{ color:#6b6b6b; }}
  .method {{ margin-top:1.6rem; padding-top:1rem; border-top:1px solid {RULE}; color:{MUTED};
    font-size:.92rem; line-height:1.5; }}
  .empty {{ color:{MUTED}; font-style:italic; }}
  .foot {{ border-top:1px solid {RULE}; text-align:center; padding:22px 20px 30px;
    margin-top:8px; color:{MUTED}; font-family:'PT Sans','Helvetica Neue',Arial,sans-serif;
    font-size:.88rem; }}
  .foot a {{ color:{LINK}; }}
</style>

<div class="mast">
  <div class="wm"><span>PREDICTING</span><b>TYLER</b></div>
  <div class="tag">MARGINAL STEPS TOWARD PREDICTING TYLER COWEN</div>
</div>
<div class="nav">{nav}</div>
<div class="wrap">
  <aside class="side">{build_sidebar()}</aside>
  <main class="main">{build_post()}</main>
</div>
<div class="foot">For feedback and recommendations, reach out to
  <a href="mailto:humzah.b.khan@gmail.com">humzah.b.khan@gmail.com</a>.</div>
""")
