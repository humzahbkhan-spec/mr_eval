"""Unit tests for URL canonicalization.

Every rule in `src/normalize.py` is covered here. When a real-world URL surprises
the pipeline in production, add a case here first, watch it fail, then fix.
"""

from __future__ import annotations

import pytest

from src.normalize import canonicalize


# --- Basic shape rules -----------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    # Idempotent baseline
    ("https://foo.substack.com/p/bar", "https://foo.substack.com/p/bar"),

    # Scheme + host lowercased; path case preserved (slugs are case-sensitive)
    ("HTTPS://Foo.Substack.com/p/Bar", "https://foo.substack.com/p/Bar"),
    # http folded to https so old archive links match new ones (D-26)
    ("http://FOO.SUBSTACK.COM/p/bar", "https://foo.substack.com/p/bar"),
    ("http://nber.org/papers/w0404", "https://nber.org/papers/w0404"),
    ("HTTP://www.nber.org/papers/w0404", "https://nber.org/papers/w0404"),

    # www stripped
    ("https://www.foo.com/article", "https://foo.com/article"),
    ("https://WWW.slowboring.com/p/hello", "https://slowboring.com/p/hello"),

    # Fragment stripped
    ("https://foo.substack.com/p/bar#comments", "https://foo.substack.com/p/bar"),
    ("https://foo.substack.com/p/bar#comment-42", "https://foo.substack.com/p/bar"),

    # Trailing slash stripped (non-root)
    ("https://foo.substack.com/p/bar/", "https://foo.substack.com/p/bar"),
    ("https://foo.substack.com/p/bar///", "https://foo.substack.com/p/bar"),

    # Trailing slash stripped (root)
    ("https://foo.substack.com/", "https://foo.substack.com"),

    # Whitespace trimmed
    ("  https://foo.substack.com/p/bar  ", "https://foo.substack.com/p/bar"),
    ("\nhttps://foo.substack.com/p/bar\t", "https://foo.substack.com/p/bar"),

    # Scheme-less input assumed https
    ("foo.substack.com/p/bar", "https://foo.substack.com/p/bar"),
])
def test_shape_rules(raw, expected):
    assert canonicalize(raw) == expected


def test_http_and_https_canonicalize_equal():
    # The scheme distinction must never cause a false non-match (D-26).
    assert (canonicalize("http://www.nber.org/papers/w0404")
            == canonicalize("https://nber.org/papers/w0404"))


# --- Tracking parameter stripping -----------------------------------------

@pytest.mark.parametrize("raw, expected", [
    # utm_*
    ("https://foo.substack.com/p/bar?utm_source=email", "https://foo.substack.com/p/bar"),
    ("https://foo.substack.com/p/bar?utm_medium=web&utm_campaign=digest",
     "https://foo.substack.com/p/bar"),
    ("https://foo.substack.com/p/bar?UTM_SOURCE=email", "https://foo.substack.com/p/bar"),

    # ref / ref_src / ref_url
    ("https://foo.substack.com/p/bar?ref=twitter", "https://foo.substack.com/p/bar"),
    ("https://foo.substack.com/p/bar?ref_src=twsrc", "https://foo.substack.com/p/bar"),

    # source
    ("https://foo.substack.com/p/bar?source=digest", "https://foo.substack.com/p/bar"),

    # facebook, google, yandex click ids
    ("https://foo.substack.com/p/bar?fbclid=abc", "https://foo.substack.com/p/bar"),
    ("https://foo.substack.com/p/bar?gclid=xyz", "https://foo.substack.com/p/bar"),
    ("https://foo.substack.com/p/bar?yclid=q", "https://foo.substack.com/p/bar"),

    # mailchimp
    ("https://foo.substack.com/p/bar?mc_cid=1&mc_eid=2", "https://foo.substack.com/p/bar"),

    # twitter t.co
    ("https://foo.substack.com/p/bar?s=20", "https://foo.substack.com/p/bar"),

    # hubspot
    ("https://foo.substack.com/p/bar?_hsenc=1&_hsmi=2", "https://foo.substack.com/p/bar"),

    # Substack-specific junk observed on real Tyler-linked posts (June 2024 scrape)
    ("https://foo.substack.com/p/bar?isFreemail=true", "https://foo.substack.com/p/bar"),
    ("https://foo.substack.com/p/bar?post_id=145873", "https://foo.substack.com/p/bar"),
    ("https://foo.substack.com/p/bar?publication_id=99", "https://foo.substack.com/p/bar"),
    ("https://foo.substack.com/p/bar?triedRedirect=true", "https://foo.substack.com/p/bar"),
    ("https://foo.substack.com/p/bar?r=abc123", "https://foo.substack.com/p/bar"),
    ("https://foo.substack.com/p/bar?isFreemail=true&post_id=1&publication_id=2&r=xyz",
     "https://foo.substack.com/p/bar"),
])
def test_tracking_stripped(raw, expected):
    assert canonicalize(raw) == expected


