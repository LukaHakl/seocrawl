"""Site-level findings added in v1.1.

Covers hero loading, heading furniture, anchor/destination mismatch, the
merged-review fingerprint and the spelling findings.
"""

from __future__ import annotations

import pytest

from seocrawl.crawler import OutLink, PageResult
from seocrawl.findings import HIGH, LOW, MED, Audit

from .test_findings import SITE, audit, find, page


# --- hero loading -----------------------------------------------------------


def test_lazy_hero_is_high():
    """The signature check: same attribute, opposite verdict by role."""
    pages = [page("/products/a", hero_lazy=True, hero_image=SITE + "/img/hero.jpg")]
    result = find(audit(pages).run(), "Hero image is lazy")
    assert result.severity == HIGH
    assert "fetchpriority" in result.why_it_matters


def test_hero_without_fetchpriority_is_low():
    pages = [page("/products/a", hero_image=SITE + "/img/hero.jpg",
                  hero_lazy=False, fetchpriority_present=False)]
    assert find(audit(pages).run(), "without fetchpriority").severity == LOW


def test_correct_hero_produces_no_finding():
    pages = [page("/products/a", hero_image=SITE + "/img/hero.jpg",
                  hero_lazy=False, fetchpriority_present=True)]
    result = audit(pages).run()
    assert find(result, "Hero image") is None
    assert find(result, "fetchpriority") is None


def test_lazy_hero_is_not_also_reported_as_missing_priority():
    """One finding per defect: a lazy hero is the lazy finding, not both."""
    pages = [page("/products/a", hero_image=SITE + "/img/h.jpg", hero_lazy=True)]
    assert find(audit(pages).run(), "without fetchpriority") is None


def test_page_without_a_hero_is_not_reported():
    assert find(audit([page("/a")]).run(), "fetchpriority") is None


def test_eager_belowfold_images_are_med():
    result = find(audit([page("/a", imgs_eager_belowfold_count=4)]).run(),
                  "Below-fold images")
    assert result.severity == MED and "4 below-fold images" in result.detail


def test_large_images_without_srcset_are_low():
    pages = [page("/a", imgs_large_no_srcset=[SITE + "/img/big.jpg"])]
    assert find(audit(pages).run(), "without srcset").severity == LOW


# --- heading furniture ------------------------------------------------------


def test_furniture_headings_finding():
    pages = [page("/products/bifold", unmarked_content_sections=True,
                  furniture_headings=["Featured Collections", "You may also like"])]
    result = find(audit(pages).run(), "Content sections carry no headings")
    assert result.severity == MED
    assert "Featured Collections" in result.detail


def test_no_furniture_finding_when_pages_are_structured():
    assert find(audit([page("/a")]).run(), "Content sections carry no headings") is None


# --- anchor / destination mismatch ------------------------------------------


def _homepage_site(links):
    """A site whose homepage is known, so 'points at the homepage' is decidable."""
    pages = [page("/", discovered_by="start"), page("/products/a")]
    return audit(pages, links)


def test_sale_anchor_pointing_at_homepage_is_med():
    links = [OutLink(SITE + "/products/a", SITE + "/", "Sale", True, False),
             OutLink(SITE + "/products/b", SITE + "/", "Sale", True, False)]
    result = find(_homepage_site(links).run(), "promises a destination")
    assert result.severity == MED
    assert "'Sale' -> homepage on 2 pages" in result.detail


def test_anchor_mismatch_aggregates_by_pair():
    """These live in the template, so the unit is the pair, not the page."""
    links = [OutLink(SITE + "/p%d" % i, SITE + "/", anchor, True, False)
             for i in range(5) for anchor in ("Sale", "Popular")]
    result = find(_homepage_site(links).run(), "promises a destination")
    assert "2 distinct anchor texts" in result.detail
    assert "on 5 pages" in result.detail


def test_discount_anchor_is_flagged():
    links = [OutLink(SITE + "/a", SITE + "/", "20% off everything", True, False)]
    assert find(_homepage_site(links).run(), "promises a destination") is not None


def test_save_and_code_anchors_are_flagged():
    for anchor in ("Save now", "Use code LEATHER10"):
        links = [OutLink(SITE + "/a", SITE + "/", anchor, True, False)]
        assert find(_homepage_site(links).run(), "promises a destination") is not None


def test_home_and_back_anchors_are_exempt():
    """A link that says "Home" and goes home has kept its promise."""
    links = [OutLink(SITE + "/a", SITE + "/", "Home", True, False),
             OutLink(SITE + "/a", SITE + "/", "Back to top", True, False)]
    assert find(_homepage_site(links).run(), "promises a destination") is None


def test_brand_anchor_is_exempt():
    """The logo usually carries the brand name and correctly points home."""
    links = [OutLink(SITE + "/a", SITE + "/", "shop", True, False)]
    assert find(_homepage_site(links).run(), "promises a destination") is None


def test_anchor_pointing_at_a_real_destination_is_fine():
    links = [OutLink(SITE + "/a", SITE + "/collections/sale", "Sale", True, False)]
    assert find(_homepage_site(links).run(), "promises a destination") is None


def test_external_anchors_are_ignored():
    links = [OutLink(SITE + "/a", "https://other.test/", "Sale", False, False)]
    assert find(_homepage_site(links).run(), "promises a destination") is None


