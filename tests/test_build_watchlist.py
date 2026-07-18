"""Unit tests for the pure pieces of `build_watchlist`.

Network-touching functions (`fetch_feed`, `verify_direct_substack`, etc.) are
exercised via the live dry-run flow, not here.
"""

from __future__ import annotations

import pytest

from src.build_watchlist import (
    is_direct_substack_host,
    is_internal_substack_host,
    parse_substack_feed,
    read_manual_additions,
)


# --- Host classification -------------------------------------------------

@pytest.mark.parametrize("host, expected", [
    ("noahpinion.substack.com", True),
    ("thezvi.substack.com", True),
    ("foo-bar.substack.com", True),
    # Root marketing site and share-URL subdomains aren't publications
    ("substack.com", False),
    ("www.substack.com", False),
    ("open.substack.com", False),
    ("on.substack.com", False),
    # Email trackers
    ("email.mg1.substack.com", False),
    ("email.mg2.substack.com", False),
    ("email.mg99.substack.com", False),
    # Not on substack.com at all
    ("noahpinion.blog", False),
    ("slowboring.com", False),
    ("example.com", False),
])
def test_is_direct_substack_host(host, expected):
    assert is_direct_substack_host(host) is expected


@pytest.mark.parametrize("host, expected", [
    ("substack.com", True),
    ("www.substack.com", True),
    ("open.substack.com", True),
    ("on.substack.com", True),
    ("email.mg2.substack.com", True),
    ("email.mg42.substack.com", True),
    ("noahpinion.substack.com", False),
    ("mail.substack.com", False),           # not an email tracker pattern
    ("slowboring.com", False),
])
def test_is_internal_substack_host(host, expected):
    assert is_internal_substack_host(host) is expected


# --- Feed fingerprinting -------------------------------------------------

_SUBSTACK_FEED_WITH_GENERATOR = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Some Substack Publication</title>
    <link>https://foo.substack.com</link>
    <generator>Substack</generator>
    <item><title>Post 1</title></item>
  </channel>
</rss>"""


_SUBSTACK_FEED_WITH_SUBSTACK_LINK = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Custom Domain Substack</title>
    <link>https://slowboring.com</link>
    <atom:link href="https://slowboring.substack.com/feed" rel="self"/>
    <generator>WordPress or something</generator>
  </channel>
</rss>"""


_NON_SUBSTACK_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>The New York Times</title>
    <link>https://nytimes.com</link>
    <generator>NYT Custom</generator>
  </channel>
</rss>"""


_MALFORMED = "<html><body>Not a feed</body></html>"


def test_parse_substack_feed_generator_match():
    parsed = parse_substack_feed(_SUBSTACK_FEED_WITH_GENERATOR)
    assert parsed is not None
    name, sub = parsed
    assert name == "Some Substack Publication"


def test_parse_substack_feed_link_match_finds_substack_subdomain():
    parsed = parse_substack_feed(_SUBSTACK_FEED_WITH_SUBSTACK_LINK)
    assert parsed is not None
    name, sub = parsed
    assert name == "Custom Domain Substack"
    assert sub == "slowboring.substack.com"


def test_parse_substack_feed_rejects_non_substack():
    assert parse_substack_feed(_NON_SUBSTACK_FEED) is None


def test_parse_substack_feed_rejects_malformed():
    assert parse_substack_feed(_MALFORMED) is None


def test_parse_substack_feed_rejects_empty():
    assert parse_substack_feed("") is None


# --- Manual additions parsing --------------------------------------------

def test_read_manual_additions_creates_empty_file_when_missing(tmp_path, monkeypatch):
    from src import build_watchlist
    fake_path = tmp_path / "manual_additions.txt"
    monkeypatch.setattr(build_watchlist, "MANUAL_ADDITIONS_PATH", fake_path)
    result = read_manual_additions()
    assert result == []
    assert fake_path.exists()
    assert "# One Substack feed URL per line" in fake_path.read_text()


def test_read_manual_additions_parses_various_forms(tmp_path, monkeypatch):
    from src import build_watchlist
    fake_path = tmp_path / "manual_additions.txt"
    fake_path.write_text(
        "# Comment line\n"
        "\n"
        "noahpinion.substack.com\n"
        "https://slowboring.com\n"
        "https://foo.substack.com/feed\n"
    )
    monkeypatch.setattr(build_watchlist, "MANUAL_ADDITIONS_PATH", fake_path)
    result = read_manual_additions()
    urls = [p.feed_url for p in result]
    assert urls == [
        "https://noahpinion.substack.com/feed",
        "https://slowboring.com/feed",
        "https://foo.substack.com/feed",
    ]
    assert all(p.source == "manual" for p in result)
