"""Tests for the site-level findings engine."""

from __future__ import annotations

import pytest

from seocrawl.crawler import OutLink, PageResult
from seocrawl.findings import HIGH, LOW, MED, Audit, anchor_report

SITE = "https://shop.test"


def page(path: str, **kwargs) -> PageResult:
    """A healthy, indexable page, overridable field by field."""
    defaults = dict(
        url=SITE + path,
        status=200,
        final_url=SITE + path,
        canonical=SITE + path,
        canonical_state="self",
        title="Title for %s" % path,
        meta_description="A description for %s that is comfortably long enough." % path,
        h1_count=1,
        word_count=500,
        content_hash="hash%s" % path,
        simhash=hash(path) & ((1 << 64) - 1),
        viewport="width=device-width",
        inlinks=3,
    )
    defaults.update(kwargs)
    return PageResult(**defaults)


def audit(pages, links=(), sitemap=()) -> Audit:
    return Audit({p.url: p for p in pages}, list(links), set(sitemap))


def find(findings, issue_fragment: str):
    """The first finding whose issue contains ``issue_fragment``, or None."""
    return next((f for f in findings if issue_fragment.lower() in f.issue.lower()), None)


# --- structure of the output ------------------------------------------------


def test_every_finding_carries_a_why_it_matters():
    result = audit([page("/a", title="Dup"), page("/b", title="Dup"),
                    page("/c", status=404)]).run()
    assert result
    for finding in result:
        assert finding.why_it_matters.strip(), finding.issue
        assert finding.severity in (HIGH, MED, LOW)
        assert finding.urls


def test_findings_are_sorted_by_severity_then_reach():
    result = audit([page("/a", title="Dup"), page("/b", title="Dup"),
                    page("/c", title_issue="too long"), page("/d", status=500)]).run()
    severities = [f.severity for f in result]
    assert severities == sorted(severities, key=lambda s: {HIGH: 0, MED: 1, LOW: 2}[s])


def test_clean_site_produces_no_findings():
    assert audit([page("/a"), page("/b")]).run() == []


# --- indexability -----------------------------------------------------------


def test_server_errors_are_high():
    result = find(audit([page("/a"), page("/boom", status=503)]).run(), "Server errors")
    assert result.severity == HIGH and result.urls == [SITE + "/boom"]


def test_dead_urls_escalate_when_in_the_sitemap():
    pages = [page("/a"), page("/gone", status=404)]
    assert find(audit(pages).run(), "Dead URLs").severity == MED
    escalated = find(audit(pages, sitemap=[SITE + "/gone"]).run(), "Dead URLs")
    assert escalated.severity == HIGH and "sitemap" in escalated.detail


def test_noindex_on_linked_page_is_high():
    result = find(audit([page("/a"), page("/hidden", noindex=True, inlinks=4)]).run(),
                  "Noindex")
    assert result.severity == HIGH and result.urls == [SITE + "/hidden"]


def test_noindex_finding_names_the_header_variant():
    result = find(audit([page("/h", noindex=True, inlinks=2,
                              x_robots_tag="noindex")]).run(), "Noindex")
    assert "X-Robots-Tag" in result.detail


def test_unlinked_noindex_page_is_not_reported():
    """A deliberately excluded page with no inlinks is intentional, not a bug."""
    assert find(audit([page("/a"), page("/x", noindex=True, inlinks=0)]).run(),
                "Noindex") is None


def test_cross_domain_canonical_is_high():
    result = find(audit([page("/a", canonical_state="cross-domain")]).run(),
                  "another domain")
    assert result.severity == HIGH


# --- duplication ------------------------------------------------------------


def test_duplicate_titles_grouped_with_all_urls():
    pages = [page("/a", title="Wallets"), page("/b", title="Wallets"),
             page("/c", title="Wallets"), page("/d", title="Belts")]
    result = find(audit(pages).run(), "Duplicate title")
    assert result.severity == HIGH and result.count == 3
    assert SITE + "/d" not in result.urls
    assert "3 pages" in result.detail


