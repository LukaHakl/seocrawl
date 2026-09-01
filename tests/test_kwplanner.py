"""Tests for Keyword Planner ingestion -- the export is hostile by default."""

from __future__ import annotations

from pathlib import Path

import pytest

from seocrawl.kwplanner import (
    KeywordFileError,
    clean_keywords,
    decode_file,
    find_header_row,
    load_gkp,
    map_columns,
    normalize_keyword,
    parse_competition,
    parse_number,
    parse_volume,
    sniff_delimiter,
)

FIXTURES = Path(__file__).parent / "fixtures"
SLOVENIAN = FIXTURES / "gkp_slovenian.csv"
ENGLISH = FIXTURES / "gkp_english.csv"


# --- numbers ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1300", 1300.0),
        ("1,300", 1300.0),          # English thousands
        ("1.300", 1300.0),          # Slovenian thousands
        ("1,5", 1.5),               # Slovenian decimal
        ("1.5", 1.5),
        ("0,35", 0.35),
        ("1.234,56", 1234.56),      # both separators, comma decimal
        ("1,234.56", 1234.56),      # both separators, dot decimal
        ("€ 1,20", 1.2),
        ("", None),
        ("+∞", None),
        ("-", None),
        (None, None),
        ("n/a", None),
    ],
)
def test_parse_number(text, expected):
    assert parse_number(text) == expected


# --- volumes ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text, minimum, maximum",
    [
        ("10 - 100", 10, 100),
        ("10 – 100", 10, 100),            # en dash
        ("10 — 100", 10, 100),            # em dash
        ("100 - 1 tis.", 100, 1000),
        ("1 tis. - 10 tis.", 1000, 10000),
        ("10 tis. - 100 tis.", 10000, 100000),
        ("1K - 10K", 1000, 10000),
        ("10K - 100K", 10000, 100000),
        ("1M - 10M", 1_000_000, 10_000_000),
        ("1 mio. - 10 mio.", 1_000_000, 10_000_000),
    ],
)
def test_parse_volume_buckets(text, minimum, maximum):
    volume = parse_volume(text)
    assert (volume.minimum, volume.maximum) == (minimum, maximum)
    assert volume.parsed and volume.is_range


def test_volume_mid_is_the_geometric_mean():
    """A 1k-10k bucket far more often holds 3k than the arithmetic 5.5k."""
    assert parse_volume("1 tis. - 10 tis.").mid == 3162
    assert parse_volume("10 - 100").mid == 32


def test_plain_integer_volume():
    volume = parse_volume("1,300")
    assert (volume.minimum, volume.maximum, volume.mid) == (1300, 1300, 1300)
    assert not volume.is_range


def test_unparseable_volume_is_zero_not_a_crash():
    volume = parse_volume("lots and lots")
    assert volume.mid == 0 and not volume.parsed


def test_blank_volume_is_zero():
    assert parse_volume("").mid == 0
    assert parse_volume(None).mid == 0


def test_localized_and_english_buckets_agree():
    assert parse_volume("1 tis. - 10 tis.").mid == parse_volume("1K - 10K").mid


# --- competition ------------------------------------------------------------


@pytest.mark.parametrize(
    "text, level",
    [
        ("Low", "LOW"), ("Medium", "MED"), ("High", "HIGH"),
        ("Šibka", "LOW"), ("Srednja", "MED"), ("Močna", "HIGH"),
        ("sibka", "LOW"), ("mocna", "HIGH"),          # unaccented
        # Neuter forms, which is what real exports actually emit.
        ("Visoko", "HIGH"), ("Nizko", "LOW"), ("Srednje", "MED"),
        ("Visoka", "HIGH"), ("Nizka", "LOW"),
        ("Neznano", None), ("Unknown", None),
        ("10", "LOW"), ("50", "MED"), ("90", "HIGH"),  # indexed value
        ("", None), (None, None),
    ],
)
def test_parse_competition(text, level):
    assert parse_competition(text) == level


# --- file reading -----------------------------------------------------------


def test_utf16_is_detected_by_bom():
    text, encoding = decode_file(SLOVENIAN)
    assert encoding == "utf-16"
    assert "Ključna beseda" in text


def test_utf8_bom_is_detected():
    _text, encoding = decode_file(ENGLISH)
    assert encoding == "utf-8-sig"


def test_delimiter_sniffing():
    assert sniff_delimiter(decode_file(SLOVENIAN)[0]) == "\t"
    assert sniff_delimiter(decode_file(ENGLISH)[0]) == ","


