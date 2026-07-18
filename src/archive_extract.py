"""Pure HTML → structured data for the Marginal Revolution archive.

Split out from `archive_scraper.py` so every parsing rule can be unit-tested
against fixture HTML without touching the network. If MR changes their markup,
only this module needs adjusting — the fixture-based tests will flag the
regression before we notice it in production.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup


# A post title is an "assorted links" post if this substring appears (case-insensitive).
# Matches: "Monday assorted links", "Friday assorted links, election edition", etc.
_ASSORTED_LINKS_RE = re.compile(r"assorted links", re.IGNORECASE)


@dataclass
class ArchivePostLink:
    """One post listed on a monthly archive index page."""
    url: str
    title: str


@dataclass
class OutboundLink:
    """One outbound link found inside an MR post body."""
    position: int          # 1-indexed order in the post body
    href: str
    anchor_text: str
    surrounding_text: str  # text of the enclosing <li>/<p>, trimmed


@dataclass
class ExtractedPost:
    """Structured data from one MR post's HTML."""
    url: str
    title: str
    published_at: Optional[str]           # ISO 8601 timestamp if discoverable
    author: Optional[str] = None          # verbatim byline from meta[name="author"]
    links: list[OutboundLink] = field(default_factory=list)


def is_assorted_links(title: str) -> bool:
    return bool(_ASSORTED_LINKS_RE.search(title))


def extract_archive_page(
    html: str,
    page_url: str,
) -> tuple[list[ArchivePostLink], Optional[str]]:
    """Parse a WordPress monthly archive page.

    Returns (posts_on_this_page, next_page_url_or_none).

    Post entries are identified by `<article>` or `<h*.entry-title>` headings
    containing an anchor whose href points somewhere under
    `marginalrevolution.com/marginalrevolution/`. Duplicates within a single
    page are dropped (WordPress themes sometimes repeat post titles in
    sidebars and "recent posts" widgets).
    """
    soup = BeautifulSoup(html, "lxml")
    posts: list[ArchivePostLink] = []
    seen: set[str] = set()

    # Prefer strict selectors first, then progressively relax
    selector_ladder = [
        "article h2.entry-title a, article h1.entry-title a",
        "h2.entry-title a, h1.entry-title a",
        "article h2 a, article h3 a",
    ]
    for selector in selector_ladder:
        for a in soup.select(selector):
            href = (a.get("href") or "").strip()
            title = a.get_text(strip=True)
            if not href or not title:
                continue
            abs_href = urljoin(page_url, href)
            if "/marginalrevolution/" not in abs_href:
                continue
            if abs_href in seen:
                continue
            seen.add(abs_href)
            posts.append(ArchivePostLink(url=abs_href, title=title))
        if posts:
            break  # first selector to yield anything wins

    # Next-page link: prefer <link rel="next">, else fall back to the WP pagination widget.
    next_url: Optional[str] = None
    link_rel_next = soup.find("link", rel="next")
    if link_rel_next and link_rel_next.get("href"):
        next_url = urljoin(page_url, link_rel_next["href"])
    else:
        a_next = soup.select_one("a.next.page-numbers, .nav-previous a, .older-posts a")
        if a_next and a_next.get("href"):
            next_url = urljoin(page_url, a_next["href"])

    return posts, next_url


def extract_post(html: str, post_url: str) -> ExtractedPost:
    """Parse one MR post's HTML into an `ExtractedPost`.

    Robust to markup variations:
      - Title tried from h1.entry-title, then h2.entry-title, then first article heading.
      - Published date tried from <time datetime="…">, then
        <meta property="article:published_time">.
      - Body tried from div.entry-content, then article body as a fallback.

    Links inside the body are captured with their anchor text and the text of
    the enclosing <li> or <p> as `surrounding_text`. Truly external links only
    — mailto:, javascript:, and pure anchor (#foo) links are skipped.
    """
    soup = BeautifulSoup(html, "lxml")

    title_el = soup.select_one(
        "h1.entry-title, h2.entry-title, article h1, article h2"
    )
    title = title_el.get_text(strip=True) if title_el else ""

    published_at: Optional[str] = None
    time_el = soup.select_one("time.entry-date[datetime], time[datetime]")
    if time_el:
        published_at = time_el.get("datetime")
    if not published_at:
        meta_el = soup.select_one('meta[property="article:published_time"]')
        if meta_el:
            published_at = meta_el.get("content")

    # Byline. MR emits `<meta name="author" content="Tyler Cowen">` on every post,
    # co-authored posts appear as "Tyler Cowen & Alexander Tabarrok". Fall back
    # to span.author (the visible byline) if the meta tag is missing.
    author: Optional[str] = None
    author_meta = soup.select_one('meta[name="author"]')
    if author_meta:
        author = (author_meta.get("content") or "").strip() or None
    if not author:
        author_el = soup.select_one("span.author, .byline .author, .author")
        if author_el:
            author = author_el.get_text(strip=True) or None

    # Prefer the tightest container so we don't pick up footer/nav links. We try
    # selectors sequentially (rather than a single comma-separated selector)
    # because `select_one` on `A, B` returns first-in-document-order, not the
    # first non-empty selector — which would pick <article> over <div.entry-content>.
    body = None
    for selector in ("div.entry-content", "article .entry-content", "article"):
        body = soup.select_one(selector)
        if body is not None:
            break
    links: list[OutboundLink] = []
    if body is not None:
        pos = 0
        for a in body.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            abs_href = urljoin(post_url, href)
            anchor_text = a.get_text(strip=True)
            # Prefer the closest <li>, then <p>, then any block-level parent
            container = a.find_parent(["li", "p", "blockquote", "div"])
            surrounding = (
                container.get_text(" ", strip=True) if container else anchor_text
            )
            if len(surrounding) > 500:
                surrounding = surrounding[:500].rsplit(" ", 1)[0] + "…"
            pos += 1
            links.append(OutboundLink(
                position=pos,
                href=abs_href,
                anchor_text=anchor_text,
                surrounding_text=surrounding,
            ))

    return ExtractedPost(
        url=post_url,
        title=title,
        published_at=published_at,
        author=author,
        links=links,
    )


def to_dict(post: ExtractedPost) -> dict:
    """JSON-serializable dict of an `ExtractedPost`."""
    return asdict(post)
