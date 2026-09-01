"""Google Keyword Planner CSV ingestion.

The export is hostile in several specific ways, and every one of them is handled
here rather than left to the caller:

* it is usually **UTF-16 with tab delimiters**, not the UTF-8 CSV the extension
  claims;
* the first rows are report metadata, not headers;
* headers are localized to the account's interface language;
* volumes come back as **ranges** ("1 tis. - 10 tis.", "1K - 10K") for accounts
  that are not actively spending, with localized magnitude suffixes;
* numbers use whichever decimal and thousands separators the locale prefers.

Nothing in here raises on a single bad row. A file that cannot be understood at
all fails loudly with a message naming the headers it *did* find, because that
is the one case where guessing wastes an afternoon.
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

# --- header aliases ---------------------------------------------------------

#: Field -> header texts that mean it, lowercased. Slovenian and English for
#: now; adding a language is adding a row here.
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "keyword": (
        "keyword", "keywords", "keyword (by relevance)", "search term",
        "ključna beseda", "kljucna beseda", "ključne besede", "iskalni izraz",
    ),
    "volume": (
        "avg. monthly searches", "avg monthly searches", "average monthly searches",
        "monthly searches", "searches",
        "povpr. mesečna iskanja", "povpr. mesecna iskanja",
        "povprečna mesečna iskanja", "povprecna mesecna iskanja",
        "mesečna iskanja", "mesecna iskanja",
    ),
    "competition": (
        "competition", "competition (indexed value)",
        "konkurenca", "konkurenčnost", "konkurencnost",
    ),
    "competition_index": (
        "competition (indexed value)", "competition index",
        "konkurenca (indeksirana vrednost)",
    ),
    "top_bid_low": (
        "top of page bid (low range)", "top of page bid low range",
        "low top of page bid",
        "ponudba na vrhu strani (nizko območje)",
        "ponudba na vrhu strani (nizki razpon)",
        "ponudba na vrhu strani (spodnji razpon)",
    ),
    "top_bid_high": (
        "top of page bid (high range)", "top of page bid high range",
        "high top of page bid",
        "ponudba na vrhu strani (visoko območje)",
        "ponudba na vrhu strani (visoki razpon)",
        "ponudba na vrhu strani (zgornji razpon)",
    ),
}

#: Competition values -> normalized level.
#:
#: Slovenian inflects the adjective for gender, and which form Google emits
#: depends on the noun it agreed with when the export was generated -- real
#: exports use the neuter "Visoko"/"Nizko" while the docs suggest the feminine
#: "Visoka"/"Nizka". All the forms are listed rather than guessed at.
COMPETITION_LEVELS: dict[str, str] = {
    "low": "LOW", "medium": "MED", "high": "HIGH",
    # Slovenian: low
    "nizka": "LOW", "nizko": "LOW", "nizek": "LOW",
    "šibka": "LOW", "sibka": "LOW", "šibko": "LOW", "sibko": "LOW",
    # Slovenian: medium
    "srednja": "MED", "srednje": "MED", "srednji": "MED", "srednja raven": "MED",
    # Slovenian: high
    "visoka": "HIGH", "visoko": "HIGH", "visok": "HIGH",
    "močna": "HIGH", "mocna": "HIGH", "močno": "HIGH", "mocno": "HIGH",
}

#: Values that explicitly mean "Google does not know", as opposed to a value we
#: failed to parse. Both end up as ``None``, but only one is a bug.
COMPETITION_UNKNOWN = frozenset({"unknown", "neznano", "neznana", "--", "-"})

#: Magnitude suffixes used in bucketed volumes.
MULTIPLIERS: tuple[tuple[str, int], ...] = (
    ("mio.", 1_000_000), ("mio", 1_000_000), ("mln", 1_000_000),
    ("tis.", 1_000), ("tis", 1_000),
    ("m", 1_000_000), ("k", 1_000),
)

#: Every dash Google has ever used in a range.
_DASHES = "-‐‑‒–—―~"
_RANGE_SPLIT = re.compile(r"\s*[%s]\s*" % re.escape(_DASHES))

#: Values that mean "no number here" rather than a parse failure.
_NULLISH = frozenset({"", "-", "--", "n/a", "na", "–", "—", "0", "+∞", "-∞", "∞", "∞%"})


class KeywordFileError(ValueError):
    """The file could not be understood as a Keyword Planner export."""


# --- number parsing ---------------------------------------------------------


def parse_number(text: Any) -> float | None:
    """Parse a locale-formatted number, or return ``None``.

    Handles ``1,300`` (English thousands), ``1.300`` (Slovenian thousands),
    ``1,5`` (Slovenian decimal) and ``1.5``. Ambiguity is resolved by shape: a
    separator followed by exactly three digits, repeating, is a thousands
    separator; anything else is a decimal point.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)

    cleaned = str(text).strip().replace(" ", " ")
    if cleaned.lower() in _NULLISH:
        return None

    # Drop currency symbols and stray spaces inside the number.
    cleaned = re.sub(r"[^\d.,\-]", "", cleaned)
    if not cleaned or not re.search(r"\d", cleaned):
        return None

    has_dot, has_comma = "." in cleaned, "," in cleaned
    if has_dot and has_comma:
        # The rightmost separator is the decimal one.
        decimal_sep = "." if cleaned.rfind(".") > cleaned.rfind(",") else ","
        thousands = "," if decimal_sep == "." else "."
        cleaned = cleaned.replace(thousands, "").replace(decimal_sep, ".")
    elif has_comma:
        cleaned = cleaned.replace(",", "" if re.fullmatch(r"-?\d{1,3}(,\d{3})+", cleaned) else ".")
    elif has_dot:
        if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", cleaned):
            cleaned = cleaned.replace(".", "")

    try:
        return float(cleaned)
    except ValueError:
        return None


