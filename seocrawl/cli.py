"""Command-line interface: ``seocrawl <url>`` / ``python -m seocrawl <url>``."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Sequence

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from . import __version__
from .config import (DEFAULT_CONFIG_NAME, DEFAULT_USER_AGENT,
                     PLACEHOLDER_CONTACT, Config, ConfigError)
from .crawler import CrawlBlocked, Crawler
from .export import (
    diff_crawls,
    report_name,
    write_diff_sheet,
    write_html_report,
    write_workbook,
)
from .findings import HIGH, LOW, MED, Audit, anchor_report
from .psi import psi_findings, run_psi
from .urls import normalize_url, registrable_host

SEVERITY_STYLE = {HIGH: "bold red", MED: "yellow", LOW: "green"}


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter,
                     argparse.RawDescriptionHelpFormatter):
    """Show flag defaults, but leave the epilog's line breaks alone."""


def build_parser() -> argparse.ArgumentParser:
    """Define every CLI flag."""
    parser = argparse.ArgumentParser(
        prog="seocrawl",
        description="SEO audit crawler for small/mid e-commerce sites.",
        epilog=(
            "Examples:\n"
            "  seocrawl https://example.com --max-urls 500 --html\n"
            "  seocrawl keywords --gkp planner_export.csv --crawl audit.xlsx\n"
            "  seocrawl ui\n"
            "\n"
            "Run 'seocrawl ui' for a local web interface, or\n"
            "'seocrawl keywords --help' for the keyword-mapping flags."
        ),
        formatter_class=_HelpFormatter,
    )
    parser.add_argument("url", nargs="?", help="start URL, e.g. https://example.com")
    parser.add_argument("--version", action="version", version="seocrawl %s" % __version__)
    parser.add_argument("-c", "--config", type=Path,
                        help="TOML config file (default: ./%s if present)" % DEFAULT_CONFIG_NAME)

    scope = parser.add_argument_group("scope")
    scope.add_argument("--max-urls", type=int, help="stop after this many URLs")
    scope.add_argument("--max-depth", type=int, help="max link depth (0 = unlimited)")
    scope.add_argument("--include", action="append", metavar="REGEX",
                       help="only crawl URLs matching this pattern (repeatable)")
    scope.add_argument("--exclude", action="append", metavar="REGEX",
                       help="skip URLs matching this pattern (repeatable)")
    scope.add_argument("--no-sitemap", dest="use_sitemap", action="store_false", default=None,
                       help="skip sitemap discovery, crawl links only")
    scope.add_argument("--sitemap-only", dest="follow_links", action="store_false", default=None,
                       help="crawl only sitemap URLs, do not follow links")

    politeness = parser.add_argument_group("politeness")
    politeness.add_argument("--delay", type=float, help="seconds between requests")
    politeness.add_argument("--workers", type=int, help="concurrent workers")
    politeness.add_argument("--timeout", type=float, help="per-request timeout in seconds")
    politeness.add_argument("--user-agent", help="User-Agent header")
    politeness.add_argument("--ignore-robots", dest="respect_robots", action="store_false",
                            default=None,
                            help="crawl URLs robots.txt disallows (use only on sites you own)")

    checks = parser.add_argument_group("checks")
    checks.add_argument("--image-head-limit", type=int,
                        help="images to size-check per page (0 disables)")
    checks.add_argument("--image-sample-per-template", type=int, metavar="N",
                        help="stop size-checking images after N pages of one URL "
                             "template (0 = every page); image HEADs dominate "
                             "crawl time and sister pages are copies")
    checks.add_argument("--near-duplicate-distance", type=int,
                        help="SimHash bit distance counted as a near-duplicate")
    checks.add_argument("--no-spellcheck", dest="spellcheck", action="store_false",
                        default=None, help="skip the typo check")
    checks.add_argument("--spellcheck-engine",
                        choices=("auto", "languagetool", "pyspellchecker", "off"),
                        help="auto prefers LanguageTool and falls back if Java is absent")
    checks.add_argument("--lang", help="audit language; other-language pages are skipped")
    checks.add_argument("--whitelist", dest="spellcheck_whitelist", action="append",
                        metavar="WORD", help="never treat this word as a typo (repeatable)")

    psi_group = parser.add_argument_group("PageSpeed Insights")
    psi_group.add_argument("--psi", action="store_true", default=None,
                           help="run PSI on the most-linked pages")
    psi_group.add_argument("--psi-key", dest="psi_api_key",
                           help="PSI API key (or set PSI_API_KEY)")
    psi_group.add_argument("--psi-top", type=int, help="how many pages to test")
    psi_group.add_argument("--psi-strategy", choices=("mobile", "desktop"),
                           help="PSI strategy")

    output = parser.add_argument_group("output")
    output.add_argument("-o", "--output-dir", type=Path, help="where to write reports")
    output.add_argument("--html", dest="html_report", action="store_true", default=None,
                        help="also write a single-file HTML report")
    output.add_argument("--diff", dest="diff_against", type=Path, metavar="PREVIOUS.XLSX",
                        help="compare against a previous audit workbook")
    output.add_argument("--recrawl", action="store_true", default=None,
                        help="ignore the resume cache and start fresh")
    output.add_argument("--cache-dir", type=Path, help="resume cache location")
    output.add_argument("-q", "--quiet", action="store_true", help="only print the summary")

    return parser