def test_empty_file_is_rejected(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(KeywordFileError, match="empty"):
        decode_file(path)


# --- headers ----------------------------------------------------------------


def test_preamble_rows_are_skipped():
    """The first rows are report metadata, not headers."""
    _rows, report = load_gkp(SLOVENIAN)
    assert report.header_row == 3          # three preamble rows


def test_header_row_found_in_english_export():
    _rows, report = load_gkp(ENGLISH)
    assert report.header_row == 2


def test_localized_headers_are_mapped():
    _rows, report = load_gkp(SLOVENIAN)
    assert set(report.mapped) >= {"keyword", "volume", "competition",
                                  "top_bid_low", "top_bid_high"}


def test_missing_keyword_column_fails_loudly(tmp_path):
    """The one case where guessing costs an afternoon: say what WAS found."""
    path = tmp_path / "wrong.csv"
    path.write_text("Report\nApples,Oranges,Pears\n1,2,3\n", encoding="utf-8")
    with pytest.raises(KeywordFileError) as excinfo:
        load_gkp(path)
    assert "No keyword column" in str(excinfo.value)


def test_map_columns_prefers_plain_competition_over_indexed():
    header = ["Keyword", "Competition (indexed value)", "Competition"]
    mapping = map_columns(header)
    assert mapping["competition"] == 2
    assert mapping["competition_index"] == 1


def test_find_header_row_rejects_a_file_without_one():
    with pytest.raises(KeywordFileError, match="No keyword column found"):
        find_header_row([["Report"], ["Generated"], ["a", "b"]])


# --- the two exports must agree ---------------------------------------------


def test_both_exports_parse_to_identical_rows():
    """Different encoding, delimiter, language and number format; same data."""
    slovenian, _ = load_gkp(SLOVENIAN)
    english, _ = load_gkp(ENGLISH)

    assert len(slovenian) == len(english) == 8
    for sl, en in zip(slovenian, english):
        assert sl.normalized == en.normalized
        assert sl.volume.minimum == en.volume.minimum
        assert sl.volume.maximum == en.volume.maximum
        assert sl.volume.mid == en.volume.mid
        assert sl.competition == en.competition
        assert sl.top_bid_low == en.top_bid_low
        assert sl.top_bid_high == en.top_bid_high


def test_infinity_change_column_is_ignored_gracefully():
    """Every row carries a "+∞" three-month change; none of it should matter."""
    rows, report = load_gkp(SLOVENIAN)
    assert report.unparseable_volumes == 0
    assert all(r.volume_mid > 0 for r in rows)


def test_bids_are_parsed_from_both_locales():
    slovenian, _ = load_gkp(SLOVENIAN)
    assert slovenian[0].top_bid_low == 0.35 and slovenian[0].top_bid_high == 1.20


# --- cleaning funnel --------------------------------------------------------


def test_normalize_keyword():
    assert normalize_keyword("  Leather   BIFOLD Wallet ") == "leather bifold wallet"


def test_contains_filter_is_or_joined():
    rows, report = load_gkp(ENGLISH)
    cleaned = clean_keywords(rows, report, contains=["bifold", "card holder"])
    assert {r.normalized for r in cleaned} == {
        "leather bifold wallet", "traditional bifold wallet", "leather card holder"
    }


def test_not_contains_filter():
    rows, report = load_gkp(ENGLISH)
    cleaned = clean_keywords(rows, report, not_contains=["code"])
    assert not any("code" in r.normalized for r in cleaned)


def test_brand_filter_accepts_a_comma_list():
    rows, report = load_gkp(ENGLISH)
    cleaned = clean_keywords(rows, report, brands=["popov,popov leather"])
    assert not any("popov" in r.normalized for r in cleaned)


def test_min_volume_filter():
    rows, report = load_gkp(ENGLISH)
    cleaned = clean_keywords(rows, report, min_volume=1000)
    assert cleaned and all(r.volume_mid >= 1000 for r in cleaned)


def test_dedupe_keeps_the_highest_volume_duplicate():
    from seocrawl.kwplanner import IngestReport, KeywordRow, parse_volume

    report = IngestReport()
    rows = [
        KeywordRow("Leather Wallet", "leather wallet", parse_volume("10 - 100")),
        KeywordRow("leather   wallet", "leather wallet", parse_volume("1K - 10K")),
    ]
    cleaned = clean_keywords(rows, report)
    assert len(cleaned) == 1 and cleaned[0].volume_mid == 3162


def test_funnel_records_every_stage():
    rows, report = load_gkp(ENGLISH)
    clean_keywords(rows, report, contains=["wallet"], not_contains=["code"],
                   brands=["popov"], min_volume=100)
    labels = [label for label, _ in report.stages]
    assert labels == ["rows in", "after --contains", "after --not-contains",
                      "after --brand", "after --min-volume", "unique"]
    assert "rows in" in report.funnel() and "->" in report.funnel()


def test_funnel_counts_decrease_monotonically():
    rows, report = load_gkp(ENGLISH)
    clean_keywords(rows, report, contains=["wallet"], brands=["popov"])
    counts = [count for _, count in report.stages]
    assert counts == sorted(counts, reverse=True)


def test_no_filters_still_dedupes_and_reports():
    rows, report = load_gkp(ENGLISH)
    cleaned = clean_keywords(rows, report)
    assert len(cleaned) == 8
    assert report.stages[-1] == ("unique", 8)