def _apply_multiplier(text: str) -> float | None:
    """Parse ``"10 tis."`` / ``"1K"`` / ``"1,5 mio."`` into a plain number."""
    lowered = text.strip().lower().replace(" ", " ")
    if not lowered or lowered in _NULLISH:
        return None

    for suffix, factor in MULTIPLIERS:
        if lowered.endswith(suffix):
            head = lowered[: -len(suffix)].strip()
            number = parse_number(head)
            return None if number is None else number * factor

    return parse_number(lowered)


@dataclass(frozen=True)
class Volume:
    """A search volume, which may be a bucket rather than a number."""

    minimum: int
    maximum: int
    mid: int
    label: str
    parsed: bool = True

    @property
    def is_range(self) -> bool:
        return self.minimum != self.maximum


def parse_volume(text: Any) -> Volume:
    """Parse a volume cell into a min/max/mid triple.

    Bucketed volumes ("1 tis. - 10 tis.") get a **geometric** mid-point, not an
    arithmetic one: a 1,000-10,000 bucket is far more likely to hold 3,000 than
    5,500, and using the arithmetic mean systematically overstates every bucket
    on the sheet.
    """
    label = "" if text is None else str(text).strip()
    if not label or label.lower() in _NULLISH:
        return Volume(0, 0, 0, label, parsed=not label or label.lower() in _NULLISH)

    parts = [p for p in _RANGE_SPLIT.split(label) if p.strip()]
    if len(parts) >= 2:
        low, high = _apply_multiplier(parts[0]), _apply_multiplier(parts[-1])
        if low is not None and high is not None:
            low, high = min(low, high), max(low, high)
            mid = round(math.sqrt(low * high)) if low > 0 else round(high / 2)
            return Volume(int(low), int(high), int(mid), label)
        return Volume(0, 0, 0, label, parsed=False)

    single = _apply_multiplier(label)
    if single is None:
        return Volume(0, 0, 0, label, parsed=False)
    return Volume(int(single), int(single), int(round(single)), label)


def parse_competition(text: Any) -> str | None:
    """Normalize a competition cell to ``LOW`` / ``MED`` / ``HIGH``."""
    if text is None:
        return None
    value = str(text).strip().lower()
    if value in COMPETITION_LEVELS:
        return COMPETITION_LEVELS[value]
    if value in COMPETITION_UNKNOWN:
        return None

    # Some exports give a 0-100 index instead of a word.
    number = parse_number(value)
    if number is None:
        return None
    if number <= 33:
        return "LOW"
    return "MED" if number <= 66 else "HIGH"


# --- rows -------------------------------------------------------------------