def resolve_config(args: argparse.Namespace) -> Config:
    """Merge CLI flags over the TOML file over the built-in defaults."""
    overrides = {
        key: getattr(args, key)
        for key in (
            "max_urls", "max_depth", "include", "exclude", "use_sitemap", "follow_links",
            "delay", "workers", "timeout", "user_agent", "respect_robots",
            "image_head_limit", "image_sample_per_template",
            "near_duplicate_distance",
            "spellcheck", "spellcheck_engine", "lang", "spellcheck_whitelist",
            "psi", "psi_api_key", "psi_top", "psi_strategy",
            "output_dir", "html_report", "diff_against", "recrawl", "cache_dir",
        )
        if getattr(args, key, None) is not None
    }
    if args.url:
        overrides["start_url"] = normalize_url(args.url)

    config = Config.load(args.config, **overrides)
    if config.psi and not config.psi_api_key:
        config.psi_api_key = os.environ.get("PSI_API_KEY")
    return config


def _print_findings(console: Console, findings: Sequence, limit: int = 15) -> None:
    """Print the top findings as a table."""
    if not findings:
        console.print("\n[green]No issues found.[/green]")
        return

    table = Table(title="Top findings", title_justify="left", show_lines=False,
                  header_style="bold", expand=True)
    table.add_column("Sev", width=5)
    table.add_column("URLs", width=6, justify="right")
    table.add_column("Issue", ratio=2)
    table.add_column("Why it matters", ratio=3, overflow="fold")

    for finding in findings[:limit]:
        table.add_row(
            "[%s]%s[/]" % (SEVERITY_STYLE.get(finding.severity, ""), finding.severity),
            str(finding.count),
            finding.issue,
            finding.why_it_matters,
        )
    console.print()
    console.print(table)
    if len(findings) > limit:
        console.print("[dim]... and %d more in the Findings sheet.[/dim]"
                      % (len(findings) - limit))


def _print_summary(console: Console, stats, findings: Sequence) -> None:
    """Print the headline crawl numbers."""
    counts = {s: sum(1 for f in findings if f.severity == s) for s in (HIGH, MED, LOW)}
    console.print(Panel(
        "URLs crawled: [bold]%d[/bold]   Indexable: [bold]%d[/bold]   "
        "Products: [bold]%d[/bold]   Orphans: [bold]%d[/bold]   Ghosts: [bold]%d[/bold]\n"
        "Findings: [bold red]%d HIGH[/bold red]  [yellow]%d MED[/yellow]  "
        "[green]%d LOW[/green]"
        % (stats.total_urls, stats.indexable, stats.products, stats.orphans,
           stats.ghosts, counts[HIGH], counts[MED], counts[LOW]),
        title="Crawl summary", border_style="blue",
    ))


