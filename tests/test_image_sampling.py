"""Image-sizing cost controls.

Image HEAD requests dominate crawl time -- on a 449-page site they were ~85% of
it. Two mechanisms cut them, and both must reduce *requests* without changing
what the audit concludes.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from seocrawl.config import Config
from seocrawl.crawler import Crawler
from seocrawl.urls import url_template

#: Every product page shares one theme asset and carries one unique photo, which
#: is exactly the shape that makes the per-URL cache pay.
PRODUCT = """<!doctype html><html lang="en"><head><title>Product %(n)d</title>
<link rel="canonical" href="http://%(host)s/products/p%(n)d"></head>
<body><main><h1>Product %(n)d</h1>
<p>A leather product cut and stitched by hand in our workshop.</p>
<img src="/img/theme-logo.png" alt="Logo" sizes="64px">
<img src="/img/product-%(n)d.jpg" alt="Product %(n)d" sizes="100vw">
</main></body></html>"""

PRODUCT_COUNT = 8

#: Counts every request the fake site receives, by method.
REQUESTS: dict[str, int] = {}
REQUEST_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _count(self, method):
        with REQUEST_LOCK:
            REQUESTS[method] = REQUESTS.get(method, 0) + 1
            REQUESTS[method + " " + self.path] = REQUESTS.get(method + " " + self.path, 0) + 1

    def do_GET(self):
        self._count("GET")
        host = self.headers.get("Host", "localhost")
        path = self.path.split("?")[0]

        if path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            content_type = "text/plain"
        elif path.startswith("/img/"):
            body = b"x" * 400_000        # every image is oversized
            content_type = "image/jpeg"
        elif path == "/":
            links = "".join('<a href="/products/p%d">P%d</a>' % (i, i)
                            for i in range(PRODUCT_COUNT))
            body = ("<!doctype html><html lang=en><head><title>Home</title></head>"
                    "<body><main><h1>Shop</h1>%s</main></body></html>" % links).encode()
            content_type = "text/html; charset=utf-8"
        elif path.startswith("/products/p"):
            number = int(path.rsplit("p", 1)[-1])
            body = (PRODUCT % {"n": number, "host": host}).encode()
            content_type = "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self._count("HEAD")
        size = 400_000 if self.path.startswith("/img/") else 100
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.end_headers()


@pytest.fixture(scope="module")
def site():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d/" % server.server_port
    server.shutdown()


def crawl(site, tmp_path, **overrides):
    """Run a crawl and report ``(results, head_request_count)``."""
    REQUESTS.clear()
    config = Config(start_url=site, delay=0.0, workers=1, use_sitemap=False,
                    cache_dir=tmp_path, recrawl=True, spellcheck=False, **overrides)
    crawler = Crawler(config)
    try:
        results = crawler.run()
    finally:
        crawler.close()
    return results, REQUESTS.get("HEAD", 0)


# --- URL templates ----------------------------------------------------------


@pytest.mark.parametrize(
    "url, template",
    [
        ("https://x.com/", "home"),
        ("https://x.com/products/bifold-natural", "products/*"),
        ("https://x.com/products/trifold-driftwood", "products/*"),
        ("https://x.com/collections/wallets", "collections/*"),
        ("https://x.com/collections/wallets/products/foo", "collections/*/products/*"),
        ("https://x.com/blogs/from-the-workshop/post", "blogs/*/*"),
        ("https://x.com/pages/about", "pages/*"),
    ],
)
def test_url_template(url, template):
    assert url_template(url) == template


def test_sister_pages_share_a_template():
    assert url_template("https://x.com/products/a") == url_template("https://x.com/products/b")


def test_different_sections_do_not_share_a_template():
    assert url_template("https://x.com/products/a") != url_template("https://x.com/collections/a")


# --- the per-URL size cache -------------------------------------------------


def test_shared_asset_is_only_head_requested_once(site, tmp_path):
    """The theme logo appears on every product page; HEAD it once, not N times."""
    crawl(site, tmp_path, image_sample_per_template=0)
    assert REQUESTS.get("HEAD /img/theme-logo.png", 0) == 1


def test_unique_images_are_still_each_measured(site, tmp_path):
    crawl(site, tmp_path, image_sample_per_template=0)
    for number in range(PRODUCT_COUNT):
        assert REQUESTS.get("HEAD /img/product-%d.jpg" % number, 0) == 1


def test_cache_does_not_change_the_findings(site, tmp_path):
    """Caching is an optimisation; every page must still report its images."""
    results, _heads = crawl(site, tmp_path, image_sample_per_template=0)
    products = [r for r in results.values() if "/products/" in r.url]
    assert len(products) == PRODUCT_COUNT
    assert all(r.images_oversized for r in products)
    assert all(r.largest_image_kb > 300 for r in products)


# --- template sampling ------------------------------------------------------


def test_sampling_stops_head_requests_after_the_limit(site, tmp_path):
    _results, heads_full = crawl(site, tmp_path, image_sample_per_template=0)
    _results, heads_sampled = crawl(site, tmp_path, image_sample_per_template=2)
    assert heads_sampled < heads_full


def test_sampled_pages_are_marked(site, tmp_path):
    results, _heads = crawl(site, tmp_path, image_sample_per_template=2)
    products = [r for r in results.values() if "/products/" in r.url]
    sized = [r for r in products if r.images_sized]
    skipped = [r for r in products if not r.images_sized]

    assert len(sized) == 2                      # the template sample
    assert skipped                              # the rest were skipped
    assert all(not r.images_oversized for r in skipped)


def test_free_checks_still_run_on_skipped_pages(site, tmp_path):
    """Only the HEAD requests are skipped; anything readable from the HTML isn't."""
    results, _heads = crawl(site, tmp_path, image_sample_per_template=1)
    skipped = [r for r in results.values()
               if "/products/" in r.url and not r.images_sized]
    assert skipped
    for page in skipped:
        assert page.image_count == 2            # counted from the markup
        assert page.hero_image                  # role classification still ran
        assert page.title and page.word_count