def normalize_keyword(text: str) -> str:
    """Lowercase, collapse whitespace -- the dedupe key for a keyword."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


@dataclass(frozen=True)
class KeywordRow:
    """One cleaned keyword from the planner export."""

    keyword: str
    normalized: str
    volume: Volume
    competition: str | None = None
    competition_index: float | None = None
    top_bid_low: float | None = None
    top_bid_high: float | None = None

    @property
    def volume_mid(self) -> int:
        return self.volume.mid

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(self.normalized.split())


@dataclass
class IngestReport:
    """What the reader had to figure out, and what it threw away."""

    path: str = ""
    encoding: str = ""
    delimiter: str = ""
    header_row: int = 0
    headers: list[str] = field(default_factory=list)
    mapped: dict[str, str] = field(default_factory=dict)
    total_rows: int = 0
    unparseable_volumes: int = 0
    blank_keywords: int = 0
    stages: list[tuple[str, int]] = field(default_factory=list)

    def stage(self, label: str, count: int) -> None:
        """Record one step of the cleaning funnel."""
        self.stages.append((label, count))

    def funnel(self) -> str:
        """The funnel as one line: ``2152 rows in -> 411 after --contains -> ...``."""
        return " -> ".join("%d %s" % (count, label) for label, count in self.stages)


# --- reading ----------------------------------------------------------------

#: Tried in order. UTF-16 first because that is what GKP actually ships.
_ENCODINGS = ("utf-16", "utf-8-sig", "utf-8", "cp1252", "latin-1")


def decode_file(path: Path) -> tuple[str, str]:
    """Decode the export, returning ``(text, encoding_used)``.

    A BOM decides it outright; otherwise each candidate encoding is tried and
    the first one that both decodes and yields a plausible table wins.
    """
    raw = path.read_bytes()
    if not raw.strip():
        raise KeywordFileError("%s is empty." % path)

    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig"), "utf-8-sig"

    # A UTF-16 file without a BOM still gives itself away: every other byte of
    # ASCII text is a NUL.
    if raw.count(b"\x00") > len(raw) // 4:
        for encoding in ("utf-16-le", "utf-16-be"):
            try:
                return raw.decode(encoding), encoding
            except UnicodeDecodeError:
                continue

    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue

    raise KeywordFileError("Could not decode %s with any of: %s"
                           % (path, ", ".join(_ENCODINGS)))


def sniff_delimiter(text: str) -> str:
    """Tab or comma, decided by which one actually splits the rows."""
    sample = [line for line in text.splitlines()[:20] if line.strip()]
    if not sample:
        return ","
    tabs = sum(line.count("\t") for line in sample)
    commas = sum(line.count(",") for line in sample)
    semicolons = sum(line.count(";") for line in sample)

    best = max((tabs, "\t"), (commas, ","), (semicolons, ";"))
    return best[1] if best[0] else ","


def _looks_like_header(cells: Sequence[str]) -> bool:
    """True when a row contains a recognizable keyword-column header."""
    lowered = {str(c).strip().lower() for c in cells}
    return bool(lowered & set(HEADER_ALIASES["keyword"]))


def find_header_row(rows: Sequence[Sequence[str]]) -> int:
    """Index of the header row, skipping the report's metadata preamble."""
    for index, row in enumerate(rows[:30]):
        if _looks_like_header(row):
            return index
    raise KeywordFileError(
        "No keyword column found in the first %d rows. Looked for any of: %s"
        % (min(30, len(rows)), ", ".join(sorted(HEADER_ALIASES["keyword"])))
    )


def map_columns(header: Sequence[str]) -> dict[str, int]:
    """Map our field names onto column indexes in this export's header."""
    mapping: dict[str, int] = {}
    normalized = [str(cell).strip().lower() for cell in header]

    for fieldname, aliases in HEADER_ALIASES.items():
        for index, cell in enumerate(normalized):
            if cell in aliases:
                # Prefer the first match, except that the indexed-value column
                # must not also claim the plain competition slot.
                if fieldname == "competition" and "index" in cell:
                    continue
                mapping.setdefault(fieldname, index)

    if "keyword" not in mapping:
        raise KeywordFileError(
            "No keyword column. Headers found: %s"
            % ", ".join(repr(h) for h in header if str(h).strip())
        )
    return mapping


