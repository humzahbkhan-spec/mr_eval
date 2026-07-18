"""URL canonicalization for the eval pipeline.

This is the highest-risk correctness component in the system: a false non-match
here silently deflates every model's score. Every rule below is covered by
tests in `tests/test_normalize.py` — add a test before adding a rule.

Canonical form:
    - Scheme and host lowercased; `http` folded to `https` (D-26).
    - `www.` subdomain stripped.
    - Tracking query params removed (see `_TRACKING_*` below).
    - Remaining query params sorted alphabetically.
    - Trailing slashes on the path removed.
    - Fragment (anchor) removed.
    - `open.substack.com/pub/{pub}/p/{slug}` rewritten to `https://{pub}.substack.com/p/{slug}`.
    - Optional publication alias map folds custom domains (e.g. `slowboring.com`)
      into their `.substack.com` equivalent — used once we know which publications
      have which custom domains (populated from `build_watchlist.py`).

Redirect following for shortened URLs (t.co, bit.ly, buff.ly, ...) is a
separate concern and lives in `resolve_redirects` — network call, cached.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


# Query parameters treated as noise. Anything not in this set is preserved
# because some publications use query params for real content (pagination,
# filters). Add sparingly, and add a test case when you do.
_TRACKING_PARAM_PREFIXES: tuple[str, ...] = ("utm_",)
_TRACKING_PARAM_EXACT: frozenset[str] = frozenset({
    "ref", "ref_src", "ref_url",
    "source",
    "fbclid", "gclid", "yclid",
    "mc_cid", "mc_eid",
    "igshid",
    "_hsenc", "_hsmi",
    "s",              # twitter's t.co tracker
    # Substack-specific junk: none of these change the underlying post identity.
    # `r` is a subscriber-referrer id; `triedRedirect` an internal flag;
    # `isFreemail` a display-mode hint; `post_id`/`publication_id` duplicate
    # information already in the slug/host. Observed on Tyler-linked posts.
    # Note: set entries must be lowercase; the check lowercases incoming keys.
    "r", "triedredirect", "isfreemail", "post_id", "publication_id",
})

_OPEN_SUBSTACK_PATH = re.compile(r"^/pub/([^/]+)/p/(.+)$")


def canonicalize(url: str, alias_map: dict[str, str] | None = None) -> str:
    """Return a canonical form of `url` suitable for exact-match comparison.

    Pure function — no network. If the URL has no scheme, `https://` is assumed.

    :param url: any URL string; may include leading/trailing whitespace or tracking params.
    :param alias_map: optional `{custom_domain: canonical_substack_host}` mapping to fold
        a publication's custom domain into its `*.substack.com` equivalent. Keys must be
        lowercased and non-`www.` (they're compared against the already-normalized host).
    """
    url = url.strip()
    parts = urlsplit(url)
    if not parts.scheme:
        # Bare host+path; assume https so downstream tooling has a scheme to work with.
        parts = urlsplit("https://" + url)

    scheme = parts.scheme.lower()
    # Treat http and https as the same resource. They virtually never serve
    # different content for the publications we track, and preserving the
    # distinction causes silent false non-matches when an old http-era archive
    # link meets a new https one (e.g. NBER working papers). Deliberate
    # deviation from the brief's literal "lowercase scheme"; see DECISIONS D-26.
    if scheme == "http":
        scheme = "https"
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or ""

    # open.substack.com/pub/foo/p/bar → foo.substack.com/p/bar.
    # Do this before alias-map lookup so slugs are consistent.
    if host == "open.substack.com":
        m = _OPEN_SUBSTACK_PATH.match(path)
        if m:
            host = f"{m.group(1)}.substack.com"
            path = f"/p/{m.group(2)}"

    if alias_map and host in alias_map:
        host = alias_map[host]

    # Strip *all* trailing slashes. Turns `foo.com/` into `foo.com` (empty path),
    # which urlunsplit renders without a trailing slash.
    path = path.rstrip("/")

    query = _strip_tracking(parts.query)

    # Rebuild netloc. Ports only preserved if non-default (rare for Substack/MR).
    netloc = host
    if parts.port:
        default_port = {"http": 80, "https": 443}.get(scheme)
        if parts.port != default_port:
            netloc = f"{host}:{parts.port}"

    return urlunsplit((scheme, netloc, path, query, ""))


def _strip_tracking(query: str) -> str:
    """Remove tracking params from a raw query string; sort what remains."""
    if not query:
        return ""
    kept: list[tuple[str, str]] = []
    for k, v in parse_qsl(query, keep_blank_values=True):
        kl = k.lower()
        if any(kl.startswith(p) for p in _TRACKING_PARAM_PREFIXES):
            continue
        if kl in _TRACKING_PARAM_EXACT:
            continue
        kept.append((k, v))
    kept.sort()
    return urlencode(kept)


def resolve_redirects(url: str, http_client=None, cache=None) -> str:
    """Follow HTTP redirects to a terminal URL, then canonicalize.

    Not implemented for build order §1. Belongs to §5 (ingestion), where we
    also introduce httpx and a persistent cache. Only ever call this for URLs
    known to be shorteners (`t.co`, `bit.ly`, `buff.ly`, ...) — canonicalize()
    first for everything else.
    """
    raise NotImplementedError("resolve_redirects is implemented in ingest.py (step 5)")
