"""Tests for the keyword -> URL join."""

from __future__ import annotations

from pathlib import Path

import pytest

from seocrawl.kwmatch import (
    CANNIBAL,
    GAP,
    OWNED,
    WEAK,
    Contender,
    PageTarget,
    TokenInformativeness,
    cluster_gaps,
    load_pages_from_xlsx,
    map_keywords,
    page_type,
    score_page,
    singularize,
    slug_tokens,
    tokenize_target,
)
from seocrawl.kwplanner import KeywordRow, normalize_keyword, parse_volume

S = "https://shop.test"


def keyword(text: str, volume: str = "100 - 1K") -> KeywordRow:
    return KeywordRow(text, normalize_keyword(text), parse_volume(volume))


@pytest.fixture
def pages() -> list[PageTarget]:
    """A small shop: a collection, its product, a sibling, a guide, the home page."""
    return [
        PageTarget(url=S + "/collections/bifold-wallets",
                   title="Bifold Wallets | Full Grain Leather", h1="Bifold Wallets"),
        PageTarget(url=S + "/products/traditional-bifold-leather-wallet",
                   title="Traditional Bifold Leather Wallet",
                   h1="Traditional Bifold Leather Wallet", product_group_id="TRAD"),
        PageTarget(url=S + "/collections/card-holders",
                   title="Leather Card Holders", h1="Card Holders"),
        PageTarget(url=S + "/blogs/guides/how-to-care-for-leather",
                   title="How to Care for Leather", h1="How to Care for Leather"),
        PageTarget(url=S + "/", title="Handmade Leather Goods", h1="Handmade Leather Goods"),
    ]


def status_of(matches, text):
    return next(m for m in matches if m.keyword == text)


# --- tokenizing -------------------------------------------------------------


@pytest.mark.parametrize(
    "word, expected",
    [("wallets", "wallet"), ("wallet", "wallet"), ("holders", "holder"),
     ("men", "men"), ("class", "class"), ("bus", "bus"), ("is", "is")],
)
def test_singularize(word, expected):
    assert singularize(word) == expected


def test_tokenize_strips_stopwords_and_singularizes():
    assert tokenize_target("Buy the Best Leather Wallets for Men") == [
        "best", "leather", "wallet", "men"
    ]


def test_slug_tokens_drop_structural_segments():
    assert slug_tokens(S + "/collections/bifold-wallets") == ["bifold", "wallet"]
    assert slug_tokens(S + "/products/traditional-bifold-leather-wallet") == [
        "traditional", "bifold", "leather", "wallet"
    ]


@pytest.mark.parametrize(
    "url, kind",
    [
        (S + "/collections/wallets", "collection"),
        (S + "/products/bifold", "product"),
        (S + "/blogs/guides/care", "content"),
        (S + "/pages/about", "static"),
        (S + "/", "home"),
        (S + "/something-else", "other"),
    ],
)
def test_page_type(url, kind):
    assert page_type(url) == kind


# --- scoring ----------------------------------------------------------------


def test_slug_outranks_title(pages):
    """A page that declares the term in its URL targets it harder."""
    flat = TokenInformativeness(pages, enabled=False)
    in_slug = PageTarget(url=S + "/collections/leather-wallets", title="Shop")
    in_title = PageTarget(url=S + "/collections/x", title="Leather Wallets")
    tokens = ["leather", "wallet"]
    assert score_page(tokens, in_slug, flat) > score_page(tokens, in_title, flat)


def test_full_slug_match_scores_one(pages):
    flat = TokenInformativeness(pages, enabled=False)
    page = PageTarget(url=S + "/collections/leather-wallets")
    assert score_page(["leather", "wallet"], page, flat) == pytest.approx(1.0)


def test_no_match_scores_zero(pages):
    flat = TokenInformativeness(pages, enabled=False)
    assert score_page(["skateboard"], pages[0], flat) == 0.0


def test_idf_discounts_tokens_every_page_shares(pages):
    """On a leather shop, "leather" separates nothing and should barely count."""
    informative = TokenInformativeness(pages, enabled=True)
    assert informative.weight("leather") < informative.weight("bifold")
    assert informative.weight("minimalist") > informative.weight("leather")


def test_idf_disabled_weighs_every_token_equally(pages):
    flat = TokenInformativeness(pages, enabled=False)
    assert flat.weight("leather") == flat.weight("minimalist") == 1.0


def test_all_generic_tokens_fall_back_to_plain_coverage(pages):
    """Dividing by a zero total weight would crash; coverage is the fallback."""
    informative = TokenInformativeness(pages, enabled=True)
    score = score_page(["leather"], pages[0], informative)
    assert 0.0 <= score <= 1.0


# --- classification ---------------------------------------------------------


def test_owned_keyword(pages):
    result = map_keywords([keyword("leather card holder")], pages)
    match = result.matches[0]
    assert match.status == OWNED
    assert match.owner == S + "/collections/card-holders"
    assert match.owner_page_type == "collection"


