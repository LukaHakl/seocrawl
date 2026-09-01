"""End-to-end checks for the two-pass brand whitelist and the v1.1 page fields."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from seocrawl.config import Config
from seocrawl.crawler import Crawler

#: A brand term ("Ritza") on four pages, a real typo ("diffrence") on one, and
#: a one-off capitalized word ("Gorbleflux") that must NOT reach the whitelist.
BODY = """<!doctype html><html lang="en"><head>
<title>Handmade Leather Notebook Covers Built to Last</title>
<meta name="description" content="%(desc)s">
<link rel="canonical" href="http://%(host)s%(path)s"></head>
<body><main><h1>%(h1)s</h1>
<p>Every cover is stitched with Ritza 25 thread on Seidel leather, cut to
fit exactly so the notebook does not shift in a bag during daily carry.</p>
<p>%(extra)s</p>
%(links)s
</main></body></html>"""


def _page(path, desc, h1, extra, links=""):
    return BODY % {"host": "%(host)s", "path": path, "desc": desc,
                   "h1": h1, "extra": extra, "links": links}


SITE = {
    "/robots.txt": "User-agent: *\nAllow: /\n",
    "/": _page("/", "Covers stitched with Ritza thread on Seidel leather, made by hand.",
               "Leather Notebook Covers",
               "Our Ritza thread is waxed and our Seidel hides are vegetable tanned.",
               '<a href="/a">A</a> <a href="/b">B</a> <a href="/c">C</a>'),
    "/a": _page("/a", "A cover stitched with Ritza thread, finished with Seidel leather.",
                "Slim Cover",
                "The Ritza thread and Seidel leather are what make the diffrence here."),
    "/b": _page("/b", "We acheive a perfect fit using Ritza thread and Seidel leather.",
                "Standard Cover",
                "Ritza thread again, and Seidel leather again, on every single cover."),
    "/c": _page("/c", "A pocket cover stitched with Ritza thread on Seidel leather.",
                "Pocket Cover",
                "Here the Ritza thread meets a Gorbleflux finish on Seidel leather."),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = SITE.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        payload = (body % {"host": self.headers.get("Host", "localhost")}).encode()
        self.send_response(200)
        self.send_header("Content-Type",
                         "text/plain" if self.path.endswith(".txt")
                         else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_HEAD = do_GET


@pytest.fixture(scope="module")
def site():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d/" % server.server_port
    server.shutdown()


@pytest.fixture(scope="module")
def crawled(site, tmp_path_factory):
    cache = tmp_path_factory.mktemp("cache")
    crawler = Crawler(Config(start_url=site, delay=0.0, workers=2, use_sitemap=False,
                             cache_dir=cache, recrawl=True,
                             spellcheck_engine="pyspellchecker"))
    results = crawler.run()
    yield crawler, results
    crawler.close()


def test_brand_term_on_enough_pages_is_whitelisted(crawled):
    """"Ritza" and "Seidel" appear on all four pages: brand terms, not typos."""
    crawler, _ = crawled
    assert "ritza" in crawler.brand_terms
    assert "seidel" in crawler.brand_terms


def test_one_off_capitalized_word_is_not_whitelisted(crawled):
    """A word on a single page has not earned the benefit of the doubt."""
    crawler, _ = crawled
    assert "gorbleflux" not in crawler.brand_terms


def test_whitelisted_brand_terms_are_removed_from_findings(crawled):
    """The second pass subtracts brand terms that the first pass flagged."""
    _, results = crawled
    for result in results.values():
        words = [w.lower() for w in
                 result.spelling_title + result.spelling_meta + result.spelling_body]
        assert "ritza" not in words and "seidel" not in words


def test_real_typos_survive_the_whitelist_pass(crawled):
    """Nothing is ever added by the second pass, so genuine typos remain."""
    _, results = crawled
    body_typos = {w.lower() for r in results.values() for w in r.spelling_body}
    meta_typos = {w.lower() for r in results.values() for w in r.spelling_meta}
    assert "diffrence" in body_typos
    assert "acheive" in meta_typos


def test_body_count_is_recomputed_after_filtering(crawled):
    _, results = crawled
    for result in results.values():
        assert result.spelling_body_count == len(result.spelling_body)


def test_site_name_is_whitelisted_without_configuration(site, tmp_path):
    """A shop is not misspelling its own domain."""
    crawler = Crawler(Config(start_url="https://acmeleather.com", delay=0.0,
                             cache_dir=tmp_path / "c", recrawl=True,
                             spellcheck_engine="off"))
    try:
        assert "acmeleather" in crawler.static_whitelist
        assert "acmeleather.com" in crawler.static_whitelist
    finally:
        crawler.close()


def test_user_whitelist_reaches_the_engine(site, tmp_path):
    crawler = Crawler(Config(start_url=site, delay=0.0, cache_dir=tmp_path / "c",
                             recrawl=True, spellcheck_engine="off",
                             spellcheck_whitelist=["Gorbleflux"]))
    try:
        assert "gorbleflux" in crawler.static_whitelist
    finally:
        crawler.close()


def test_spellcheck_can_be_disabled(site, tmp_path):
    crawler = Crawler(Config(start_url=site, delay=0.0, workers=1, use_sitemap=False,
                             cache_dir=tmp_path / "c", recrawl=True, spellcheck=False))
    try:
        results = crawler.run()
        assert all(not r.spelling_body and not r.spelling_meta for r in results.values())
    finally:
        crawler.close()


def test_v11_page_fields_survive_the_resume_cache(crawled, site, tmp_path):
    """New fields must round-trip through sqlite like every other column."""
    from seocrawl.crawler import PageResult

    _, results = crawled
    original = next(iter(results.values()))
    original.hero_lazy = True
    original.furniture_headings = ["Share"]

    restored = PageResult.from_json(original.to_json())
    assert restored.hero_lazy is True
    assert restored.furniture_headings == ["Share"]
    assert restored.spelling_body == original.spelling_body
    assert restored.product_group_id == original.product_group_id
