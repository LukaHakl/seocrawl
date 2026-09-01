"""Tests for the spellcheck engines and the two-pass brand whitelist."""

from __future__ import annotations

import pytest

from seocrawl.checks import (
    check_spelling,
    collect_brand_candidates,
    language_matches,
    page_language,
    parse_html,
    spellcheckable_text,
)
from seocrawl.spelling import (
    BUILTIN_ALLOWLIST,
    NullEngine,
    PySpellEngine,
    american_variants,
    build_engine,
    subwords,
    tokenize,
)

from .conftest import load


@pytest.fixture(scope="module")
def engine() -> PySpellEngine:
    """A real pyspellchecker engine (loading the dictionary is slow, so reuse)."""
    return PySpellEngine("en")


# --- tokenizing -------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("We acheive a perfect fit", ["acheive", "perfect", "fit"]),
        ("Ritza 25 thread", ["Ritza", "thread"]),          # bare number dropped
        ("SKU TRAD-BLACK-001", []),                        # acronym + identifier
        ("hello@example.com", []),
        ("Leuchtturm1917 notebook", ["notebook"]),         # digit-bearing token
    ],
)
def test_tokenize_drops_non_words(text, expected):
    assert tokenize(text) == expected


def test_tokenize_keeps_hyphenated_and_apostrophes():
    assert tokenize("don't full-grain") == ["don't", "full-grain"]


def test_tokenize_never_leaks_a_fragment_of_an_alphanumeric_name():
    """"Leuchtturm1917" is dropped whole, not split into a fake typo.

    The word pattern has to swallow the digits, otherwise the token breaks at
    the "1" and the leftover "Leuchtturm" gets reported as a misspelling that
    no whitelist entry can ever match.
    """
    tokens = tokenize("a Leuchtturm1917 cover")
    assert tokens == ["cover"]
    assert not any(t.startswith("Leuchtturm") for t in tokens)


def test_hyphenated_compounds_are_not_typos(engine):
    """Neither half is a typo, so the compound is not one either."""
    assert engine.unknown("A saddle-stitched full-grain hand-cut cover.") == []


def test_hyphenated_compound_with_a_real_typo_still_fires(engine):
    assert engine.unknown("A saddle-stiched cover.") == ["saddle-stiched"]


# --- engines ----------------------------------------------------------------


def test_pyspellchecker_finds_a_typo(engine):
    assert "acheive" in [w.lower() for w in engine.unknown("We acheive a perfect fit.")]


def test_pyspellchecker_passes_correct_prose(engine):
    assert engine.unknown("We achieve a perfect fit for every notebook.") == []


def test_engine_honours_the_builtin_allowlist(engine):
    """Trade vocabulary a general dictionary does not know is not a typo."""
    assert "bifold" in BUILTIN_ALLOWLIST
    assert engine.unknown("A bifold in veg tanned leather with a burnished edge.") == []


def test_engine_honours_a_supplied_allowlist():
    checker = PySpellEngine("en", allowlist=["Leuchtturm1917", "Ritza"])
    assert checker.unknown("A Ritza stitched Leuchtturm1917 cover.") == []


def test_engine_preserves_original_capitalization(engine):
    found = engine.unknown("The Leuchtturm notebook is nice.")
    assert found and found[0][0].isupper()


def test_null_engine_reports_nothing():
    assert NullEngine().unknown("acheive definately seperate") == []


def test_build_engine_off_returns_null():
    built, warning = build_engine("off")
    assert isinstance(built, NullEngine) and warning is None


def test_build_engine_pyspellchecker_is_direct():
    built, warning = build_engine("pyspellchecker", "en")
    assert built.name == "pyspellchecker" and warning is None


def test_build_engine_auto_falls_back_without_java():
    """LanguageTool needs a JVM; auto must degrade rather than fail."""
    built, warning = build_engine("auto", "en")
    assert built.name in ("languagetool", "pyspellchecker")
    if built.name == "pyspellchecker":
        assert warning and "pyspellchecker" in warning


def test_build_engine_unknown_name_disables_with_a_warning():
    built, warning = build_engine("nonsense")
    assert isinstance(built, NullEngine) and "Unknown spellcheck engine" in warning


# --- language gate ----------------------------------------------------------


def test_page_language_is_read():
    assert page_language(load("typos_german.html")) == "de-de"


def test_language_matches_ignores_region():
    assert language_matches(parse_html('<html lang="en-GB"></html>'), "en")


def test_language_matches_allows_undeclared():
    """Themes often omit lang; assume the audit language rather than skip."""
    assert language_matches(parse_html("<html></html>"), "en")


def test_other_language_page_is_skipped(engine):
    result = check_spelling(load("typos_german.html"), engine, lang="en")
    assert result.skipped and not result.any_found
    assert "de-de" in result.skip_reason


# --- brand candidates -------------------------------------------------------


def test_brand_candidates_are_collected():
    candidates = collect_brand_candidates(load("typos.html"))
    assert "Leuchtturm1917" in candidates
    assert "Ritza" in candidates and "Seidel" in candidates


def test_brand_candidates_skip_sentence_openers():
    """A word capitalized only because it starts a sentence is not a brand."""
    soup = parse_html("<main><p>Every cover is cut to fit. Every one is stitched.</p></main>")
    assert "Every" not in collect_brand_candidates(soup)


def test_brand_candidates_are_deduped():
    soup = parse_html("<main><p>We use Ritza thread. Our Ritza thread is waxed.</p></main>")
    assert list(collect_brand_candidates(soup)).count("Ritza") == 1


