"""Unit tests for the small pure helpers in `archive_scraper`.

Networked functions (`fetch`, `scrape_month`, `scrape_range`) are exercised via
the live smoke-test flow, not here. This file covers only the pieces that don't
touch the network or filesystem.
"""

from __future__ import annotations

import pytest

from src.archive_extract import OutboundLink
from src.archive_scraper import (
    _has_substack_link,
    iter_months,
    is_tyler_authored,
    slug_from_post_url,
)


# --- is_tyler_authored ----------------------------------------------------

@pytest.mark.parametrize("author, expected", [
    ("Tyler Cowen", True),
    ("tyler cowen", True),                              # case-insensitive
    ("Tyler Cowen & Alexander Tabarrok", True),         # co-authored counts
    ("Alexander Tabarrok & Tyler Cowen", True),         # order irrelevant
    ("Alexander Tabarrok", False),
    ("Alex Tabarrok", False),
    ("Guest Contributor", False),
    ("", False),
    (None, False),
])
def test_is_tyler_authored(author, expected):
    assert is_tyler_authored(author) is expected


# --- _has_substack_link ---------------------------------------------------

def _link(href: str) -> OutboundLink:
    return OutboundLink(position=1, href=href, anchor_text="x", surrounding_text="x")


def test_has_substack_link_true_on_direct_substack():
    assert _has_substack_link([_link("https://foo.substack.com/p/bar")]) is True


def test_has_substack_link_true_via_open_substack_rewrite():
    """canonicalize() rewrites open.substack.com/pub/foo/p/bar to foo.substack.com,
    so the has_substack_link check catches these even in raw href form."""
    assert _has_substack_link([
        _link("https://open.substack.com/pub/foo/p/bar")
    ]) is True


def test_has_substack_link_true_when_any_link_matches():
    assert _has_substack_link([
        _link("https://nytimes.com/x"),
        _link("https://foo.substack.com/p/bar"),
        _link("https://arxiv.org/abs/1234"),
    ]) is True


def test_has_substack_link_false_when_none_match():
    assert _has_substack_link([
        _link("https://nytimes.com/x"),
        _link("https://arxiv.org/abs/1234"),
    ]) is False


def test_has_substack_link_false_on_empty():
    assert _has_substack_link([]) is False


# --- iter_months ---------------------------------------------------------

def test_iter_months_single_month():
    assert list(iter_months("2020-01", "2020-01")) == [(2020, 1)]


def test_iter_months_spans_year_boundary():
    assert list(iter_months("2019-11", "2020-02")) == [
        (2019, 11), (2019, 12), (2020, 1), (2020, 2),
    ]


def test_iter_months_start_after_end_yields_nothing():
    assert list(iter_months("2020-06", "2020-05")) == []


# --- slug_from_post_url --------------------------------------------------

def test_slug_from_post_url_basic():
    url = "https://marginalrevolution.com/marginalrevolution/2024/01/monday-assorted-links-999.html"
    assert slug_from_post_url(url) == "monday-assorted-links-999.html"


def test_slug_from_post_url_strips_trailing_slash():
    assert slug_from_post_url("https://example.com/a/b/c/") == "c"