def test_zero_disables_sampling(site, tmp_path):
    results, _heads = crawl(site, tmp_path, image_sample_per_template=0)
    assert all(r.images_sized for r in results.values() if r.status == 200)


def test_sampling_is_per_template_not_global(site, tmp_path):
    """A budget of 1 is spent once per template, not once for the whole crawl.

    The home page and a product page are different templates, so both must be
    measured even though the limit is one.
    """
    results, _heads = crawl(site, tmp_path, image_sample_per_template=1)
    sized = {r.url for r in results.values() if r.images_sized and r.status == 200}

    products = [u for u in sized if "/products/" in u]
    home = [u for u in sized if "/products/" not in u]
    assert len(products) == 1
    assert len(home) == 1


# --- the finding must admit it sampled --------------------------------------


def test_finding_declares_the_sampling(site, tmp_path):
    from seocrawl.findings import Audit

    results, _heads = crawl(site, tmp_path, image_sample_per_template=2)
    audit = Audit(results, [], set())
    finding = next(f for f in audit.run() if f.issue == "Oversized images")
    assert "were not size-checked" in finding.detail
    assert "measured on 2 of" in finding.detail or "measured on 3 of" in finding.detail


def test_finding_is_silent_about_sampling_when_nothing_was_skipped(site, tmp_path):
    from seocrawl.findings import Audit

    results, _heads = crawl(site, tmp_path, image_sample_per_template=0)
    audit = Audit(results, [], set())
    finding = next(f for f in audit.run() if f.issue == "Oversized images")
    assert "not size-checked" not in finding.detail


# --- the point of the exercise ----------------------------------------------


def test_both_mechanisms_together_cut_requests_substantially(site, tmp_path):
    """Cache plus sampling should cost a fraction of the naive request count."""
    naive = PRODUCT_COUNT * 2                   # 2 images on each product page
    _results, heads = crawl(site, tmp_path, image_sample_per_template=2)
    assert heads < naive / 2