#: Subcommands dispatched before the crawl parser sees the arguments. Keeping
#: the crawl form as the bare default means "seocrawl <url>" is unchanged.
SUBCOMMANDS = ("keywords", "ui")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns a shell exit status."""
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in SUBCOMMANDS:
        if argv[0] == "keywords":
            from .kwcli import main as keywords_main

            return keywords_main(argv[1:])

        if argv[0] == "ui":
            from .ui import serve

            ui_parser = argparse.ArgumentParser(
                prog="seocrawl ui",
                description="Open the local web UI in a browser.")
            ui_parser.add_argument("--port", type=int, default=8724)
            ui_parser.add_argument("--host", default="127.0.0.1",
                                   help="bind address; localhost only by default")
            ui_parser.add_argument("-o", "--output-dir", type=Path, default=None,
                                   help="where reports are written")
            ui_parser.add_argument("--no-browser", dest="open_browser",
                                   action="store_false",
                                   help="do not open a browser window")
            ui_args = ui_parser.parse_args(argv[1:])
            return serve(host=ui_args.host, port=ui_args.port,
                         output_dir=ui_args.output_dir,
                         open_browser=ui_args.open_browser)

    args = build_parser().parse_args(argv)
    console = Console(stderr=False, quiet=False)

    if not args.url and not (args.config or Path(DEFAULT_CONFIG_NAME).is_file()):
        build_parser().print_help()
        return 2

    try:
        config = resolve_config(args)
    except ConfigError as exc:
        console.print("[red]Config error:[/red] %s" % exc)
        return 2
    if not config.start_url:
        console.print("[red]No start URL given (pass one, or set start_url in the config).[/red]")
        return 2

    if config.user_agent == DEFAULT_USER_AGENT and not args.quiet:
        console.print(
            "[yellow]Identifying as the placeholder contact %s.[/yellow] "
            "Set user_agent in seocrawl.toml or pass --user-agent so site "
            "owners can reach [italic]you[/italic] about this crawl."
            % PLACEHOLDER_CONTACT
        )

    started = time.monotonic()
    crawler = Crawler(config)
    if crawler.spell_warning and not args.quiet:
        console.print("[yellow]%s[/yellow]" % crawler.spell_warning)

    try:
        if args.quiet:
            pages = crawler.run()
        else:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(bar_width=30),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=console,
                transient=True,
            ) as progress:
                task = progress.add_task("Crawling %s" % registrable_host(config.start_url),
                                         total=config.max_urls)

                def report(url: str, done: int, total: int) -> None:
                    progress.update(task, completed=done, total=max(total, done),
                                    description="Crawling %s" % url[-60:])

                crawler.on_progress = report
                pages = crawler.run()
    except CrawlBlocked as exc:
        console.print("\n[bold red]Crawl stopped.[/bold red]\n%s" % exc)
        crawler.close()
        return 1
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted -- progress is cached, rerun to resume.[/yellow]")
        crawler.close()
        return 130
    finally:
        duration = time.monotonic() - started

    if not pages:
        console.print("[red]No pages were crawled. Check the URL and robots.txt.[/red]")
        crawler.close()
        return 1

    audit = Audit(pages, crawler.links, crawler.sitemap_urls,
                  near_duplicate_distance=config.near_duplicate_distance,
                  destination_anchors=config.destination_anchors)
    findings = audit.run()

    psi_results = []
    if config.psi:
        if not config.psi_api_key and not args.quiet:
            console.print("[yellow]No PSI API key -- using the unauthenticated quota, "
                          "which is very low.[/yellow]")
        with console.status("Running PageSpeed Insights..."):
            psi_results = run_psi(
                list(pages.values()), config.psi_api_key,
                top=config.psi_top, strategy=config.psi_strategy, delay=config.psi_delay,
            )
        findings.extend(psi_findings(psi_results))
        findings.sort(key=lambda f: f.sort_key())

    stats = audit.stats(findings)
    anchors = anchor_report(crawler.links)

    diff = None
    if config.diff_against:
        if config.diff_against.is_file():
            diff = diff_crawls(config.diff_against, findings, list(pages.values()))
        else:
            console.print("[yellow]--diff file not found: %s[/yellow]" % config.diff_against)

    xlsx_path = config.output_dir / report_name(config.start_url, "xlsx")
    write_workbook(xlsx_path, list(pages.values()), findings, stats, anchors, psi_results,
                   site=registrable_host(config.start_url), duration_s=duration)

    if diff is not None:
        from openpyxl import load_workbook

        book = load_workbook(xlsx_path)
        write_diff_sheet(book, diff)
        book.save(xlsx_path)

    html_path = None
    if config.html_report:
        html_path = config.output_dir / report_name(config.start_url, "html")
        write_html_report(html_path, stats, findings,
                          site=registrable_host(config.start_url),
                          psi_results=psi_results, diff=diff)

    crawler.close()

    _print_summary(console, stats, findings)
    _print_findings(console, findings)

    if diff is not None:
        console.print("\n[bold]Since %s:[/bold] %d new issue types, %d fixed, "
                      "%d new URLs, %d removed"
                      % (config.diff_against.name, len(diff.new_issues),
                         len(diff.fixed_issues), len(diff.new_urls), len(diff.removed_urls)))

    console.print("\n[bold]Report:[/bold] %s" % xlsx_path)
    if html_path:
        console.print("[bold]HTML:[/bold]   %s" % html_path)

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
