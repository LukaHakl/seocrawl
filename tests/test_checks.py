"""Unit tests for every per-page check in :mod:`seocrawl.checks`."""

from __future__ import annotations

import pytest

from seocrawl import checks
from seocrawl.checks import (
    check_canonical,
    check_content,
    check_headings,
    check_hreflang,
    check_images,
    check_links,
    check_meta_description,
    check_price_consistency,
    check_review_sanity,
    check_robots_directives,
    check_social,
    check_structured_data,
    check_title,
    extract_main_content,
    hamming,
    parse_html,
    parse_jsonld,
)

GOOD_URL = "https://example-shop.com/collections/wallets"
BAD_URL = "https://example-shop.com/shop"
PDP_URL = "https://example-shop.com/products/traditional-bifold"


# --- title / description ----------------------------------------------------


def test_title_good(good_page):
    result = check_title(good_page)
    assert result.text == "Handmade Leather Wallets Built to Last a Lifetime"
    assert not (result.missing or result.too_short or result.too_long)


def test_title_too_short(bad_page):
    result = check_title(bad_page)
    assert result.text == "Shop"
    assert result.too_short and not result.too_long


def test_title_missing():
    result = check_title(parse_html("<html><head></head><body></body></html>"))
    assert result.missing and result.length == 0


def test_title_too_long():
    long_title = "x" * (checks.TITLE_MAX + 1)
    result = check_title(parse_html("<title>%s</title>" % long_title))
    assert result.too_long


def test_meta_description_good(good_page):
    result = check_meta_description(good_page)
    assert not result.missing
    assert checks.META_DESC_MIN <= result.length <= checks.META_DESC_MAX


def test_meta_description_missing(bad_page):
    assert check_meta_description(bad_page).missing


# --- canonical --------------------------------------------------------------


def test_canonical_self_referencing(good_page):
    result = check_canonical(good_page, GOOD_URL)
    assert result.present and result.is_self and not result.is_cross_domain


def test_canonical_cross_domain(bad_page):
    result = check_canonical(bad_page, BAD_URL)
    assert result.present and result.is_cross_domain and not result.is_self


def test_canonical_missing():
    result = check_canonical(parse_html("<html><head></head></html>"), GOOD_URL)
    assert not result.present and result.href is None


def test_canonical_relative_is_resolved():
    soup = parse_html('<link rel="canonical" href="/collections/wallets">')
    assert check_canonical(soup, GOOD_URL).is_self


def test_canonical_ignores_www_and_scheme_noise():
    soup = parse_html('<link rel="canonical" href="https://example-shop.com/a">')
    assert check_canonical(soup, "http://www.example-shop.com/a").is_self


# --- robots -----------------------------------------------------------------


def test_robots_indexable(good_page):
    result = check_robots_directives(good_page)
    assert not result.noindex and not result.nofollow


def test_robots_meta_noindex(bad_page):
    result = check_robots_directives(bad_page)
    assert result.noindex and result.nofollow and result.source == "meta"


def test_robots_header_variant_is_caught(good_page):
    """The X-Robots-Tag header deindexes just as hard as the meta tag."""
    result = check_robots_directives(good_page, {"X-Robots-Tag": "noindex"})
    assert result.noindex and "header" in result.source


def test_robots_none_implies_both():
    result = check_robots_directives(parse_html('<meta name="robots" content="none">'))
    assert result.noindex and result.nofollow


# --- social / viewport ------------------------------------------------------


def test_social_complete(good_page):
    result = check_social(good_page)
    assert result.has_viewport and result.og_title and result.og_image
    assert result.twitter_card == "summary_large_image"
    assert result.missing == ()


def test_social_missing_everything(bad_page):
    result = check_social(bad_page)
    assert set(result.missing) == {"viewport", "og:title", "og:image", "twitter:card"}


# --- hreflang ---------------------------------------------------------------


def test_hreflang_collects_entries_and_x_default(hreflang_page):
    result = check_hreflang(hreflang_page, "https://example-shop.com/pages/care")
    langs = [lang for lang, _ in result.entries]
    assert langs == ["en", "de-DE", "fr-CA", "english", "x-default"]
    assert result.has_x_default and result.has_self_reference


def test_hreflang_flags_invalid_language_code(hreflang_page):
    result = check_hreflang(hreflang_page, "https://example-shop.com/pages/care")
    assert result.invalid_langs == ("english",)


def test_hreflang_ignores_non_hreflang_alternates(hreflang_page):
    """An RSS <link rel=alternate> must not be mistaken for a locale."""
    hrefs = [href for _, href in check_hreflang(hreflang_page, "https://x.com/").entries]
    assert not any("feed.xml" in href for href in hrefs)