def load_gkp(path: Path | str) -> tuple[list[KeywordRow], IngestReport]:
    """Read a Keyword Planner export into normalized rows.

    Returns the rows plus an :class:`IngestReport` describing what had to be
    guessed -- encoding, delimiter, which headers matched -- so the console can
    show it and a surprising result can be traced back to a misread file.
    """
    path = Path(path)
    text, encoding = decode_file(path)
    delimiter = sniff_delimiter(text)

    rows = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    rows = [row for row in rows if any(str(cell).strip() for cell in row)]
    if not rows:
        raise KeywordFileError("%s has no data rows." % path)

    header_index = find_header_row(rows)
    header = rows[header_index]
    mapping = map_columns(header)

    report = IngestReport(
        path=str(path),
        encoding=encoding,
        delimiter={"\t": "tab", ",": "comma", ";": "semicolon"}.get(delimiter, delimiter),
        header_row=header_index,
        headers=[str(h).strip() for h in header if str(h).strip()],
        mapped={f: str(header[i]).strip() for f, i in mapping.items()},
    )

    def cell(row: Sequence[str], fieldname: str) -> str | None:
        index = mapping.get(fieldname)
        if index is None or index >= len(row):
            return None
        return row[index]

    keywords: list[KeywordRow] = []
    for row in rows[header_index + 1:]:
        raw_keyword = (cell(row, "keyword") or "").strip()
        if not raw_keyword:
            report.blank_keywords += 1
            continue

        volume = parse_volume(cell(row, "volume"))
        if not volume.parsed:
            report.unparseable_volumes += 1

        keywords.append(
            KeywordRow(
                keyword=raw_keyword,
                normalized=normalize_keyword(raw_keyword),
                volume=volume,
                competition=parse_competition(cell(row, "competition")),
                competition_index=parse_number(cell(row, "competition_index")),
                top_bid_low=parse_number(cell(row, "top_bid_low")),
                top_bid_high=parse_number(cell(row, "top_bid_high")),
            )
        )

    report.total_rows = len(keywords)
    report.stage("rows in", len(keywords))
    return keywords, report


# --- cleaning ---------------------------------------------------------------


def _split_terms(values: Iterable[str]) -> list[str]:
    """Split comma-separated flag values into individual lowercase terms."""
    terms: list[str] = []
    for value in values or ():
        terms.extend(part.strip().lower() for part in str(value).split(",") if part.strip())
    return terms


def clean_keywords(
    rows: Sequence[KeywordRow],
    report: IngestReport,
    *,
    contains: Iterable[str] = (),
    not_contains: Iterable[str] = (),
    brands: Iterable[str] = (),
    min_volume: int = 0,
) -> list[KeywordRow]:
    """Apply the filters in order, recording the count after each stage.

    Order matters and is fixed: keep-what-I-want, then drop-what-I-don't, then
    brand, then volume, then dedupe. Reporting the count at every stage is what
    makes a surprising final number diagnosable instead of mysterious.
    """
    keep = _split_terms(contains)
    drop = _split_terms(not_contains)
    brand_terms = _split_terms(brands)

    working = list(rows)

    if keep:
        working = [r for r in working if any(term in r.normalized for term in keep)]
        report.stage("after --contains", len(working))

    if drop:
        working = [r for r in working if not any(term in r.normalized for term in drop)]
        report.stage("after --not-contains", len(working))

    if brand_terms:
        working = [r for r in working
                   if not any(term in r.normalized for term in brand_terms)]
        report.stage("after --brand", len(working))

    if min_volume:
        working = [r for r in working if r.volume_mid >= min_volume]
        report.stage("after --min-volume", len(working))

    # Dedupe on the normalized text, keeping the highest-volume duplicate so a
    # bucketed row never hides a numeric one for the same term.
    best: dict[str, KeywordRow] = {}
    for row in working:
        existing = best.get(row.normalized)
        if existing is None or row.volume_mid > existing.volume_mid:
            best[row.normalized] = row

    deduped = sorted(best.values(), key=lambda r: (-r.volume_mid, r.normalized))
    report.stage("unique", len(deduped))
    return deduped
