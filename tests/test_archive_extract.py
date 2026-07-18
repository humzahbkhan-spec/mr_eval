"""Unit tests for the archive HTML extractor.

These run against static fixture files under tests/fixtures/ — no network. Once
we scrape a real MR page, we should replace the synthetic fixtures with trimmed
real-world HTML so this suite stays honest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.archive_extract import (
    extract_archive_page,
    extract_post,
    is_assorted_links,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


# --- title predicate ------------------------------------------------------

@pytest.mark.parametrize("title, expected", [
    ("Monday assorted links", True),
    ("Friday assorted links, election edition", True),
    ("assorted links", True),
    ("ASSORTED LINKS", True),
    ("Thursday Assorted Links", True),
    ("Thoughts on something", False),
    ("Assorted", False),
    ("What I've been reading", False),
])
def test_is_assorted_links(title, expected):
    assert is_assorted_links(title) is expected


# --- archive page parsing -------------------------------------------------

def test_extract_archive_page_finds_all_posts():
    html = _read("mr_archive_page_sample.html")
    posts, _ = extract_archive_page(
        html, "https://marginalrevolution.com/marginalrevolution/2024/01/"
    )
    urls = [p.url for p in posts]
    # 4 unique post URLs; the duplicate in the sidebar widget must be deduped.
    assert len(posts) == 4
    assert urls == [
        "https://marginalrevolution.com/marginalrevolution/2024/01/monday-assorted-links-999.html",
        "https://marginalrevolution.com/marginalrevolution/2024/01/thoughts-on-something.html",
        "https://marginalrevolution.com/marginalrevolution/2024/01/friday-assorted-links-1000.html",
        "https://marginalrevolution.com/marginalrevolution/2024/01/wednesday-assorted-links-election-edition.html",
    ]


def test_extract_archive_page_returns_next_page_url():
    html = _read("mr_archive_page_sample.html")
    _, next_url = extract_archive_page(
        html, "https://marginalrevolution.com/marginalrevolution/2024/01/"
    )
    assert next_url == "https://marginalrevolution.com/marginalrevolution/2024/01/page/2/"


def test_extract_archive_page_skips_off_site_and_widget_links():
    """Twitter link and /about link in <aside> must not be treated as posts."""
    html = _read("mr_archive_page_sample.html")
    posts, _ = extract_archive_page(
        html, "https://marginalrevolution.com/marginalrevolution/2024/01/"
    )
    assert all("twitter.com" not in p.url for p in posts)
    assert all(p.url.startswith("https://marginalrevolution.com/") for p in posts)


def test_extract_archive_page_filter_yields_only_assorted_links():
    """The is_assorted_links predicate is what the scraper actually uses."""
    html = _read("mr_archive_page_sample.html")
    posts, _ = extract_archive_page(
        html, "https://marginalrevolution.com/marginalrevolution/2024/01/"
    )
    assorted = [p for p in posts if is_assorted_links(p.title)]
    assert len(assorted) == 3  # Monday, Friday, Wednesday-election-edition


# --- post parsing ---------------------------------------------------------

def test_extract_post_title_and_published_at():
    html = _read("mr_post_sample.html")
    post = extract_post(
        html,
        "https://marginalrevolution.com/marginalrevolution/2024/01/monday-assorted-links-999.html",
    )
    assert post.title == "Monday assorted links"
    assert post.published_at == "2024-01-15T13:37:00+00:00"


def test_extract_post_author_from_meta():
    html = _read("mr_post_sample.html")
    post = extract_post(html, "https://example.com/post")
    assert post.author == "Tyler Cowen"


def test_extract_post_author_missing_returns_none():
    html = "<html><head></head><body><article><h1 class='entry-title'>x</h1></article></body></html>"
    post = extract_post(html, "https://example.com/post")
    assert post.author is None


def test_extract_post_captures_all_outbound_links_in_order():
    html = _read("mr_post_sample.html")
    post = extract_post(
        html,
        "https://marginalrevolution.com/marginalrevolution/2024/01/monday-assorted-links-999.html",
    )
    hrefs = [ln.href for ln in post.links]
    # Six real outbound links; the #top anchor and mailto: are skipped.
    # The footer's "Previous" internal link IS captured because it's inside <article>
    # but not inside <div.entry-content>; we only look inside entry-content, so it's not.
    assert hrefs == [
        "https://foo.substack.com/p/interesting-post",
        "https://arxiv.org/abs/2401.12345",
        "https://bar.substack.com/p/hot-take?utm_source=twitter",
        "https://open.substack.com/pub/baz/p/deep-dive",
        "https://foo.com/related",
    ]


def test_extract_post_positions_are_1_indexed_and_sequential():
    html = _read("mr_post_sample.html")
    post = extract_post(html, "https://example.com/post")
    positions = [ln.position for ln in post.links]
    assert positions == list(range(1, len(positions) + 1))


def test_extract_post_surrounding_text_captures_context():
    html = _read("mr_post_sample.html")
    post = extract_post(html, "https://example.com/post")
    first = post.links[0]
    # Enclosing <li> was "1. The most interesting post today, worth reading."
    assert "interesting post" in first.surrounding_text.lower()
    assert "worth reading" in first.surrounding_text.lower()


def test_extract_post_anchor_and_surrounding_differ():
    """Anchor text is just the link text; surrounding_text is the whole enclosing block."""
    html = _read("mr_post_sample.html")
    post = extract_post(html, "https://example.com/post")
    ln = post.links[0]
    assert ln.anchor_text == "The most interesting post today"
    assert ln.surrounding_text != ln.anchor_text
    assert ln.anchor_text in ln.surrounding_text


def test_extract_post_skips_fragment_and_mailto_links():
    html = _read("mr_post_sample.html")
    post = extract_post(html, "https://example.com/post")
    for ln in post.links:
        assert not ln.href.startswith("#")
        assert not ln.href.startswith("mailto:")