# --- Non-tracking params preserved and sorted ------------------------------

@pytest.mark.parametrize("raw, expected", [
    # Content-affecting param preserved
    ("https://foo.substack.com/p/bar?page=2", "https://foo.substack.com/p/bar?page=2"),

    # Sorted alphabetically
    ("https://foo.substack.com/p/bar?z=1&a=2&m=3",
     "https://foo.substack.com/p/bar?a=2&m=3&z=1"),

    # Mixed: strip tracking, keep + sort the rest
    ("https://foo.substack.com/p/bar?utm_source=x&page=2&a=1",
     "https://foo.substack.com/p/bar?a=1&page=2"),
])
def test_non_tracking_preserved_and_sorted(raw, expected):
    assert canonicalize(raw) == expected


# --- open.substack.com rewrite --------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("https://open.substack.com/pub/foo/p/bar",
     "https://foo.substack.com/p/bar"),

    ("https://open.substack.com/pub/slow-boring/p/hello-world",
     "https://slow-boring.substack.com/p/hello-world"),

    # Rewrite + tracking strip in the same URL
    ("https://open.substack.com/pub/foo/p/bar?utm_source=email",
     "https://foo.substack.com/p/bar"),

    # Rewrite + fragment strip
    ("https://open.substack.com/pub/foo/p/bar#comments",
     "https://foo.substack.com/p/bar"),

    # open.substack.com but not a pub/p path — leave host alone
    ("https://open.substack.com/browse/culture",
     "https://open.substack.com/browse/culture"),
])
def test_open_substack_rewrite(raw, expected):
    assert canonicalize(raw) == expected


# --- Publication alias map (custom domain → substack host) ----------------

def test_alias_map_folds_custom_domain():
    alias = {"slowboring.com": "slowboring.substack.com"}
    assert canonicalize("https://slowboring.com/p/hello", alias_map=alias) == \
        "https://slowboring.substack.com/p/hello"


def test_alias_map_no_op_when_host_absent():
    alias = {"slowboring.com": "slowboring.substack.com"}
    assert canonicalize("https://someone-else.com/x", alias_map=alias) == \
        "https://someone-else.com/x"


def test_alias_map_after_www_strip():
    """www is stripped BEFORE alias lookup, so map keys need not include www."""
    alias = {"slowboring.com": "slowboring.substack.com"}
    assert canonicalize("https://www.slowboring.com/p/hello", alias_map=alias) == \
        "https://slowboring.substack.com/p/hello"


# --- Idempotence -----------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "https://foo.substack.com/p/bar?utm_source=email",
    "https://open.substack.com/pub/foo/p/bar/#comments",
    "HTTPS://WWW.Slowboring.com/p/hello?a=1&utm_x=1",
    "  foo.substack.com/p/bar/  ",
])
def test_canonicalize_is_idempotent(raw):
    once = canonicalize(raw)
    twice = canonicalize(once)
    assert once == twice, f"non-idempotent: {raw!r} → {once!r} → {twice!r}"


# --- Real-world messy examples (accumulate as we encounter them) ----------

@pytest.mark.parametrize("raw, expected", [
    # Substack email tracker in the wild
    ("https://foo.substack.com/p/bar?utm_source=substack&utm_medium=email",
     "https://foo.substack.com/p/bar"),

    # Case + tracking + trailing slash + fragment all at once
    ("HTTPS://Foo.Substack.com/p/Bar/?utm_source=email#top",
     "https://foo.substack.com/p/Bar"),
])
def test_realworld_examples(raw, expected):
    assert canonicalize(raw) == expected