def test_hreflang_absent():
    assert not check_hreflang(parse_html("<html></html>"), GOOD_URL).present


# --- headings ---------------------------------------------------------------


def test_headings_good(good_page):
    result = check_headings(good_page)
    assert result.h1_count == 1 and result.h2_count == 2
    assert result.order_violations == ()


def test_headings_multiple_h1(bad_page):
    result = check_headings(bad_page)
    assert result.multiple_h1 and result.h1_count == 2


def test_headings_order_violation_h3_before_h2(bad_page):
    result = check_headings(bad_page)
    assert any("h3" in v for v in result.order_violations)
    assert any("not h1" in v for v in result.order_violations)


def test_headings_missing_h1():
    result = check_headings(parse_html("<h2>Only</h2>"))
    assert result.missing_h1


# --- content ----------------------------------------------------------------


def test_content_prefers_main_block(good_page):
    """Nav and footer links must not land in the content fingerprint."""
    text = extract_main_content(good_page).get_text(" ", strip=True)
    assert "Privacy" not in text and "full-grain leather" in text.lower()


def test_content_word_count_and_thin_flag(good_page, bad_page):
    assert check_content(good_page).word_count > 100
    assert check_content(bad_page).thin


def test_content_hash_is_stable_and_distinct(good_page, bad_page):
    assert check_content(good_page).content_hash == check_content(good_page).content_hash
    assert check_content(good_page).content_hash != check_content(bad_page).content_hash


def test_simhash_detects_near_duplicates(good_page):
    """A one-word body edit must stay inside the near-duplicate distance."""
    original = check_content(good_page)
    edited = check_content(parse_html(str(good_page).replace("patina", "sheen")))
    assert original.content_hash != edited.content_hash  # not an exact duplicate
    assert hamming(original.simhash, edited.simhash) <= checks.NEAR_DUPLICATE_DISTANCE


def test_simhash_separates_unrelated_pages(good_page, bad_page):
    distance = hamming(check_content(good_page).simhash, check_content(bad_page).simhash)
    assert distance > checks.NEAR_DUPLICATE_DISTANCE


# --- images -----------------------------------------------------------------


def test_images_counts_alt_states(good_page):
    result = check_images(good_page, GOOD_URL)
    assert result.count == 3
    assert result.missing_alt == 0
    assert result.empty_alt == 1  # decorative divider, correctly alt=""


def test_images_missing_alt(bad_page):
    result = check_images(bad_page, BAD_URL)
    assert result.missing_alt == 3
    assert "https://example-shop.com/img/a.jpg" in result.missing_alt_sources


def test_images_resolve_srcset_when_src_absent():
    soup = parse_html('<main><img srcset="/img/w_400.jpg 400w, /img/w_800.jpg 800w" alt="x">'
                      + "<p>%s</p></main>" % ("filler " * 40))
    assert check_images(soup, GOOD_URL).sources == ("https://example-shop.com/img/w_400.jpg",)


# --- links ------------------------------------------------------------------


def test_links_classifies_internal_and_external(good_page):
    result = check_links(good_page, GOOD_URL)
    assert result.internal_count == 5  # nav 2, footer 1, body 2
    assert result.external_count == 1
    assert result.nofollow_count == 1


def test_links_skips_mailto_and_fragments(bad_page):
    urls = [link.url for link in check_links(bad_page, BAD_URL).links]
    assert not any(u.startswith("mailto") for u in urls)
    assert not any("#section" in u for u in urls)


def test_links_normalization_dedupes_tracking_params(bad_page):
    """?utm_source links must collapse onto the clean URL."""
    internal = check_links(bad_page, BAD_URL).internal_urls
    assert internal.count("https://example-shop.com/collections/all") == 1


def test_links_anchor_falls_back_to_image_alt():
    soup = parse_html('<a href="/p/x"><img src="/i.jpg" alt="Blue wallet"></a>')
    assert check_links(soup, GOOD_URL).links[0].anchor == "Blue wallet"


def test_links_counts_empty_anchors(bad_page):
    assert check_links(bad_page, BAD_URL).empty_anchor_count == 1


# --- structured data --------------------------------------------------------


def test_jsonld_flattens_graph(good_page):
    types = check_structured_data(parse_jsonld(good_page)).types
    assert "Organization" in types and "BreadcrumbList" in types


def test_jsonld_survives_a_malformed_block(bad_page):
    """One broken script must not cost us the rest of the page."""
    assert parse_jsonld(bad_page) == []


