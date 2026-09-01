"""Spellcheck engine adapters.

The engines live here rather than in :mod:`seocrawl.checks` because they do
real I/O -- loading dictionaries, and in LanguageTool's case starting a Java
process -- while everything in ``checks.py`` stays pure and offline.

Two engines are supported:

* **languagetool** (``language_tool_python``) catches grammar as well as
  spelling, but needs a JVM on the machine.
* **pyspellchecker** is pure Python with no system dependency, and catches
  misspellings only.

``auto`` prefers LanguageTool and falls back to pyspellchecker when it is
missing or cannot start, so a machine without Java still gets a working check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Protocol

#: Words a general dictionary flags but that are normal in e-commerce copy.
BUILTIN_ALLOWLIST = frozenset(
    {
        # commerce / logistics
        "ecommerce", "checkout", "sku", "skus", "upsell", "dropship", "backorder",
        "preorder", "prepaid", "unboxing", "restock", "restocked", "giftable",
        # web / SEO
        "url", "urls", "html", "css", "js", "cdn", "seo", "faq", "faqs", "api",
        "sitemap", "canonical", "noindex", "hreflang", "webp", "svg", "og",
        # materials and craft vocabulary that trips generic dictionaries
        "veg", "tanned", "tannage", "burnished", "burnishing", "saddle",
        "stitched", "stitching", "topstitch", "grommet", "rivet", "rivets",
        "patina", "full-grain", "fullgrain", "hardwearing", "handstitched",
        "cardholder", "bifold", "trifold", "crossbody", "carryall", "everyday",
        "keychain", "keychains", "lockstitch", "gifter", "giftable",
        "sitewide", "storewide", "colorway", "colorways",
        # sizing / units
        "cm", "mm", "oz", "lb", "lbs", "ml", "xl", "xxl", "gsm",
        # abbreviations that appear in ordinary product copy
        "etc", "eg", "ie", "vs", "approx", "diy", "usb", "rfid", "airtag",
    }
)

#: Tokens that are never real words worth spellchecking.
_SKIP_TOKEN = re.compile(
    r"""^(
        .{0,2}                  # one- and two-letter fragments
        |\d[\w.,-]*             # anything starting with a digit
        |[\w.-]*\d[\w.-]*       # anything containing a digit (SKUs, Ritza 25)
        |[A-Z]{2,}              # acronyms
        |.*[_/@\\].*            # identifiers, paths, handles
    )$""",
    re.VERBOSE,
)

#: Word-ish tokens, keeping internal digits, apostrophes and hyphens. Digits
#: must stay attached: splitting "Leuchtturm1917" into "Leuchtturm" would both
#: invent a misspelling and stop the whitelist entry from ever matching.
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'’-]*")

#: Stripped before tokenizing -- an address or a link is not prose.
_NOT_PROSE = re.compile(
    r"""(
        https?://\S+                    # URLs
        |www\.\S+
        |[\w.+-]+@[\w-]+\.[\w.]+        # email addresses
        |\{\{.*?\}\}|\{%.*?%\}          # liquid template leftovers
    )""",
    re.VERBOSE | re.IGNORECASE,
)


def tokenize(text: str) -> list[str]:
    """Split prose into checkable word tokens, dropping obvious non-words."""
    prose = _NOT_PROSE.sub(" ", text or "")
    return [word for word in _WORD.findall(prose) if not _SKIP_TOKEN.match(word)]


class SpellEngine(Protocol):
    """Anything that can name the misspelled words in a piece of text."""

    name: str

    def unknown(self, text: str) -> list[str]:
        """Return the misspelled words in ``text``, in order of appearance."""
        ...


@dataclass
class NullEngine:
    """Engine used when spellchecking is disabled -- never reports anything."""

    name: str = "disabled"

    def unknown(self, text: str) -> list[str]:
        return []


#: British -> American spelling transforms. The dictionary shipped with
#: pyspellchecker is US English, so without these every Canadian, British and
#: Australian shop gets "colour", "favour" and "moulds" reported as typos --
#: which would make the whole check untrustworthy on half the sites it runs on.
#: Over-generation is harmless: a variant is only accepted if the dictionary
#: actually knows the transformed word.
_BRITISH_TO_AMERICAN = (
    (re.compile(r"our(s|ed|ing|ite)?$"), r"or\1"),        # colour(s) -> color(s)
    (re.compile(r"ise(s|d|r|rs)?$"), r"ize\1"),           # organise -> organize
    (re.compile(r"isation(s)?$"), r"ization\1"),          # -isation -> -ization
    (re.compile(r"yse(s|d)?$"), r"yze\1"),                # analyse -> analyze
    (re.compile(r"([bcdfgklmnprstvz])re(s)?$"), r"\1er\2"),  # centre -> center
    (re.compile(r"ogue(s)?$"), r"og\1"),                  # catalogue -> catalog
    (re.compile(r"ll(ing|ed|er|ers)$"), r"l\1"),          # travelling -> traveling
    (re.compile(r"ou"), "o"),                             # moulds -> molds
    (re.compile(r"ae|oe"), "e"),                          # anaemia -> anemia
    (re.compile(r"^(.*)mme$"), r"\1m"),                   # programme -> program
)


def american_variants(word: str) -> set[str]:
    """Candidate US spellings of a possibly-British ``word``."""
    return {
        candidate
        for pattern, replacement in _BRITISH_TO_AMERICAN
        for candidate in (pattern.sub(replacement, word),)
        if candidate != word
    }


#: English contraction endings. These are stripped rather than split on, because
#: splitting "doesn't" on the apostrophe leaves "doesn", which is not a word --
#: removing the "n't" leaves "does", which is.
_CONTRACTION = re.compile(r"(n't|'s|'re|'ve|'ll|'d|'m)$", re.I)


def subwords(token: str) -> list[str]:
    """Split a token into the pieces worth checking against a dictionary.

    Contractions and possessives lose their ending; hyphens split compounds a
    general dictionary has never seen ("saddle-stitched"). Only pieces longer
    than two characters are returned, so "I've" and "it's" reduce to nothing and
    are left alone, while a genuine typo inside a compound still surfaces.
    """
    normalized = token.lower().replace("’", "'")
    stripped = _CONTRACTION.sub("", normalized)
    pieces = re.split(r"[-']", stripped or normalized)
    return [p for p in pieces if len(p) > 2]


class PySpellEngine:
    """Dictionary lookup via ``pyspellchecker``. No system dependencies."""

    name = "pyspellchecker"

    def __init__(self, lang: str = "en", allowlist: Iterable[str] = ()) -> None:
        from spellchecker import SpellChecker

        self._checker = SpellChecker(language=lang, distance=1)
        extra = set(BUILTIN_ALLOWLIST) | {w.lower() for w in allowlist}
        if extra:
            self._checker.word_frequency.load_words(extra)

    def _known(self, word: str) -> bool:
        """True when the dictionary knows the word, or a US variant of it."""
        if not self._checker.unknown([word]):
            return True
        variants = american_variants(word)
        if not variants:
            return False
        still_unknown = self._checker.unknown(variants)
        return any(variant not in still_unknown for variant in variants)

    def unknown(self, text: str) -> list[str]:
        tokens = tokenize(text)
        if not tokens:
            return []

        seen: set[str] = set()
        found: list[str] = []
        for token in tokens:
            lowered = token.lower()
            if lowered in seen:
                continue
            pieces = subwords(token)
            # Nothing substantial to judge (e.g. "I've" -> no piece over two
            # characters), so give the token the benefit of the doubt.
            if not pieces:
                continue
            if any(not self._known(piece) for piece in pieces):
                seen.add(lowered)
                found.append(token)
        return found


class LanguageToolEngine:
    """Grammar and spelling via ``language_tool_python`` (needs a JVM)."""

    name = "languagetool"

    def __init__(self, lang: str = "en-US", allowlist: Iterable[str] = ()) -> None:
        import language_tool_python

        self._tool = language_tool_python.LanguageTool(lang)
        self._allow = {w.lower() for w in allowlist} | {
            w.lower() for w in BUILTIN_ALLOWLIST
        }

    def unknown(self, text: str) -> list[str]:
        if not (text or "").strip():
            return []
        found: list[str] = []
        for match in self._tool.check(text):
            # Only spelling-class rules; grammar hits are too noisy for an
            # SEO report and would swamp the actual typos.
            if "SPELL" not in (match.ruleIssueType or "").upper() + match.ruleId.upper():
                continue
            word = text[match.offset:match.offset + match.errorLength].strip()
            if not word or _SKIP_TOKEN.match(word) or word.lower() in self._allow:
                continue
            if word not in found:
                found.append(word)
        return found

    def close(self) -> None:
        try:
            self._tool.close()
        except Exception:  # pragma: no cover - best effort shutdown
            pass


def build_engine(
    engine: str = "auto", lang: str = "en", allowlist: Iterable[str] = ()
) -> tuple[SpellEngine, str | None]:
    """Construct the requested engine.

    Returns ``(engine, warning)``. ``warning`` is a human-readable note when the
    requested engine was unavailable and something else was used instead, so the
    CLI can say so rather than silently checking nothing.
    """
    allowlist = list(allowlist)

    if engine == "off":
        return NullEngine(), None

    if engine in ("auto", "languagetool"):
        try:
            locale = lang if "-" in lang else "%s-US" % lang if lang == "en" else lang
            return LanguageToolEngine(locale, allowlist), None
        except Exception as exc:
            if engine == "languagetool":
                return NullEngine(), (
                    "LanguageTool could not start (%s); spellcheck disabled. "
                    "It needs Java -- install a JRE, or use "
                    "spellcheck_engine = \"pyspellchecker\"." % type(exc).__name__
                )
            warning = (
                "LanguageTool unavailable (needs Java), using pyspellchecker "
                "-- spelling only, no grammar."
            )
            try:
                return PySpellEngine(lang, allowlist), warning
            except Exception as fallback_exc:
                return NullEngine(), (
                    "No spellcheck engine available (%s); spellcheck disabled."
                    % type(fallback_exc).__name__
                )

    if engine == "pyspellchecker":
        try:
            return PySpellEngine(lang, allowlist), None
        except Exception as exc:
            return NullEngine(), (
                "pyspellchecker could not start (%s); spellcheck disabled."
                % type(exc).__name__
            )

    return NullEngine(), "Unknown spellcheck engine %r; spellcheck disabled." % engine