def test_cannibalization_lists_every_contender(pages):
    """The fixture case: a collection and its own product both claim the term."""
    result = map_keywords([keyword("leather bifold wallet")], pages)
    match = result.matches[0]
    assert match.status == CANNIBAL
    urls = {c.url for c in match.contenders}
    assert urls == {S + "/collections/bifold-wallets",
                    S + "/products/traditional-bifold-leather-wallet"}


def test_gap_when_nothing_targets_the_modifier(pages):
    """"minimalist" appears on no page, so the demand has no owner."""
    match = map_keywords([keyword("minimalist leather wallet")], pages).matches[0]
    assert match.status == GAP
    assert match.owner is None


def test_weak_partial_coverage():
    pages = [PageTarget(url=S + "/collections/wallets", title="Wallets", h1="Wallets")]
    match = map_keywords([keyword("slim wallet")], pages, use_idf=False).matches[0]
    assert match.status == WEAK
    assert 0.5 <= match.score < 0.75


def test_variants_of_one_product_are_not_cannibalization():
    """Three colours of one wallet compete with nobody -- suppress them."""
    variants = [
        PageTarget(url=S + "/products/bifold-black", title="Bifold Wallet Black",
                   h1="Bifold Wallet", product_group_id="TRAD"),
        PageTarget(url=S + "/products/bifold-natural", title="Bifold Wallet Natural",
                   h1="Bifold Wallet", product_group_id="TRAD"),
        PageTarget(url=S + "/products/bifold-brown", title="Bifold Wallet Brown",
                   h1="Bifold Wallet", product_group_id="TRAD"),
    ]
    match = map_keywords([keyword("bifold wallet")], variants).matches[0]
    assert match.status == OWNED


def test_different_products_sharing_a_term_are_cannibalization():
    rivals = [
        PageTarget(url=S + "/products/bifold-a", title="Bifold Wallet",
                   h1="Bifold Wallet", product_group_id="A"),
        PageTarget(url=S + "/products/bifold-b", title="Bifold Wallet",
                   h1="Bifold Wallet", product_group_id="B"),
    ]
    assert map_keywords([keyword("bifold wallet")], rivals).matches[0].status == CANNIBAL


def test_pages_without_a_product_group_are_not_suppressed(pages):
    """A collection has no ProductGroup id, so it can never be a variant."""
    match = map_keywords([keyword("leather bifold wallet")], pages).matches[0]
    assert match.status == CANNIBAL


# --- inverted targeting -----------------------------------------------------


def test_head_keyword_owned_by_a_product_is_inverted():
    pages = [
        PageTarget(url=S + "/products/leather-wallet", title="Leather Wallet",
                   h1="Leather Wallet"),
    ]
    rows = [keyword("leather wallet", "10K - 100K"), keyword("obscure thing", "10 - 100")]
    match = status_of(map_keywords(rows, pages, use_idf=False).matches, "leather wallet")
    assert match.inverted == "head keyword owned by a product page"


def test_long_tail_owned_by_a_collection_is_inverted():
    pages = [
        PageTarget(url=S + "/collections/slim-leather-wallets-for-men",
                   title="Slim Leather Wallets For Men", h1="Slim Leather Wallets"),
    ]
    match = map_keywords([keyword("slim leather wallet for men")],
                         pages, use_idf=False).matches[0]
    assert match.inverted == "long-tail keyword owned by a collection page"


def test_healthy_targeting_is_not_flagged(pages):
    """Head -> collection is the pattern we want; it must stay silent."""
    match = map_keywords([keyword("leather card holder", "10K - 100K")], pages).matches[0]
    assert match.inverted == ""


def test_gap_is_never_flagged_as_inverted(pages):
    match = map_keywords([keyword("minimalist leather wallet", "10K - 100K")],
                         pages).matches[0]
    assert match.inverted == ""


# --- ordering ---------------------------------------------------------------


def test_matches_are_sorted_gap_first_by_volume(pages):
    rows = [
        keyword("leather card holder", "10K - 100K"),      # OWNED
        keyword("minimalist leather wallet", "100 - 1K"),  # GAP
        keyword("obscure minimalist thing", "10K - 100K"), # GAP, bigger
    ]
    statuses = [m.status for m in map_keywords(rows, pages).matches]
    assert statuses[0] == GAP and statuses[-1] == OWNED

    gaps = [m for m in map_keywords(rows, pages).matches if m.status == GAP]
    assert gaps[0].volume_mid >= gaps[-1].volume_mid


def test_counts_cover_every_status(pages):
    result = map_keywords([keyword("leather card holder")], pages)
    assert set(result.counts) == {OWNED, WEAK, GAP, CANNIBAL}


# --- gap clustering ---------------------------------------------------------


def _gap_matches(texts):
    from seocrawl.kwmatch import KeywordMatch

    return [KeywordMatch(row=keyword(t), status=GAP) for t in texts]


def test_gaps_cluster_on_a_shared_two_token_stem():
    gaps = _gap_matches([
        "minimalist wallet", "minimalist wallet for men", "best minimalist wallet",
        "leather belt", "leather belt for men",
    ])
    clusters = cluster_gaps(gaps)
    heads = {c.head for c in clusters}
    assert "minimalist wallet" in heads and "leather belt" in heads

    minimalist = next(c for c in clusters if c.head == "minimalist wallet")
    assert minimalist.size == 3