def test_duplicate_descriptions_are_med():
    pages = [page("/a", meta_description="same"), page("/b", meta_description="same")]
    assert find(audit(pages).run(), "Duplicate meta").severity == MED


def test_identical_content_is_reported():
    pages = [page("/a", content_hash="same"), page("/b", content_hash="same")]
    result = find(audit(pages).run(), "Identical page content")
    assert result.severity == HIGH and result.count == 2


def test_near_duplicate_clustering():
    """Two pages one bit apart cluster; a distant third does not join them."""
    pages = [
        page("/a", simhash=0b1010, content_hash="a"),
        page("/b", simhash=0b1011, content_hash="b"),
        page("/c", simhash=(1 << 63) | 0xFFFF, content_hash="c"),
    ]
    result = find(audit(pages).run(), "Near-duplicate")
    assert result.count == 2 and SITE + "/c" not in result.urls


def test_near_duplicate_ignores_exact_duplicates():
    """Exact duplicates are already reported; they must not be counted twice."""
    pages = [page("/a", content_hash="same", simhash=1),
             page("/b", content_hash="same", simhash=1)]
    assert find(audit(pages).run(), "Near-duplicate") is None


def test_near_duplicate_skips_very_short_pages():
    pages = [page("/a", word_count=10, simhash=7, content_hash="a"),
             page("/b", word_count=10, simhash=7, content_hash="b")]
    assert find(audit(pages).run(), "Near-duplicate") is None


# --- link graph -------------------------------------------------------------


def test_orphan_detection():
    pages = [page("/", discovered_by="start"), page("/lonely", inlinks=0)]
    result = find(audit(pages, sitemap=[SITE + "/", SITE + "/lonely"]).run(), "Orphan")
    assert result.urls == [SITE + "/lonely"]


def test_homepage_is_never_an_orphan():
    pages = [page("/", discovered_by="start", inlinks=0)]
    assert find(audit(pages, sitemap=[SITE + "/"]).run(), "Orphan") is None


def test_ghost_detection():
    pages = [page("/a"), page("/ghost")]
    result = find(audit(pages, sitemap=[SITE + "/a"]).run(), "Ghost")
    assert result.urls == [SITE + "/ghost"]


def test_no_ghosts_reported_without_a_sitemap():
    assert find(audit([page("/a"), page("/b")]).run(), "Ghost") is None


def test_broken_internal_links_name_the_source_page():
    pages = [page("/a"), page("/dead", status=404)]
    links = [OutLink(SITE + "/a", SITE + "/dead", "Buy now", True, False)]
    result = find(audit(pages, links).run(), "Broken internal")
    assert result.severity == HIGH
    assert result.urls == [SITE + "/a"]          # the page you have to edit
    assert "/a -> " in result.detail and "/dead" in result.detail


def test_links_to_redirects_are_flagged():
    pages = [page("/a"), page("/old", redirect_hops=1, final_url=SITE + "/new")]
    links = [OutLink(SITE + "/a", SITE + "/old", "Old", True, False)]
    result = find(audit(pages, links).run(), "pointing at redirects")
    assert result.severity == LOW and result.urls == [SITE + "/a"]


def test_redirect_chain_over_one_hop():
    pages = [page("/old", redirect_hops=2,
                  redirect_chain=["301 -> %s/mid" % SITE, "301 -> %s/new" % SITE])]
    result = find(audit(pages).run(), "Redirect chains")
    assert result.severity == MED and "2 hops" in result.detail


def test_redirect_loop_is_high():
    """A URL that redirects back to itself never resolves."""
    pages = [page("/loop", redirect_hops=2, status=301,
                  redirect_chain=["301 -> %s/mid" % SITE, "301 -> %s/loop" % SITE])]
    result = find(audit(pages).run(), "Redirect loops")
    assert result.severity == HIGH


