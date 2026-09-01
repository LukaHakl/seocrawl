"""The crawler: discovery, polite concurrent fetching, and per-page analysis.

Discovery merges two sources so neither blind spot survives: the sitemap (which
reveals pages nothing links to) and a BFS link crawl from the start URL (which
reveals pages the sitemap forgot). Fetching is deliberately polite -- robots.txt
is obeyed, a global rate limiter spaces every request regardless of how many
workers are running, and repeated 403s stop the crawl outright.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Iterator

import requests

from .checks import (
    check_canonical,
    check_content,
    check_heading_furniture,
    check_headings,
    check_hreflang,
    check_image_loading,
    check_images,
    check_links,
    check_meta_description,
    check_price_consistency,
    check_review_sanity,
    check_robots_directives,
    check_social,
    check_spelling,
    check_structured_data,
    check_title,
    parse_html,
    parse_jsonld,
)
from .checks import FURNITURE_HEADINGS
from .config import Config
from .spelling import build_engine
from .urls import (
    check_url_hygiene,
    is_crawlable,
    normalize_url,
    path_segments,
    registrable_host,
    same_site,
    url_template,
)


class CrawlBlocked(RuntimeError):
    """Raised when the site is actively refusing us and crawling must stop."""


# --- records ----------------------------------------------------------------


@dataclass
class OutLink:
    """One link found on a page, kept for the inlink/anchor/broken-link passes."""

    source: str
    target: str
    anchor: str
    internal: bool
    nofollow: bool


@dataclass
class PageResult:
    """Flat, JSON-serializable audit record for a single URL.

    Deliberately flat rather than nesting the check dataclasses: this is what
    goes into sqlite, into the resume cache and into one spreadsheet row.
    """

    url: str
    depth: int = 0
    in_sitemap: bool = False
    discovered_by: str = "link"          # "sitemap", "link", "start"

    # response
    status: int | None = None
    final_url: str | None = None
    redirect_chain: list[str] = field(default_factory=list)
    redirect_hops: int = 0
    content_type: str = ""
    response_ms: int = 0
    size_bytes: int = 0
    error: str | None = None

    # head
    title: str | None = None
    title_length: int = 0
    title_issue: str = ""
    meta_description: str | None = None
    meta_description_length: int = 0
    meta_description_issue: str = ""
    canonical: str | None = None
    canonical_state: str = "missing"      # self | cross-domain | other | missing
    noindex: bool = False
    nofollow: bool = False
    robots_directive_source: str | None = None
    x_robots_tag: str | None = None
    viewport: str | None = None
    og_title: str | None = None
    og_image: str | None = None
    twitter_card: str | None = None
    missing_social: list[str] = field(default_factory=list)
    hreflang: list[list[str]] = field(default_factory=list)   # [[lang, href], ...]
    hreflang_x_default: bool = False
    hreflang_invalid: list[str] = field(default_factory=list)

    # headings + content
    h1_count: int = 0
    h1: str = ""
    h2_count: int = 0
    heading_issues: list[str] = field(default_factory=list)
    word_count: int = 0
    thin_content: bool = False
    content_hash: str = ""
    simhash: int = 0

    # images
    image_count: int = 0
    images_missing_alt: int = 0
    images_oversized: list[str] = field(default_factory=list)
    largest_image_kb: int = 0
    images_sized: bool = True        # False when the template sample was full
    hero_image: str | None = None
    hero_lazy: bool = False
    fetchpriority_present: bool = False
    imgs_lazy_count: int = 0
    imgs_eager_belowfold_count: int = 0
    imgs_large_no_srcset: list[str] = field(default_factory=list)

    # headings: template furniture vs. real content sections
    furniture_headings: list[str] = field(default_factory=list)
    content_headings: list[str] = field(default_factory=list)
    unmarked_content_sections: bool = False

    # spelling (filtered against the sitewide brand whitelist after the crawl)
    spelling_title: list[str] = field(default_factory=list)
    spelling_meta: list[str] = field(default_factory=list)
    spelling_h1: list[str] = field(default_factory=list)
    spelling_body: list[str] = field(default_factory=list)
    spelling_body_count: int = 0
    spellcheck_skipped: bool = False
    brand_candidates: list[str] = field(default_factory=list)

    # links
    internal_links: int = 0
    external_links: int = 0
    nofollow_links: int = 0
    inlinks: int = 0                      # filled in after the crawl

    # url hygiene
    url_flags: list[str] = field(default_factory=list)
    path_depth: int = 0

    # structured data
    schema_types: list[str] = field(default_factory=list)
    product_name: str | None = None
    prices: dict[str, str] = field(default_factory=dict)
    price_consistent: bool = True
    currency_consistent: bool = True
    is_product: bool = False
    availability: str | None = None
    product_group_id: str | None = None
    rating_value: str | None = None
    review_schema_count: int | None = None
    review_onpage_count: int = 0
    review_suspicious: bool = False
    review_reason: str | None = None

    @property
    def indexable(self) -> bool:
        """True when this URL is eligible to rank as itself."""
        return (
            self.status == 200
            and not self.noindex
            and self.canonical_state in ("self", "missing")
            and self.error is None
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "PageResult":
        return cls(**json.loads(raw))


# --- politeness -------------------------------------------------------------


class RateLimiter:
    """A global minimum interval between requests, shared by all workers.

    Per-worker sleeping would let a pool of N workers hit the site N times
    faster than the configured delay implies; gating on one shared timestamp
    keeps the real request rate at exactly ``1/delay`` no matter the pool size.
    """

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def acquire(self) -> None:
        """Block until this thread is allowed to issue its request."""
        with self._lock:
            now = time.monotonic()
            wait_for = max(0.0, self._next_slot - now)
            self._next_slot = max(now, self._next_slot) + self.delay
        if wait_for:
            time.sleep(wait_for)

    def slow_down(self, factor: float = 2.0, cap: float = 30.0) -> None:
        """Permanently widen the interval after the server pushes back."""
        with self._lock:
            self.delay = min(self.delay * factor, cap)


class RobotsPolicy:
    """robots.txt rules plus the ``Sitemap:`` lines it advertises."""

    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent
        self._parser: urllib.robotparser.RobotFileParser | None = None
        self.sitemaps: list[str] = []
        self.crawl_delay: float | None = None
        self.fetched = False

    def load(self, session: requests.Session, root: str, timeout: float) -> None:
        """Fetch and parse ``/robots.txt``; a missing file means "allow all"."""
        robots_url = normalize_url("/robots.txt", base=root)
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            response = session.get(robots_url, timeout=timeout)
        except requests.RequestException:
            parser.allow_all = True
            self._parser = parser
            return

        if response.status_code >= 400:
            # 4xx means no restrictions; 5xx conservatively means "stay out",
            # which matches how Googlebot treats an unreachable robots.txt.
            parser.allow_all = response.status_code < 500
            parser.disallow_all = response.status_code >= 500
            self._parser = parser
            return

        parser.parse(response.text.splitlines())
        self._parser = parser
        self.fetched = True
        self.sitemaps = [normalize_url(s, base=root) for s in (parser.site_maps() or [])]

        declared = parser.crawl_delay(self.user_agent)
        if declared:
            self.crawl_delay = float(declared)

    def can_fetch(self, url: str) -> bool:
        """True when robots.txt permits this user agent to fetch ``url``."""
        if self._parser is None:
            return True
        return self._parser.can_fetch(self.user_agent, url)


# --- resume cache -----------------------------------------------------------


class CrawlStore:
    """SQLite-backed cache so a crashed 8k-URL crawl resumes where it stopped."""

    def __init__(self, path, recrawl: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if recrawl and path.exists():
            path.unlink()
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pages (
                url TEXT PRIMARY KEY,
                record TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS links (
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                anchor TEXT,
                internal INTEGER,
                nofollow INTEGER
            );
            CREATE INDEX IF NOT EXISTS links_target ON links(target);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        self._conn.commit()

    def save(self, result: PageResult, links: Iterable[OutLink]) -> None:
        """Persist one page and its outgoing links atomically."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO pages(url, record) VALUES (?, ?)",
                (result.url, result.to_json()),
            )
            self._conn.execute("DELETE FROM links WHERE source = ?", (result.url,))
            self._conn.executemany(
                "INSERT INTO links(source, target, anchor, internal, nofollow) "
                "VALUES (?, ?, ?, ?, ?)",
                [(l.source, l.target, l.anchor, int(l.internal), int(l.nofollow))
                 for l in links],
            )
            self._conn.commit()

    def load_pages(self) -> dict[str, PageResult]:
        """Every page recorded by a previous run of this crawl."""
        with self._lock:
            rows = self._conn.execute("SELECT record FROM pages").fetchall()
        return {r.url: r for r in (PageResult.from_json(row[0]) for row in rows)}

    def load_links(self) -> list[OutLink]:
        """Every outgoing link recorded so far."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, target, anchor, internal, nofollow FROM links"
            ).fetchall()
        return [OutLink(s, t, a or "", bool(i), bool(n)) for s, t, a, i, n in rows]

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
            )
            self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --- sitemaps ---------------------------------------------------------------

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Split one sitemap document into ``(page_urls, nested_sitemap_urls)``.

    Handles both ``<urlset>`` and ``<sitemapindex>`` documents, and tolerates
    feeds served without the sitemap namespace.
    """
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return [], []

    entries: list[str] = []
    for child in root:
        loc = child.find("%sloc" % _SITEMAP_NS)
        if loc is None:
            loc = child.find("loc")
        if loc is not None and loc.text:
            entries.append(loc.text.strip())

    if root.tag.split("}")[-1] == "sitemapindex":
        return [], entries
    return entries, []


