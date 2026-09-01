"""URL normalization and classification helpers.

Everything that decides "are these two URLs the same page?" lives here so the
crawler, the sitemap parser and the findings engine all agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urldefrag, urlsplit, urlunsplit, parse_qsl, urlencode

#: Query parameters that never change the page content -- dropped on normalization.
TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid",
        "_ga", "_gl", "ref", "srsltid", "gad_source",
    }
)

#: File extensions we never want to fetch as HTML pages.
NON_HTML_EXT = frozenset(
    {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif",
        ".pdf", ".zip", ".gz", ".rar", ".7z", ".dmg", ".exe", ".mp4", ".webm",
        ".mp3", ".wav", ".css", ".js", ".json", ".xml", ".rss", ".atom",
        ".woff", ".woff2", ".ttf", ".eot", ".txt",
    }
)


def normalize_url(url: str, *, base: str | None = None, keep_query: bool = True) -> str:
    """Return a canonical string form of ``url``.

    Strips fragments, resolves relative references against ``base``, lowercases the
    scheme and host, drops the default port, removes tracking query parameters and
    sorts what remains, and collapses a trailing slash on non-root paths.
    """
    if base:
        url = urljoin(base, url)
    url, _frag = urldefrag(url.strip())
    parts = urlsplit(url)

    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    if not host:
        return url

    netloc = host
    if parts.port and not (
        (scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443)
    ):
        netloc = f"{host}:{parts.port}"

    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    query = ""
    if keep_query and parts.query:
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k.lower() not in TRACKING_PARAMS]
        query = urlencode(sorted(kept))

    return urlunsplit((scheme, netloc, path, query, ""))


def registrable_host(url: str) -> str:
    """Host with a leading ``www.`` stripped -- used for same-site comparisons."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def same_site(a: str, b: str) -> bool:
    """True when two URLs belong to the same site, ignoring scheme and ``www``."""
    return registrable_host(a) == registrable_host(b)


def dedupe_key(url: str) -> str:
    """Identity key that folds http/https and www/non-www together."""
    parts = urlsplit(normalize_url(url))
    return f"{registrable_host(url)}{parts.path}?{parts.query}"


def is_crawlable(url: str) -> bool:
    """False for mailto:/tel:/javascript: links and obvious binary assets."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    path = parts.path.lower()
    dot = path.rfind(".")
    if dot != -1 and "/" not in path[dot:]:
        return path[dot:] not in NON_HTML_EXT
    return True


def path_segments(url: str) -> list[str]:
    """Non-empty path segments, e.g. ``/a/b/`` -> ``["a", "b"]``."""
    return [s for s in urlsplit(url).path.split("/") if s]


@dataclass(frozen=True)
class UrlHygiene:
    """Result of :func:`check_url_hygiene`."""

    flags: tuple[str, ...]
    depth: int

    @property
    def ok(self) -> bool:
        return not self.flags


def check_url_hygiene(url: str, *, has_canonical: bool = True, max_depth: int = 4) -> UrlHygiene:
    """Flag cosmetic and structural problems in a URL.

    Catches Shopify's ``-copy`` duplicates, uppercase paths, tracking-free query
    strings on pages with no canonical, and paths deeper than ``max_depth``.
    """
    parts = urlsplit(url)
    segments = path_segments(url)
    flags: list[str] = []

    if re.search(r"-copy(-\d+)?(/|$)", parts.path, re.IGNORECASE):
        flags.append("copy-suffix")
    if any(c.isupper() for c in parts.path):
        flags.append("uppercase")
    if parts.query and not has_canonical:
        flags.append("query-params-no-canonical")
    if len(segments) > max_depth:
        flags.append("deep-path")
    if "_" in parts.path:
        flags.append("underscores")
    if len(url) > 115:
        flags.append("long-url")

    return UrlHygiene(flags=tuple(flags), depth=len(segments))


#: Path segments that describe a site's structure rather than one item. Any
#: other segment is an item name and gets collapsed to "*".
STRUCTURAL_SEGMENTS = frozenset(
    {
        "products", "collections", "blogs", "blog", "pages", "policies",
        "account", "cart", "search", "category", "categories", "shop",
        "product", "tag", "tags", "author",
    }
)


def url_template(url: str) -> str:
    """Collapse a URL to the template its page was generated from.

    ``/products/bifold-natural`` and ``/products/trifold-driftwood`` are the
    same Shopify template with different data, so they are the same key. Used
    to avoid repeating expensive per-page work across sister pages that are
    copies of one another.
    """
    segments = path_segments(url)
    if not segments:
        return "home"
    return "/".join(
        segment if segment.lower() in STRUCTURAL_SEGMENTS else "*"
        for segment in segments
    )
