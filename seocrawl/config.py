"""Crawl configuration, merged from defaults, ``seocrawl.toml`` and CLI flags."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

#: Identifies the crawler honestly and gives site owners someone to contact.
#:
#: This is a placeholder on purpose. A crawler that identifies itself is being
#: polite; one that identifies *somebody else* is worse than anonymous, because
#: the complaints land on a person who never ran it. Set ``user_agent`` in
#: seocrawl.toml or pass ``--user-agent`` before crawling anything you do not
#: own -- the CLI says so on every run that leaves this unchanged.
PLACEHOLDER_CONTACT = "you@example.com"
DEFAULT_CONTACT = PLACEHOLDER_CONTACT
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; seocrawl/1.0; SEO audit; +mailto:%s)" % DEFAULT_CONTACT
)

DEFAULT_CONFIG_NAME = "seocrawl.toml"


class ConfigError(ValueError):
    """A config file exists but cannot be used."""


@dataclass
class Config:
    """Everything that shapes a crawl.

    Values are resolved defaults < ``seocrawl.toml`` < CLI flags, so a flag
    always wins over the file and the file always wins over the built-in default.
    """

    start_url: str = ""

    # --- scope ---
    max_urls: int = 10_000
    max_depth: int = 0                       # 0 = unlimited
    include: list[str] = field(default_factory=list)   # regex; empty = allow all
    exclude: list[str] = field(default_factory=list)   # regex
    use_sitemap: bool = True
    follow_links: bool = True

    # --- politeness ---
    delay: float = 0.7
    workers: int = 4
    timeout: float = 20.0
    user_agent: str = DEFAULT_USER_AGENT
    respect_robots: bool = True
    max_retries: int = 3
    forbidden_limit: int = 10                # consecutive 403s before hard stop

    # --- checks ---
    #: "auto" prefers LanguageTool (grammar + spelling, needs Java) and falls
    #: back to pyspellchecker. Also accepts "languagetool", "pyspellchecker",
    #: "off".
    spellcheck: bool = True
    spellcheck_engine: str = "auto"
    lang: str = "en"
    #: Words never reported as misspellings, on top of the automatic per-site
    #: whitelist built from brand terms seen across the crawl.
    spellcheck_whitelist: list[str] = field(default_factory=list)
    #: Times a capitalized mid-sentence word must appear across the crawl
    #: before it is treated as a brand term rather than a typo.
    brand_term_min_pages: int = 3
    #: Heading texts treated as template furniture rather than content.
    furniture_headings: list[str] = field(default_factory=list)
    #: Anchor texts that promise a specific destination.
    destination_anchors: list[str] = field(default_factory=list)
    image_head_limit: int = 5                # images HEAD-checked per page
    #: After this many pages of one URL template have had their images
    #: size-checked, later sister pages skip the checks. Templated pages are
    #: copies of each other, so the tenth product page teaches you nothing new
    #: about the theme's image pipeline -- and image HEAD requests dominate
    #: crawl time. 0 checks every page.
    image_sample_per_template: int = 25
    oversized_image_bytes: int = 300 * 1024
    near_duplicate_distance: int = 8
    max_redirect_hops: int = 10

    # --- PageSpeed Insights ---
    psi: bool = False
    psi_api_key: str | None = None
    psi_top: int = 10
    psi_delay: float = 1.5
    psi_strategy: str = "mobile"

    # --- storage / output ---
    cache_dir: Path = Path(".seocrawl_cache")
    recrawl: bool = False
    output_dir: Path = Path(".")
    html_report: bool = False
    diff_against: Path | None = None

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.cache_dir)
        self.output_dir = Path(self.output_dir)
        if self.diff_against is not None:
            self.diff_against = Path(self.diff_against)
        self._include_re = [re.compile(p) for p in self.include]
        self._exclude_re = [re.compile(p) for p in self.exclude]

    def allows(self, url: str) -> bool:
        """True when ``url`` passes the include/exclude pattern filters."""
        if any(rx.search(url) for rx in self._exclude_re):
            return False
        if self._include_re and not any(rx.search(url) for rx in self._include_re):
            return False
        return True

    @classmethod
    def from_toml(cls, path: Path) -> dict[str, Any]:
        """Read a ``seocrawl.toml`` into a kwargs dict, ignoring unknown keys.

        Accepts both a flat table and a ``[seocrawl]`` section. A malformed file
        raises :class:`ConfigError` so the CLI can print something readable
        instead of a tomllib traceback.
        """
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError("%s is not valid TOML: %s" % (path, exc)) from exc
        except OSError as exc:
            raise ConfigError("could not read %s: %s" % (path, exc)) from exc
        if "seocrawl" in data and isinstance(data["seocrawl"], dict):
            data = data["seocrawl"]

        known = {f.name for f in fields(cls)}
        return {k: v for k, v in data.items() if k in known}

    @classmethod
    def unknown_keys(cls, path: Path) -> list[str]:
        """Keys present in the file that seocrawl does not recognise."""
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError):
            return []
        if "seocrawl" in data and isinstance(data["seocrawl"], dict):
            data = data["seocrawl"]
        known = {f.name for f in fields(cls)}
        return sorted(k for k in data if k not in known)

    @classmethod
    def load(cls, config_path: Path | None = None, **overrides: Any) -> "Config":
        """Build a config from an optional TOML file plus CLI overrides.

        ``overrides`` whose value is ``None`` are dropped, so an unset CLI flag
        never clobbers a value coming from the file.
        """
        values: dict[str, Any] = {}

        path = config_path or Path(DEFAULT_CONFIG_NAME)
        if path.is_file():
            values.update(cls.from_toml(path))

        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)