def test_unresolved_chain_with_a_repeat_is_a_loop():
    pages = [page("/a", redirect_hops=3, status=302,
                  redirect_chain=["302 -> %s/x" % SITE, "302 -> %s/y" % SITE,
                                  "302 -> %s/x" % SITE])]
    assert find(audit(pages).run(), "Redirect loops") is not None


def test_oauth_bounce_that_resolves_is_not_a_loop():
    """Shopify's login bounce revisits its authorize endpoint but ends at 200."""
    pages = [page("/customer_authentication/redirect", status=200, redirect_hops=4,
                  redirect_chain=["302 -> %s/authorize" % SITE,
                                  "302 -> %s/start" % SITE,
                                  "302 -> %s/authorize" % SITE,
                                  "302 -> %s/login" % SITE])]
    assert find(audit(pages).run(), "Redirect loops") is None


# --- hreflang ---------------------------------------------------------------


def _hreflang_pair(reciprocal: bool, x_default: bool = True):
    """An en/de cluster, optionally missing de's return tag.

    x-default points at the (uncrawled) root rather than at /en, so it cannot
    accidentally supply the return reference the test is trying to remove.
    """
    en, de = SITE + "/en", SITE + "/de"
    en_set = [["en", en], ["de", de]]
    de_set = [["de", de], ["en", en]] if reciprocal else [["de", de]]
    if x_default:
        en_set.append(["x-default", SITE + "/"])
        de_set.append(["x-default", SITE + "/"])
    return [
        page("/en", hreflang=en_set, hreflang_x_default=x_default),
        page("/de", hreflang=de_set, hreflang_x_default=x_default),
    ]


def test_hreflang_reciprocity_failure_is_high():
    result = find(audit(_hreflang_pair(reciprocal=False)).run(), "reciprocity")
    assert result.severity == HIGH
    assert result.urls == [SITE + "/en"]
    assert "does not name it back" in result.detail


def test_hreflang_reciprocal_pair_is_clean():
    assert find(audit(_hreflang_pair(reciprocal=True)).run(), "reciprocity") is None


def test_missing_x_default_is_reported():
    result = find(audit(_hreflang_pair(True, x_default=False)).run(), "x-default")
    assert result.severity == MED and result.count == 2


def test_invalid_hreflang_codes_are_reported():
    pages = [page("/a", hreflang=[["english", SITE + "/a"]],
                  hreflang_invalid=["english"], hreflang_x_default=True)]
    result = find(audit(pages).run(), "Invalid hreflang")
    assert "english" in result.detail


def test_hreflang_ignores_uncrawled_targets():
    """A page naming an alternate we never crawled cannot be judged reciprocal."""
    pages = [page("/en", hreflang=[["en", SITE + "/en"], ["fr", "https://other.test/fr"]],
                  hreflang_x_default=True)]
    assert find(audit(pages).run(), "reciprocity") is None


# --- e-commerce -------------------------------------------------------------


def test_price_mismatch_is_high_and_shows_the_numbers():
    pages = [page("/products/bifold", is_product=True, price_consistent=False,
                  prices={"og:price:amount": "179", "jsonld:offers.price": "169"},
                  schema_types=["Product"])]
    result = find(audit(pages).run(), "price signals disagree")
    assert result.severity == HIGH
    assert "179" in result.detail and "169" in result.detail
    assert "Merchant Center" in result.why_it_matters


def test_currency_mismatch_is_high():
    pages = [page("/products/x", is_product=True, currency_consistent=False,
                  schema_types=["Product"])]
    assert find(audit(pages).run(), "currency signals").severity == HIGH


def test_review_schema_flag_is_med_with_policy_wording():
    pages = [page("/products/bifold", is_product=True, schema_types=["Product"],
                  review_suspicious=True, review_schema_count=4187,
                  review_onpage_count=3,
                  review_reason="schema claims 4187 reviews, page renders ~3")]
    result = find(audit(pages).run(), "review schema grouping")
    assert result.severity == MED
    assert "policy risk" in result.issue
    assert "4187" in result.detail