# --- the crawler ------------------------------------------------------------


class Crawler:
    """Runs one audit crawl and returns the collected page records."""

    def __init__(self, config: Config, on_progress: Callable[[str, int, int], None] | None = None):
        self.config = config
        self.on_progress = on_progress or (lambda url, done, total: None)

        self.start_url = normalize_url(config.start_url)
        self.robots = RobotsPolicy(config.user_agent)
        self.limiter = RateLimiter(config.delay)

        self.results: dict[str, PageResult] = {}
        self.links: list[OutLink] = []
        self.sitemap_urls: set[str] = set()
        self.blocked_by_robots: set[str] = set()

        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._consecutive_403 = 0
        self._local = threading.local()

        self.furniture_headings = tuple(config.furniture_headings) or FURNITURE_HEADINGS
        self.static_whitelist = self._static_whitelist()
        self.spell_engine, self.spell_warning = (
            build_engine(config.spellcheck_engine, config.lang, self.static_whitelist)
            if config.spellcheck else (build_engine("off")[0], None)
        )
        self.brand_terms: set[str] = set()
        self._image_sizes: dict[str, int | None] = {}
        self._template_sampled: Counter[str] = Counter()
        #: Set to end the crawl early and still write a report from what was
        #: gathered -- a 20-minute crawl needs a stop button.
        self.stop_requested = threading.Event()

        cache_name = "%s.sqlite" % (self._host_slug() or "crawl")
        self.store = CrawlStore(config.cache_dir / cache_name, recrawl=config.recrawl)

    def _static_whitelist(self) -> set[str]:
        """Words never treated as typos, known before the crawl starts.

        The domain name and its parts go in automatically: "acmeleather" and
        "acme" are not misspellings on acmeleather.com, and a site's own name
        is the single most common false positive in a spellcheck.
        """
        host = registrable_host(self.start_url)
        words = {host, host.replace(".", " ")}
        for part in re.split(r"[.\_-]", host):
            if len(part) > 2:
                words.add(part)
        words.update(self.config.spellcheck_whitelist)
        return {w.lower() for w in words if w}

    # --- session handling ---

    def _host_slug(self) -> str:
        return registrable_host(self.start_url).replace(".", "_")

    @property
    def session(self) -> requests.Session:
        """One :class:`requests.Session` per worker thread, for connection reuse."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": self.config.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                }
            )
            self._local.session = session
        return session

    # --- fetching ---

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Issue one rate-limited request, backing off on 429/5xx.

        ``Retry-After`` is honoured when the server sends it; otherwise the wait
        doubles each attempt. A 429 also permanently widens the global interval,
        because the site has told us our steady-state rate is too high.
        """
        last_error: Exception | None = None

        for attempt in range(self.config.max_retries + 1):
            self.limiter.acquire()
            try:
                response = self.session.request(
                    method, url, timeout=self.config.timeout,
                    allow_redirects=False, **kwargs
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.config.max_retries:
                    raise
                time.sleep(min(2 ** attempt, 15))
                continue

            if response.status_code == 403:
                with self._lock:
                    self._consecutive_403 += 1
                    count = self._consecutive_403
                if count >= self.config.forbidden_limit:
                    raise CrawlBlocked(
                        "Stopped after %d consecutive 403 responses -- the site is "
                        "blocking this crawler (WAF, bot protection or an IP block).\n"
                        "Try: a slower --delay, --workers 1, or ask the site owner to "
                        "allowlist the User-Agent:\n  %s" % (count, self.config.user_agent)
                    )
                return response

            with self._lock:
                self._consecutive_403 = 0

            if response.status_code in (429, 503) or response.status_code >= 500:
                if attempt == self.config.max_retries:
                    return response
                retry_after = response.headers.get("Retry-After", "")
                try:
                    pause = float(retry_after)
                except ValueError:
                    pause = min(2 ** attempt, 15)
                if response.status_code == 429:
                    self.limiter.slow_down()
                time.sleep(pause)
                continue

            return response

        raise last_error or requests.RequestException("request failed: %s" % url)

    def _follow(self, url: str) -> tuple[requests.Response, list[str]]:
        """Fetch ``url``, following redirects manually to record every hop."""
        chain: list[str] = []
        current = url

        for _ in range(self.config.max_redirect_hops):
            response = self._request("GET", current)
            if not response.is_redirect and not response.is_permanent_redirect:
                return response, chain

            location = response.headers.get("Location")
            if not location:
                return response, chain

            target = normalize_url(location, base=current)
            chain.append("%d -> %s" % (response.status_code, target))
            if target == current:
                break
            current = target
            response.close()

        # Ran out of hops: report the last response we actually have.
        return response, chain

    # --- discovery ---

    def discover_sitemaps(self) -> set[str]:
        """Collect every URL advertised by the site's sitemaps.

        robots.txt is consulted first for ``Sitemap:`` lines; ``/sitemap.xml`` is
        the fallback. Sitemap indexes are recursed all the way down.
        """
        candidates = list(self.robots.sitemaps)
        if not candidates:
            candidates = [normalize_url("/sitemap.xml", base=self.start_url)]

        found: set[str] = set()
        queue = deque(candidates)
        visited: set[str] = set()

        while queue:
            sitemap_url = queue.popleft()
            if sitemap_url in visited:
                continue
            visited.add(sitemap_url)

            try:
                response = self._request("GET", sitemap_url)
            except (requests.RequestException, CrawlBlocked):
                continue
            if response.status_code != 200 or not response.text.strip():
                continue

            pages, nested = parse_sitemap(response.text)
            queue.extend(normalize_url(n, base=sitemap_url) for n in nested)
            for page in pages:
                url = normalize_url(page, base=sitemap_url)
                if same_site(url, self.start_url) and is_crawlable(url):
                    found.add(url)

        self.sitemap_urls = found
        return found

    # --- analysis ---

    def _image_size(self, src: str) -> int | None:
        """Content-Length of one image, fetched at most once per crawl.

        Theme assets repeat on every page of a site, so without this cache the
        same logo is HEAD-requested several hundred times -- and every one of
        those requests costs a full slot on the global rate limiter.
        """
        with self._lock:
            if src in self._image_sizes:
                return self._image_sizes[src]

        try:
            response = self._request("HEAD", src)
            size: int | None = int(response.headers.get("Content-Length") or 0)
        except (requests.RequestException, ValueError, CrawlBlocked):
            size = None

        with self._lock:
            self._image_sizes[src] = size
        return size

    def _should_size_images(self, url: str) -> bool:
        """False once enough sister pages of this template have been measured."""
        limit = self.config.image_sample_per_template
        if not limit:
            return True
        key = url_template(url)
        with self._lock:
            seen = self._template_sampled[key]
            if seen >= limit:
                return False
            self._template_sampled[key] = seen + 1
        return True

    def _measure_images(self, sources: Iterable[str]) -> tuple[list[str], int]:
        """HEAD the first N images to find ones heavy enough to hurt LCP."""
        oversized: list[str] = []
        largest = 0

        for src in list(sources)[: self.config.image_head_limit]:
            length = self._image_size(src)
            if length is None:
                continue
            largest = max(largest, length)
            if length > self.config.oversized_image_bytes:
                oversized.append("%s (%d KB)" % (src, length // 1024))

        return oversized, largest // 1024

    def analyze(self, result: PageResult, html: str, headers: dict[str, str]) -> list[OutLink]:
        """Run every per-page check and fill ``result`` in place.

        Returns the page's outgoing links, which the caller both queues for
        crawling and stores for the sitewide inlink/anchor/broken-link passes.
        """
        soup = parse_html(html)
        page_url = result.final_url or result.url

        title = check_title(soup)
        result.title = title.text
        result.title_length = title.length
        result.title_issue = (
            "missing" if title.missing else
            "too short" if title.too_short else
            "too long" if title.too_long else ""
        )

        desc = check_meta_description(soup)
        result.meta_description = desc.text
        result.meta_description_length = desc.length
        result.meta_description_issue = (
            "missing" if desc.missing else
            "too short" if desc.too_short else
            "too long" if desc.too_long else ""
        )

        canonical = check_canonical(soup, page_url)
        result.canonical = canonical.href
        result.canonical_state = (
            "missing" if not canonical.present else
            "cross-domain" if canonical.is_cross_domain else
            "self" if canonical.is_self else "other"
        )

        robots = check_robots_directives(soup, headers)
        result.noindex = robots.noindex
        result.nofollow = robots.nofollow
        result.robots_directive_source = robots.source
        result.x_robots_tag = robots.header_content

        social = check_social(soup)
        result.viewport = social.viewport
        result.og_title = social.og_title
        result.og_image = social.og_image
        result.twitter_card = social.twitter_card
        result.missing_social = list(social.missing)

        hreflang = check_hreflang(soup, page_url)
        result.hreflang = [[lang, href] for lang, href in hreflang.entries]
        result.hreflang_x_default = hreflang.has_x_default
        result.hreflang_invalid = list(hreflang.invalid_langs)

        headings = check_headings(soup)
        result.h1_count = headings.h1_count
        result.h1 = headings.h1_texts[0] if headings.h1_texts else ""
        result.h2_count = headings.h2_count
        result.heading_issues = list(headings.order_violations)

        content = check_content(soup)
        result.word_count = content.word_count
        result.thin_content = content.thin
        result.content_hash = content.content_hash
        result.simhash = content.simhash

        images = check_images(soup, page_url)
        result.image_count = images.count
        result.images_missing_alt = images.missing_alt
        if self._should_size_images(result.url):
            oversized, largest_kb = self._measure_images(images.sources)
            result.images_oversized = oversized
            result.largest_image_kb = largest_kb
        else:
            # Sister page of an already-sampled template: the alt-text and role
            # checks above are free from the HTML, only the sizing is skipped.
            result.images_sized = False

        loading = check_image_loading(soup, page_url)
        result.hero_image = loading.hero.src if loading.hero else None
        result.hero_lazy = loading.hero_lazy
        result.fetchpriority_present = loading.fetchpriority_present
        result.imgs_lazy_count = loading.lazy_count
        result.imgs_eager_belowfold_count = loading.eager_belowfold_count
        result.imgs_large_no_srcset = list(loading.large_without_srcset)

        furniture = check_heading_furniture(soup, self.furniture_headings)
        result.furniture_headings = list(furniture.furniture)
        result.content_headings = list(furniture.content_headings)
        result.unmarked_content_sections = furniture.unmarked_content

        spelling = check_spelling(
            soup, self.spell_engine, lang=self.config.lang, whitelist=self.static_whitelist
        )
        result.spelling_title = list(spelling.title)
        result.spelling_meta = list(spelling.meta_description)
        result.spelling_h1 = list(spelling.h1)
        result.spelling_body = list(spelling.body)
        result.spelling_body_count = len(spelling.body)
        result.spellcheck_skipped = spelling.skipped
        result.brand_candidates = list(spelling.brand_candidates)

        link_check = check_links(soup, page_url)
        result.internal_links = link_check.internal_count
        result.external_links = link_check.external_count
        result.nofollow_links = link_check.nofollow_count

        hygiene = check_url_hygiene(result.url, has_canonical=canonical.present)
        result.url_flags = list(hygiene.flags)
        result.path_depth = hygiene.depth

        nodes = parse_jsonld(soup)
        structured = check_structured_data(nodes)
        result.schema_types = list(structured.types)
        result.product_name = structured.product_name
        result.availability = structured.offer_availability
        result.product_group_id = structured.product_group_id
        result.rating_value = structured.rating_value

        price = check_price_consistency(soup, structured)
        result.prices = dict(price.sources)
        result.price_consistent = price.consistent
        result.currency_consistent = price.currency_consistent
        result.is_product = price.is_product

        review = check_review_sanity(soup, structured)
        result.review_schema_count = review.schema_count
        result.review_onpage_count = review.onpage_count
        result.review_suspicious = review.suspicious
        result.review_reason = review.reason

        return [
            OutLink(result.url, link.url, link.anchor, link.internal, link.nofollow)
            for link in link_check.links
        ]

    def _process(self, url: str, depth: int, discovered_by: str) -> tuple[PageResult, list[OutLink]]:
        """Fetch and audit one URL. Never raises for ordinary HTTP failures."""
        result = PageResult(
            url=url,
            depth=depth,
            in_sitemap=url in self.sitemap_urls,
            discovered_by=discovered_by,
        )

        started = time.monotonic()
        try:
            response, chain = self._follow(url)
        except CrawlBlocked:
            raise
        except requests.RequestException as exc:
            result.error = type(exc).__name__
            return result, []

        result.response_ms = int((time.monotonic() - started) * 1000)
        result.status = response.status_code
        result.redirect_chain = chain
        result.redirect_hops = len(chain)
        result.final_url = normalize_url(str(response.url))
        result.content_type = (response.headers.get("Content-Type") or "").split(";")[0]
        result.size_bytes = len(response.content or b"")

        headers = dict(response.headers)
        is_html = "html" in result.content_type.lower()
        if response.status_code == 200 and is_html:
            try:
                links = self.analyze(result, response.text, headers)
            except Exception as exc:  # a single malformed page must not kill the crawl
                result.error = "parse error: %s" % exc
                links = []
            return result, links

        # Non-HTML or error responses still get the header-level robots check.
        robots = check_robots_directives(parse_html(""), headers)
        result.noindex = robots.noindex
        result.x_robots_tag = robots.header_content
        return result, []

    # --- the run loop ---

    def _queue_seeds(self) -> tuple[deque[tuple[str, int, str]], deque[tuple[str, int, str]]]:
        """Build the two frontiers: link-discovered (priority) and sitemap-only.

        They are kept separate because a crawl capped by ``--max-urls`` should
        spend its budget on the pages the site itself navigates to. Draining a
        single merged queue means whatever the sitemap happens to list first --
        usually blog posts, alphabetically -- eats the whole budget before the
        crawler ever reaches a collection or a product.

        Sitemap-only URLs are ordered shallowest-first so that when the budget
        does reach them, it starts with the structurally important ones.
        """
        links: deque[tuple[str, int, str]] = deque()
        links.append((self.start_url, 0, "start"))
        self._seen.add(self.start_url)

        sitemap_only = sorted(
            (u for u in self.sitemap_urls if u not in self._seen and self.config.allows(u)),
            key=lambda u: (len(path_segments(u)), u),
        )
        sitemap: deque[tuple[str, int, str]] = deque(
            (url, 1, "sitemap") for url in sitemap_only
        )
        self._seen.update(url for url, _, _ in sitemap)
        return links, sitemap

    def _should_queue(self, url: str, depth: int) -> bool:
        """Scope gate: same site, crawlable, in-pattern, robots-allowed, in-depth."""
        if url in self._seen:
            return False
        if not same_site(url, self.start_url) or not is_crawlable(url):
            return False
        if not self.config.allows(url):
            return False
        if self.config.max_depth and depth > self.config.max_depth:
            return False
        if self.config.respect_robots and not self.robots.can_fetch(url):
            self.blocked_by_robots.add(url)
            return False
        return True

    def run(self) -> dict[str, PageResult]:
        """Execute the crawl and return every audited page, keyed by URL."""
        config = self.config

        self.robots.load(self.session, self.start_url, config.timeout)
        # A crawl-delay in robots.txt outranks our default, never loosens it.
        if self.robots.crawl_delay and self.robots.crawl_delay > self.limiter.delay:
            self.limiter.delay = self.robots.crawl_delay

        if config.use_sitemap:
            self.discover_sitemaps()

        cached = {} if config.recrawl else self.store.load_pages()
        if cached:
            self.results.update(cached)
            self.links.extend(self.store.load_links())
            self._seen.update(cached)
            for url, record in cached.items():
                record.in_sitemap = url in self.sitemap_urls

        link_frontier, sitemap_frontier = self._queue_seeds()
        for frontier in (link_frontier, sitemap_frontier):
            already_done = [item for item in frontier if item[0] in self.results]
            for item in already_done:
                frontier.remove(item)

        def next_url() -> tuple[str, int, str] | None:
            """Link-discovered URLs first; sitemap-only URLs with what is left."""
            if link_frontier:
                return link_frontier.popleft()
            if sitemap_frontier:
                return sitemap_frontier.popleft()
            return None

        done = len(self.results)
        with ThreadPoolExecutor(max_workers=config.workers) as pool:
            pending: dict[Any, tuple[str, int]] = {}

            while ((link_frontier or sitemap_frontier or pending)
                   and done < config.max_urls
                   and not self.stop_requested.is_set()):
                while len(pending) < config.workers * 2 and \
                        done + len(pending) < config.max_urls:
                    item = next_url()
                    if item is None:
                        break
                    url, depth, source = item
                    if config.respect_robots and not self.robots.can_fetch(url):
                        self.blocked_by_robots.add(url)
                        continue
                    pending[pool.submit(self._process, url, depth, source)] = (url, depth)

                if not pending:
                    break

                finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for future in finished:
                    url, depth = pending.pop(future)
                    try:
                        result, links = future.result()
                    except CrawlBlocked:
                        for other in pending:
                            other.cancel()
                        raise

                    with self._lock:
                        self.results[url] = result
                        self.links.extend(links)
                    self.store.save(result, links)
                    done += 1
                    self.on_progress(url, done, min(config.max_urls, len(self._seen)))

                    if not config.follow_links:
                        continue
                    for link in links:
                        if link.internal and self._should_queue(link.target, depth + 1):
                            self._seen.add(link.target)
                            link_frontier.append((link.target, depth + 1, "link"))

        self._apply_spelling_whitelist()
        self._resolve_inlinks()
        self.store.set_meta("last_run", str(time.time()))
        return self.results

    def _apply_spelling_whitelist(self) -> None:
        """Second spellcheck pass: subtract brand terms learned from the crawl.

        A capitalized mid-sentence word that shows up on several pages is a
        proper noun, not a typo -- "Leuchtturm1917", "Ritza", "Seidel". The
        first pass could not know that, because a term only earns the benefit of
        the doubt once the whole site has been seen. Nothing is ever *added*
        here, so a real typo cannot be introduced by this pass.
        """
        appearances: Counter[str] = Counter()
        for result in self.results.values():
            for term in set(result.brand_candidates):
                appearances[term.lower()] += 1

        self.brand_terms = {
            term for term, count in appearances.items()
            if count >= self.config.brand_term_min_pages
        }
        allow = self.brand_terms | self.static_whitelist

        for result in self.results.values():
            for attribute in ("spelling_title", "spelling_meta",
                              "spelling_h1", "spelling_body"):
                kept = [w for w in getattr(result, attribute) if w.lower() not in allow]
                setattr(result, attribute, kept)
            result.spelling_body_count = len(result.spelling_body)

    def _resolve_inlinks(self) -> None:
        """Count internal inlinks per page, ignoring a page's links to itself."""
        counts: dict[str, int] = {}
        for link in self.links:
            if link.internal and link.target != link.source:
                counts[link.target] = counts.get(link.target, 0) + 1
        for url, result in self.results.items():
            result.inlinks = counts.get(url, 0)

    def request_stop(self) -> None:
        """Ask the crawl to finish early. In-flight requests still complete."""
        self.stop_requested.set()

    def close(self) -> None:
        self.store.close()
        closer = getattr(self.spell_engine, "close", None)
        if callable(closer):
            closer()