# --- the check itself -------------------------------------------------------


def test_typo_in_meta_description_fires(engine):
    """The 'acheive' class of error, on the copy shown in the SERP."""
    result = check_spelling(load("typos.html"), engine, lang="en")
    assert any(w.lower() == "acheive" for w in result.meta_description)


def test_typo_in_body_fires(engine):
    result = check_spelling(load("typos.html"), engine, lang="en")
    assert any(w.lower() == "diffrence" for w in result.body)


def test_clean_page_reports_nothing_but_brand_terms(engine):
    """Brand names must not be typos once the whitelist has them."""
    whitelist = ["Leuchtturm1917", "Ritza", "Seidel", "Moleskine"]
    result = check_spelling(load("typo_clean.html"), engine, lang="en", whitelist=whitelist)
    assert not result.any_found


def test_brand_terms_are_not_flagged_when_whitelisted(engine):
    result = check_spelling(load("typos.html"), engine, lang="en",
                            whitelist=["Leuchtturm1917", "Ritza", "Seidel", "Moleskine"])
    body = [w.lower() for w in result.body]
    assert "leuchtturm1917" not in body and "ritza" not in body
    assert "diffrence" in body          # the real typo survives the whitelist


def test_body_misspellings_are_capped(engine):
    from seocrawl.checks import MAX_BODY_MISSPELLINGS

    # Digit-bearing tokens are skipped outright, so build distinct nonsense
    # words from letters only -- otherwise the cap is never exercised.
    letters = "abcdefghijklmnopqrstuvwxyz"
    noise = " ".join("zqx%s%s" % (a, b) for a in letters for b in letters[:4])
    soup = parse_html("<main><p>%s</p></main>" % noise)

    body = check_spelling(soup, engine).body
    assert len(body) == MAX_BODY_MISSPELLINGS      # cap reached, not merely under


def test_disabled_engine_finds_nothing():
    result = check_spelling(load("typos.html"), NullEngine(), lang="en")
    assert not result.any_found and not result.skipped


# --- locale and punctuation false positives ---------------------------------
# Every case below was a false positive found by crawling a real Canadian shop.


@pytest.mark.parametrize(
    "word, american",
    [
        ("colour", "color"),
        ("colours", "colors"),
        ("favour", "favor"),
        ("organise", "organize"),
        ("centre", "center"),
        ("catalogue", "catalog"),
        ("analyse", "analyze"),
        ("moulds", "molds"),
    ],
)
def test_american_variants_are_generated(word, american):
    assert american in american_variants(word)


@pytest.mark.parametrize(
    "text",
    [
        "The colours favour a mould.",
        "Everything is organised in the centre.",
        "Our catalogue moulds to the hand.",
    ],
)
def test_british_spellings_are_not_typos(engine, text):
    """A US dictionary must not report every Canadian and UK shop as sloppy."""
    assert engine.unknown(text) == []


def test_american_spellings_still_pass(engine):
    assert engine.unknown("The colors favor a mold, organized in the center.") == []


def test_british_variant_tolerance_does_not_hide_real_typos(engine):
    """"seperate" has no US variant that rescues it."""
    assert engine.unknown("These are seperate colours.") == ["seperate"]


@pytest.mark.parametrize(
    "token, pieces",
    [
        ("men's", ["men"]),
        ("men’s", ["men"]),          # typographic apostrophe
        ("I've", []),                      # nothing substantial to judge
        ("it's", []),
        ("saddle-stitched", ["saddle", "stitched"]),
        ("doesn't", ["does"]),          # suffix stripped, not split
        ("won't", []),
        ("they'll", ["they"]),
    ],
)
def test_subwords_splits_on_apostrophes_and_hyphens(token, pieces):
    assert subwords(token) == pieces


def test_possessives_are_not_typos(engine):
    assert engine.unknown("Browse men's and women's wallets.") == []


def test_contractions_are_not_typos(engine):
    assert engine.unknown("I’ve used it and it’s great; don’t hesitate.") == []


def test_typographic_and_straight_apostrophes_behave_alike(engine):
    assert engine.unknown("men’s wallets") == engine.unknown("men's wallets")


# --- customer-generated content ---------------------------------------------


def test_review_blocks_are_excluded_from_spellcheck(engine):
    """A customer's typo is not the shop's typo, and cannot be fixed by them."""
    result = check_spelling(load("reviews_with_typos.html"), engine, lang="en")
    body = [w.lower() for w in result.body]
    for customer_typo in ("amazging", "opend", "challanged", "grate", "yeer"):
        assert customer_typo not in body


def test_shop_copy_outside_the_widget_is_still_checked(engine):
    soup = parse_html(
        '<main><p>This is our own copy with a diffrence in it.</p>'
        '<div class="jdgm-rev"><p>A custommer wrote this.</p></div></main>'
    )
    body = [w.lower() for w in check_spelling(soup, engine).body]
    assert "diffrence" in body and "custommer" not in body


def test_spellcheckable_text_drops_the_review_widget():
    text = spellcheckable_text(load("reviews_with_typos.html"))
    assert "burnished by hand" in text
    assert "amazging" not in text


def test_contractions_resolve_to_their_base_word(engine):
    """Splitting "doesn't" on the apostrophe would leave "doesn", not a word."""
    assert engine.unknown("It doesn't fade and won't crack; they'll last.") == []