def test_empty_anchors_are_ignored():
    links = [OutLink(SITE + "/a", SITE + "/", "", True, False)]
    assert find(_homepage_site(links).run(), "promises a destination") is None


def test_destination_anchor_list_is_configurable():
    links = [OutLink(SITE + "/a", SITE + "/", "Wyprzedaz", True, False)]
    pages = {p.url: p for p in [page("/", discovered_by="start"), page("/a")]}
    custom = Audit(pages, links, destination_anchors=["wyprzedaz"])
    assert find(custom.run(), "promises a destination") is not None


# --- merged review pool -----------------------------------------------------


def _product(path, count, **kwargs):
    """A product page, named after its URL slug unless told otherwise."""
    kwargs.setdefault("product_name", path.rsplit("/", 1)[-1])
    return page(path, is_product=True, schema_types=["Product"],
                review_schema_count=count, **kwargs)


def test_three_distinct_products_sharing_a_count_is_high():
    """A paginated widget cannot explain three products with the same total."""
    pages = [_product("/products/bifold", 1417),
             _product("/products/cardholder", 1417),
             _product("/products/belt", 1417)]
    result = find(audit(pages).run(), "Merged review pool")
    assert result.severity == HIGH
    assert "3 distinct products" in result.detail and "1417" in result.detail
    assert result.count == 3


def test_two_products_sharing_a_count_is_not_enough():
    pages = [_product("/products/a", 1417), _product("/products/b", 1417)]
    assert find(audit(pages).run(), "Merged review pool") is None


def test_variants_of_one_productgroup_do_not_fire():
    """Legitimate variant pooling: one product wearing three URLs."""
    pages = [_product("/products/bifold-black", 1417, product_group_id="TRAD"),
             _product("/products/bifold-natural", 1417, product_group_id="TRAD"),
             _product("/products/bifold-brown", 1417, product_group_id="TRAD")]
    assert find(audit(pages).run(), "Merged review pool") is None


def test_same_product_name_across_urls_counts_once():
    pages = [_product("/products/a", 1417, product_name="Traditional Bifold"),
             _product("/products/b", 1417, product_name="Traditional Bifold"),
             _product("/products/c", 1417, product_name="Traditional Bifold")]
    assert find(audit(pages).run(), "Merged review pool") is None


def test_distinct_counts_do_not_fire():
    pages = [_product("/products/a", 226), _product("/products/b", 619),
             _product("/products/c", 555)]
    assert find(audit(pages).run(), "Merged review pool") is None


def test_merged_pool_lists_the_affected_urls():
    pages = [_product("/products/a", 900), _product("/products/b", 900),
             _product("/products/c", 900)]
    result = find(audit(pages).run(), "Merged review pool")
    assert set(result.urls) == {SITE + "/products/a", SITE + "/products/b",
                                SITE + "/products/c"}


def test_per_page_review_check_still_runs_alongside():
    """The site-level signal is an upgrade, not a replacement."""
    pages = [_product("/products/a", 900, review_suspicious=True,
                      review_reason="schema claims 900 reviews, page renders ~3"),
             _product("/products/b", 900), _product("/products/c", 900)]
    result = audit(pages).run()
    assert find(result, "Merged review pool").severity == HIGH
    assert find(result, "review schema grouping").severity == MED


# --- spelling ---------------------------------------------------------------


def test_serp_typos_are_med():
    pages = [page("/a", spelling_meta=["acheive"]),
             page("/b", spelling_title=["seperate"])]
    result = find(audit(pages).run(), "Typos in title or meta")
    assert result.severity == MED and result.count == 2
    assert "acheive" in result.detail or "seperate" in result.detail


def test_h1_typos_are_med():
    result = find(audit([page("/a", spelling_h1=["definately"])]).run(), "Typos in H1")
    assert result.severity == MED and "definately" in result.detail


def test_body_typos_are_low_and_aggregated():
    pages = [page("/a", spelling_body=["one", "two", "three"], spelling_body_count=3),
             page("/b", spelling_body=["four"], spelling_body_count=1)]
    result = find(audit(pages).run(), "Typos in body")
    assert result.severity == LOW
    assert "4 misspellings across 2 pages" in result.detail


def test_body_typo_detail_shows_five_worst_examples():
    words = ["w%d" % i for i in range(12)]
    pages = [page("/a", spelling_body=words, spelling_body_count=len(words))]
    result = find(audit(pages).run(), "Typos in body")
    assert result.detail.count("'w") == 5


def test_clean_copy_produces_no_spelling_findings():
    assert find(audit([page("/a"), page("/b")]).run(), "Typos") is None


def test_every_v11_finding_carries_a_why_it_matters():
    pages = [page("/products/a", hero_lazy=True, hero_image=SITE + "/h.jpg",
                  unmarked_content_sections=True, furniture_headings=["Share"],
                  spelling_meta=["acheive"], spelling_body=["diffrence"],
                  spelling_body_count=1, imgs_eager_belowfold_count=2,
                  imgs_large_no_srcset=[SITE + "/b.jpg"])]
    for finding in audit(pages).run():
        assert finding.why_it_matters.strip(), finding.issue
        assert finding.severity in (HIGH, MED, LOW)