def test_jsonld_extracts_product_offer(product_ok):
    result = check_structured_data(parse_jsonld(product_ok))
    assert result.product_name == "Traditional Bifold Wallet"
    assert result.offer_prices == ("169",)
    assert result.offer_currency == "USD"
    assert result.offer_availability == "InStock"
    assert result.review_count == 38
    assert result.rating_value == "4.9"


def test_jsonld_handles_top_level_array():
    soup = parse_html(
        '<script type="application/ld+json">'
        '[{"@type":"WebPage"},{"@type":"Product","offers":{"price":9.5}}]</script>'
    )
    result = check_structured_data(parse_jsonld(soup))
    assert set(result.types) == {"WebPage", "Product"}
    assert result.offer_prices == ("9.5",)


def test_jsonld_handles_offer_list_and_price_specification():
    soup = parse_html(
        '<script type="application/ld+json">'
        '{"@type":"Product","offers":[{"price":"10.00"},'
        '{"priceSpecification":{"price":"12.00"}}]}</script>'
    )
    assert check_structured_data(parse_jsonld(soup)).offer_prices == ("10", "12")


# --- price consistency (signature check) ------------------------------------


def test_price_consistent_when_all_signals_agree(product_ok):
    result = check_price_consistency(product_ok, check_structured_data(parse_jsonld(product_ok)))
    assert result.is_product and result.consistent
    assert result.distinct_prices == ("169",)


def test_price_mismatch_og_vs_jsonld(product_mismatch):
    """The Merchant Center disapproval predictor: og 179 vs real 169."""
    structured = check_structured_data(parse_jsonld(product_mismatch))
    result = check_price_consistency(product_mismatch, structured)
    assert not result.consistent
    assert result.sources["og:price:amount"] == "179"
    assert result.sources["jsonld:offers.price"] == "169"
    assert set(result.distinct_prices) == {"179", "169"}


def test_price_sale_price_does_not_count_as_mismatch():
    soup = parse_html(
        '<meta property="og:price:standard_amount" content="199.00">'
        '<meta property="product:sale_price:amount" content="149.00">'
        '<meta property="og:price:amount" content="149.00">'
        '<script type="application/ld+json">'
        '{"@type":"Product","offers":{"price":"149.00"}}</script>'
    )
    result = check_price_consistency(soup, check_structured_data(parse_jsonld(soup)))
    assert result.consistent


def test_price_currency_mismatch_is_flagged():
    soup = parse_html(
        '<meta property="og:price:amount" content="10.00">'
        '<meta property="og:price:currency" content="USD">'
        '<script type="application/ld+json">'
        '{"@type":"Product","offers":{"price":"10.00","priceCurrency":"CAD"}}</script>'
    )
    result = check_price_consistency(soup, check_structured_data(parse_jsonld(soup)))
    assert result.consistent and not result.currency_consistent


def test_price_not_a_product_page(good_page):
    result = check_price_consistency(good_page, check_structured_data(parse_jsonld(good_page)))
    assert not result.is_product and result.consistent


def test_price_parses_currency_symbols_and_commas():
    soup = parse_html('<span itemprop="price">$1,299.00</span>')
    result = check_price_consistency(soup, check_structured_data([]))
    assert result.sources["itemprop:price"] == "1299"


# --- review sanity ----------------------------------------------------------


def test_review_sanity_passes_on_plausible_counts(product_mismatch):
    """38 schema reviews against 3 rendered blocks is normal pagination."""
    structured = check_structured_data(parse_jsonld(product_mismatch))
    result = check_review_sanity(product_mismatch, structured, ratio_threshold=20)
    assert not result.suspicious
    assert result.schema_count == 38 and result.onpage_count == 3


def test_review_sanity_flags_merged_review_pool(product_merged_reviews):
    """4187 schema reviews on a page rendering 3 is a merged-reviews setup."""
    structured = check_structured_data(parse_jsonld(product_merged_reviews))
    result = check_review_sanity(product_merged_reviews, structured)
    assert result.suspicious
    assert "4187" in result.reason and result.onpage_count == 3


def test_review_sanity_flags_schema_with_no_rendered_reviews():
    soup = parse_html(
        '<script type="application/ld+json">{"@type":"Product",'
        '"aggregateRating":{"reviewCount":250}}</script><main><h1>P</h1></main>'
    )
    result = check_review_sanity(soup, check_structured_data(parse_jsonld(soup)))
    assert result.suspicious and "no review blocks" in result.reason


def test_review_sanity_ignores_small_counts():
    soup = parse_html(
        '<script type="application/ld+json">{"@type":"Product",'
        '"aggregateRating":{"reviewCount":4}}</script>'
    )
    result = check_review_sanity(soup, check_structured_data(parse_jsonld(soup)))
    assert not result.suspicious