def test_clusters_are_sorted_by_combined_volume():
    gaps = [
        *[__import__("seocrawl.kwmatch", fromlist=["KeywordMatch"]).KeywordMatch(
            row=keyword(t, "10K - 100K"), status=GAP)
          for t in ("slim wallet", "slim wallet men")],
        *_gap_matches(["tiny pouch", "tiny pouch small"]),
    ]
    clusters = cluster_gaps(gaps)
    assert clusters[0].head == "slim wallet"
    assert clusters[0].volume > clusters[-1].volume


def test_unclusterable_gaps_become_singletons():
    clusters = cluster_gaps(_gap_matches(["alpha", "beta", "gamma"]))
    assert len(clusters) == 3 and all(c.size == 1 for c in clusters)


def test_cluster_suggested_action_reads_like_a_recommendation():
    clusters = cluster_gaps(_gap_matches(["slim wallet", "slim wallet men"]))
    action = clusters[0].suggested_action()
    assert "no owning URL" in action and "slim wallet" in action
    assert "2 keywords" in action


def test_empty_gap_list_clusters_to_nothing():
    assert cluster_gaps([]) == []


# --- reading a crawl workbook -----------------------------------------------


@pytest.fixture
def audit_workbook(tmp_path):
    """A real audit workbook produced by the exporter."""
    from seocrawl.crawler import PageResult
    from seocrawl.export import write_workbook
    from seocrawl.findings import SiteStats

    pages = [
        PageResult(url=S + "/collections/bifold-wallets", status=200,
                   canonical_state="self", title="Bifold Wallets | Full Grain Leather",
                   h1="Bifold Wallets", inlinks=9),
        PageResult(url=S + "/products/traditional-bifold-leather-wallet", status=200,
                   canonical_state="self", title="Traditional Bifold Leather Wallet",
                   h1="Traditional Bifold Leather Wallet", product_group_id="TRAD"),
        PageResult(url=S + "/collections/card-holders", status=200,
                   canonical_state="self", title="Leather Card Holders", h1="Card Holders"),
        PageResult(url=S + "/blogs/guides/how-to-care-for-leather", status=200,
                   canonical_state="self", title="How to Care for Leather",
                   h1="How to Care for Leather"),
        PageResult(url=S + "/", status=200, canonical_state="self",
                   title="Handmade Leather Goods", h1="Handmade Leather Goods"),
        PageResult(url=S + "/gone", status=404, title="Gone"),
        PageResult(url=S + "/hidden", status=200, canonical_state="self",
                   title="Hidden", noindex=True),
    ]
    path = write_workbook(tmp_path / "audit.xlsx", pages, [], SiteStats())
    return path


def test_pages_load_from_the_audit_workbook(audit_workbook):
    pages = load_pages_from_xlsx(audit_workbook)
    urls = {p.url for p in pages}
    assert S + "/collections/bifold-wallets" in urls
    assert S + "/gone" not in urls          # 404 cannot own a keyword
    assert S + "/hidden" not in urls        # noindex cannot own a keyword


def test_product_group_survives_the_round_trip(audit_workbook):
    pages = load_pages_from_xlsx(audit_workbook)
    product = next(p for p in pages if "/products/" in p.url)
    assert product.product_group_id == "TRAD"


def test_non_indexable_pages_can_be_included(audit_workbook):
    pages = load_pages_from_xlsx(audit_workbook, indexable_only=False)
    assert len(pages) == 7


def test_titles_and_types_are_read(audit_workbook):
    pages = {p.url: p for p in load_pages_from_xlsx(audit_workbook)}
    collection = pages[S + "/collections/bifold-wallets"]
    assert collection.title == "Bifold Wallets | Full Grain Leather"
    assert collection.page_type == "collection"
    assert collection.inlinks == 9


def test_a_workbook_without_pages_is_rejected(tmp_path):
    from openpyxl import Workbook

    path = tmp_path / "not-an-audit.xlsx"
    Workbook().save(path)
    with pytest.raises(ValueError, match="no 'Pages' sheet"):
        load_pages_from_xlsx(path)


def test_end_to_end_against_the_workbook(audit_workbook):
    pages = load_pages_from_xlsx(audit_workbook)
    result = map_keywords(
        [keyword("leather bifold wallet"), keyword("minimalist leather wallet")], pages
    )
    assert status_of(result.matches, "leather bifold wallet").status == CANNIBAL
    assert status_of(result.matches, "minimalist leather wallet").status == GAP


def test_informativeness_never_reaches_zero(pages):
    """A floor keeps a three-token keyword from being decided by one token."""
    informative = TokenInformativeness(pages, enabled=True)
    everywhere = informative.weight("leather")
    assert everywhere >= TokenInformativeness.FLOOR
    assert informative.weight("nowhere-at-all") > everywhere


def test_weights_stay_within_bounds(pages):
    informative = TokenInformativeness(pages, enabled=True)
    for token in ("leather", "wallet", "bifold", "unseen"):
        assert TokenInformativeness.FLOOR <= informative.weight(token) <= 1.0
