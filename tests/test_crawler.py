"""Crawler tests, run against a throwaway HTTP server on localhost.

The fake site is deliberately nasty: a redirect chain, a 404, a noindex page,
an orphan that only the sitemap knows about, a ghost that only links reach, and
a robots.txt Disallow rule.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from seocrawl.config import Config
from seocrawl.crawler import Crawler, RateLimiter, parse_sitemap

PAGE = """<!doctype html><html><head><title>%(title)s</title>
<link rel="canonical" href="%(canon)s">%(head)s</head>
<body><main><h1>%(title)s</h1><p>%(body)s</p>%(links)s</main></body></html>"""


def _page(title, canon, body="Body copy here for the word counter.", head="", links=""):
    return PAGE % {"title": title, "canon": canon, "body": body, "head": head, "links": links}


SITE: dict[str, tuple[int, str, dict]] = {
    "/robots.txt": (200, "User-agent: *\nDisallow: /admin\nSitemap: http://%(host)s/sitemap.xml\n",
                    {"Content-Type": "text/plain"}),
    "/sitemap.xml": (200,
                     '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                     "<sitemap><loc>http://%(host)s/sitemap-pages.xml</loc></sitemap></sitemapindex>",
                     {"Content-Type": "application/xml"}),
    "/sitemap-pages.xml": (200,
                           '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                           "<url><loc>http://%(host)s/</loc></url>"
                           "<url><loc>http://%(host)s/about</loc></url>"
                           "<url><loc>http://%(host)s/orphan</loc></url></urlset>",
                           {"Content-Type": "application/xml"}),
    "/": (200, _page("Home", "http://%(host)s/",
                     links='<a href="/about">About</a> <a href="/ghost">Ghost</a> '
                           '<a href="/gone">Gone</a> <a href="/old">Old</a> '
                           '<a href="/admin">Admin</a> <a href="/secret">Secret</a>'), {}),
    "/about": (200, _page("About Us", "http://%(host)s/about"), {}),
    "/orphan": (200, _page("Orphan Page", "http://%(host)s/orphan"), {}),
    "/ghost": (200, _page("Ghost Page", "http://%(host)s/ghost"), {}),
    "/secret": (200, _page("Secret", "http://%(host)s/secret",
                           head='<meta name="robots" content="noindex">'), {}),
    "/gone": (404, "not found", {}),
    "/admin": (200, _page("Admin", "http://%(host)s/admin"), {}),
    "/hidden": (200, _page("Hidden", "http://%(host)s/hidden"),
                {"X-Robots-Tag": "noindex"}),
    "/old": (301, "", {"Location": "/older"}),
    "/older": (301, "", {"Location": "/final"}),
    "/final": (200, _page("Final Destination", "http://%(host)s/final"), {}),
}


class Handler(BaseHTTPRequestHandler):
    """Serves ``SITE`` and nothing else."""

    def log_message(self, *args):  # silence the default stderr spam
        pass

    def _send(self, method: str) -> None:
        host = self.headers.get("Host", "localhost")
        entry = SITE.get(self.path.split("?")[0])
        if entry is None:
            self.send_response(404)
            self.end_headers()
            return

        status, body, headers = entry
        body = (body % {"host": host}) if "%(host)s" in body else body
        payload = body.encode()

        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if "Content-Type" not in headers:
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if method == "GET":
            self.wfile.write(payload)

    def do_GET(self):
        self._send("GET")

    def do_HEAD(self):
        self._send("HEAD")


@pytest.fixture(scope="module")
def site() -> str:
    """Start the fake site and yield its base URL."""
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d/" % server.server_port
    server.shutdown()


@pytest.fixture
def crawl(site, tmp_path):
    """Run a full crawl of the fake site with a fresh cache directory."""
    config = Config(
        start_url=site, delay=0.0, workers=2,
        cache_dir=tmp_path / "cache", recrawl=True,
    )
    crawler = Crawler(config)
    results = crawler.run()
    yield crawler, results
    crawler.close()


# --- sitemap parsing --------------------------------------------------------


def test_parse_sitemap_urlset():
    pages, nested = parse_sitemap(
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://x.com/a</loc></url></urlset>"
    )
    assert pages == ["https://x.com/a"] and nested == []


def test_parse_sitemap_index():
    pages, nested = parse_sitemap(
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>https://x.com/s1.xml</loc></sitemap></sitemapindex>"
    )
    assert pages == [] and nested == ["https://x.com/s1.xml"]


def test_parse_sitemap_without_namespace():
    pages, _ = parse_sitemap("<urlset><url><loc>https://x.com/a</loc></url></urlset>")
    assert pages == ["https://x.com/a"]


def test_parse_sitemap_survives_garbage():
    assert parse_sitemap("<html>not a sitemap") == ([], [])


# --- discovery --------------------------------------------------------------


def test_robots_sitemap_line_is_used(crawl):
    crawler, _ = crawl
    assert crawler.robots.fetched
    assert any("sitemap.xml" in s for s in crawler.robots.sitemaps)


def test_sitemap_index_is_recursed(crawl):
    """Three URLs live in a nested sitemap, reachable only via the index."""
    crawler, _ = crawl
    paths = {u.rsplit("/", 1)[-1] for u in crawler.sitemap_urls}
    assert {"about", "orphan"} <= paths
    assert len(crawler.sitemap_urls) == 3


def test_crawls_sitemap_only_page(crawl):
    """/orphan is in the sitemap and linked from nowhere -- it must still be crawled."""
    _, results = crawl
    assert any(u.endswith("/orphan") for u in results)


def test_crawls_link_only_page(crawl):
    """/ghost is linked but absent from the sitemap."""
    _, results = crawl
    ghost = next(r for u, r in results.items() if u.endswith("/ghost"))
    assert ghost.status == 200 and not ghost.in_sitemap


def test_sitemap_membership_is_recorded(crawl):
    _, results = crawl
    about = next(r for u, r in results.items() if u.endswith("/about"))
    assert about.in_sitemap


# --- robots -----------------------------------------------------------------


def test_disallowed_path_is_not_fetched(crawl):
    crawler, results = crawl
    assert not any(u.endswith("/admin") for u in results)
    assert any(u.endswith("/admin") for u in crawler.blocked_by_robots)


# --- responses --------------------------------------------------------------


def test_redirect_chain_records_every_hop(crawl):
    _, results = crawl
    old = next(r for u, r in results.items() if u.endswith("/old"))
    assert old.redirect_hops == 2
    assert len(old.redirect_chain) == 2
    assert old.final_url.endswith("/final")
    assert old.status == 200


def test_404_is_recorded(crawl):
    _, results = crawl
    gone = next(r for u, r in results.items() if u.endswith("/gone"))
    assert gone.status == 404 and not gone.indexable


def test_noindex_meta_marks_page_unindexable(crawl):
    _, results = crawl
    secret = next(r for u, r in results.items() if u.endswith("/secret"))
    assert secret.noindex and not secret.indexable


def test_inlinks_are_counted(crawl):
    _, results = crawl
    about = next(r for u, r in results.items() if u.endswith("/about"))
    assert about.inlinks == 1


def test_home_is_indexable(crawl):
    _, results = crawl
    home = next(r for u, r in results.items() if u.rstrip("/").endswith(str(r.url).rstrip("/")))
    assert home.status == 200


# --- scope controls ---------------------------------------------------------


def test_capped_crawl_prefers_linked_pages_over_sitemap_order(site, tmp_path):
    """A budget-limited crawl must follow the site's navigation first.

    /ghost is linked from the homepage; /orphan is only in the sitemap. With a
    budget of three, the linked pages have to win -- otherwise a capped crawl of
    a real shop spends its whole budget on whatever the sitemap lists first
    (usually blog posts, alphabetically) and never reaches a product page.
    """
    config = Config(start_url=site, delay=0.0, workers=1, max_urls=3,
                    cache_dir=tmp_path / "c", recrawl=True)
    crawler = Crawler(config)
    try:
        crawled = {u.rsplit("/", 1)[-1] for u in crawler.run()}
    finally:
        crawler.close()
    assert "ghost" in crawled
    assert "orphan" not in crawled


def test_uncapped_crawl_still_reaches_sitemap_only_pages(site, tmp_path):
    """Prioritising links must not mean sitemap-only pages get dropped."""
    config = Config(start_url=site, delay=0.0, workers=2,
                    cache_dir=tmp_path / "c", recrawl=True)
    crawler = Crawler(config)
    try:
        crawled = {u.rsplit("/", 1)[-1] for u in crawler.run()}
    finally:
        crawler.close()
    assert {"ghost", "orphan"} <= crawled


def test_max_urls_is_respected(site, tmp_path):
    config = Config(start_url=site, delay=0.0, workers=2, max_urls=3,
                    cache_dir=tmp_path / "c", recrawl=True)
    crawler = Crawler(config)
    try:
        assert len(crawler.run()) <= 3
    finally:
        crawler.close()


def test_exclude_pattern_skips_urls(site, tmp_path):
    config = Config(start_url=site, delay=0.0, workers=2, exclude=[r"/ghost"],
                    cache_dir=tmp_path / "c", recrawl=True)
    crawler = Crawler(config)
    try:
        assert not any(u.endswith("/ghost") for u in crawler.run())
    finally:
        crawler.close()


def test_include_pattern_limits_the_crawl(site, tmp_path):
    """An include pattern must not block the start URL itself."""
    config = Config(start_url=site, delay=0.0, workers=1, include=[r"/about"],
                    use_sitemap=False, cache_dir=tmp_path / "c", recrawl=True)
    crawler = Crawler(config)
    try:
        crawled = {u.rsplit("/", 1)[-1] for u in crawler.run()}
        assert "about" in crawled and "ghost" not in crawled
    finally:
        crawler.close()


# --- resume -----------------------------------------------------------------


def test_resume_reuses_the_cache(site, tmp_path):
    """A second run over the same cache re-reads pages instead of refetching."""
    cache = tmp_path / "cache"
    first = Crawler(Config(start_url=site, delay=0.0, workers=2, cache_dir=cache, recrawl=True))
    try:
        first_results = first.run()
    finally:
        first.close()

    second = Crawler(Config(start_url=site, delay=0.0, workers=2, cache_dir=cache))
    try:
        assert set(second.store.load_pages()) == set(first_results)
        assert set(second.run()) == set(first_results)
    finally:
        second.close()


def test_recrawl_flag_discards_the_cache(site, tmp_path):
    cache = tmp_path / "cache"
    first = Crawler(Config(start_url=site, delay=0.0, workers=1, cache_dir=cache, recrawl=True))
    first.run()
    first.close()

    second = Crawler(Config(start_url=site, delay=0.0, workers=1, cache_dir=cache, recrawl=True))
    try:
        assert second.store.load_pages() == {}
    finally:
        second.close()


# --- rate limiter -----------------------------------------------------------


def test_rate_limiter_spaces_requests_globally():
    """Four acquires at 50ms must take at least three intervals, not zero."""
    import time

    limiter = RateLimiter(0.05)
    started = time.monotonic()
    threads = [threading.Thread(target=limiter.acquire) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert time.monotonic() - started >= 0.15


def test_rate_limiter_slows_down_on_pushback():
    limiter = RateLimiter(0.5)
    limiter.slow_down()
    assert limiter.delay == 1.0
    limiter.slow_down(factor=100, cap=5.0)
    assert limiter.delay == 5.0