def test_review_sanity_noop_without_schema(good_page):
    structured = check_structured_data(parse_jsonld(good_page))
    assert not check_review_sanity(good_page, structured).suspicious


# --- Shopify ProductGroup / variant markup ----------------------------------


def test_productgroup_variant_offers_are_found(product_group):
    """Prices hang off hasVariant[].offers, not off the group itself."""
    result = check_structured_data(parse_jsonld(product_group))
    assert "ProductGroup" in result.types
    assert result.variant_count == 2
    assert result.offer_prices == ("169",)
    assert result.offer_currency == "CAD"
    assert result.offer_availability == "InStock"


def test_strikethrough_price_is_kept_separate(product_group):
    """A compare-at price must never be read as a current price."""
    result = check_structured_data(parse_jsonld(product_group))
    assert result.list_prices == ("179",)


def test_productgroup_review_count_is_read(product_group):
    result = check_structured_data(parse_jsonld(product_group))
    assert result.review_count == 1417 and result.rating_value == "4.81"


def test_productgroup_is_recognised_as_a_product(product_group):
    structured = check_structured_data(parse_jsonld(product_group))
    assert check_price_consistency(product_group, structured).is_product


def test_variant_price_range_is_summarised():
    """Variants at genuinely different prices report as a range."""
    soup = parse_html(
        '<script type="application/ld+json">{"@type":"ProductGroup","hasVariant":['
        '{"@type":"Product","offers":{"price":"89.00"}},'
        '{"@type":"Product","offers":{"price":"99.00"}}]}</script>'
    )
    structured = check_structured_data(parse_jsonld(soup))
    result = check_price_consistency(soup, structured)
    assert result.sources["jsonld:offers.price"] == "89-99"


def test_variant_prices_alone_are_not_a_mismatch():
    """A product whose variants cost 89 and 99 is not inconsistent."""
    soup = parse_html(
        '<meta property="product:price:amount" content="89.00">'
        '<script type="application/ld+json">{"@type":"ProductGroup","hasVariant":['
        '{"@type":"Product","offers":{"price":"89.00"}},'
        '{"@type":"Product","offers":{"price":"99.00"}}]}</script>'
    )
    structured = check_structured_data(parse_jsonld(soup))
    assert set(structured.offer_prices) == {"89", "99"}
    assert check_price_consistency(soup, structured).consistent


def test_meta_price_outside_every_variant_is_a_mismatch():
    """Advertising 79 when no variant costs 79 is a real disagreement."""
    soup = parse_html(
        '<meta property="product:price:amount" content="79.00">'
        '<script type="application/ld+json">{"@type":"ProductGroup","hasVariant":['
        '{"@type":"Product","offers":{"price":"89.00"}},'
        '{"@type":"Product","offers":{"price":"99.00"}}]}</script>'
    )
    structured = check_structured_data(parse_jsonld(soup))
    assert not check_price_consistency(soup, structured).consistent


def test_strikethrough_alone_does_not_create_a_mismatch():
    """169 current + 179 strikethrough, both advertised, is consistent."""
    soup = parse_html(
        '<meta property="product:price:amount" content="169.00">'
        '<meta property="product:sale_price:amount" content="169.00">'
        '<meta property="og:price:standard_amount" content="179.00">'
        '<script type="application/ld+json">{"@type":"Product","offers":'
        '{"priceSpecification":[{"price":169.0},'
        '{"priceType":"https://schema.org/StrikethroughPrice","price":179.0}]}}</script>'
    )
    structured = check_structured_data(parse_jsonld(soup))
    result = check_price_consistency(soup, structured)
    assert structured.offer_prices == ("169",) and structured.list_prices == ("179",)
    assert result.consistent


def test_the_popov_pattern_is_still_flagged(product_group):
    """The real finding: og:price:standard_amount 179 is the page's only OG
    price, but the wallet sells for 169 and no sale price is declared."""
    structured = check_structured_data(parse_jsonld(product_group))
    result = check_price_consistency(product_group, structured)
    assert not result.consistent
    assert result.sources["og:price:standard_amount"] == "179"
    assert result.sources["product:price:amount"] == "169"


def test_review_flag_fires_on_the_productgroup_pool(product_group):
    """1417 pooled reviews against 3 rendered blocks is the merged-pool shape."""
    structured = check_structured_data(parse_jsonld(product_group))
    review = check_review_sanity(product_group, structured)
    assert review.suspicious
    assert review.schema_count == 1417 and review.onpage_count == 3