def test_product_page_without_schema_is_flagged():
    pages = [page("/products/naked", schema_types=["WebPage"])]
    result = find(audit(pages).run(), "without Product schema")
    assert result.severity == MED


def test_productgroup_counts_as_product_schema():
    """Shopify emits ProductGroup for variant PDPs; Google reads it as Product."""
    pages = [page("/products/bifold", schema_types=["ProductGroup", "BreadcrumbList"])]
    assert find(audit(pages).run(), "without Product schema") is None


# --- on-page ----------------------------------------------------------------


def test_missing_title_is_high():
    assert find(audit([page("/a", title=None, title_issue="missing")]).run(),
                "Missing title").severity == HIGH


def test_missing_h1_is_med():
    assert find(audit([page("/a", h1_count=0)]).run(), "Missing H1").severity == MED


def test_thin_content_reports_the_site_average():
    pages = [page("/a", word_count=40, thin_content=True), page("/b", word_count=800)]
    result = find(audit(pages).run(), "Thin content")
    assert result.severity == MED and "420" in result.detail


def test_oversized_images_named_with_the_worst_offender():
    pages = [page("/a", images_oversized=["%s/img/hero.jpg (900 KB)" % SITE],
                  largest_image_kb=900)]
    result = find(audit(pages).run(), "Oversized images")
    assert result.severity == MED and "900 KB" in result.detail


def test_missing_viewport_is_high():
    assert find(audit([page("/a", viewport=None)]).run(), "viewport").severity == HIGH


def test_url_hygiene_flags_are_grouped():
    pages = [page("/product-copy", url_flags=["copy-suffix"]),
             page("/Other", url_flags=["uppercase"])]
    result = audit(pages).run()
    assert find(result, "-copy suffix").severity == MED
    assert find(result, "Uppercase").severity == LOW


# --- stats ------------------------------------------------------------------


def test_stats_summarize_the_crawl():
    pages = [page("/a"), page("/b", noindex=True, inlinks=1),
             page("/gone", status=404),
             page("/products/x", is_product=True, schema_types=["Product"])]
    site_audit = audit(pages, sitemap=[SITE + "/a"])
    stats = site_audit.stats()

    assert stats.total_urls == 4
    assert stats.indexable == 2          # /b is noindex, /gone is 404
    assert stats.non_indexable == 2
    assert stats.products == 1
    assert stats.status_counts["2xx"] == 3 and stats.status_counts["4xx"] == 1


def test_stats_issue_rate_is_a_percentage():
    pages = [page("/a", title="Dup"), page("/b", title="Dup"), page("/c"), page("/d")]
    stats = audit(pages).stats()
    assert stats.issue_rate["duplication"] == "50%"


def test_stats_counts_orphans_and_ghosts():
    pages = [page("/", discovered_by="start"), page("/orphan", inlinks=0), page("/ghost")]
    stats = audit(pages, sitemap=[SITE + "/", SITE + "/orphan"]).stats()
    assert stats.orphans == 1 and stats.ghosts == 1


# --- anchors ----------------------------------------------------------------


def test_anchor_report_aggregates_by_anchor_and_target():
    links = [
        OutLink(SITE + "/a", SITE + "/p", "Buy wallet", True, False),
        OutLink(SITE + "/b", SITE + "/p", "Buy wallet", True, False),
        OutLink(SITE + "/c", SITE + "/p", "Shop now", True, False),
        OutLink(SITE + "/a", "https://ext.test/x", "External", False, False),
    ]
    rows = anchor_report(links)
    assert rows[0].anchor == "Buy wallet" and rows[0].count == 2 and rows[0].sources == 2
    assert all("ext.test" not in row.target for row in rows)


def test_anchor_report_labels_empty_anchors():
    rows = anchor_report([OutLink(SITE + "/a", SITE + "/p", "", True, False)])
    assert rows[0].anchor == "(no anchor text)"
