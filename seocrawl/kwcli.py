"""The ``seocrawl keywords`` subcommand: GKP export + crawl -> keyword map."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .export import write_keyword_sheets
from .kwmatch import (
    CANNIBAL,
    GAP,
    OWNED,
    WEAK,
    VOLUME_CAVEAT,
    KeywordMap,
    load_pages_from_xlsx,
    map_keywords,
    pages_from_crawl,
)
from .kwplanner import KeywordFileError, clean_keywords, load_gkp

STATUS_STYLE = {GAP: "bold red", CANNIBAL: "yellow", WEAK: "cyan", OWNED: "green"}


def build_keyword_parser() -> argparse.ArgumentParser:
    """Flags for ``seocrawl keywords``."""
    parser = argparse.ArgumentParser(
        prog="seocrawl keywords",
        description="Join a Google Keyword Planner export against a crawl.",
        epilog=("Example: seocrawl keywords --gkp planner_export.csv "
                "--crawl audit_example-com_2026-08-26.xlsx --brand acme --contains wallet"),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--gkp", type=Path, required=True,
                        help="Keyword Planner CSV/TSV export")

    source = parser.add_argument_group("crawl source")
    source.add_argument("--crawl", type=Path,
                        help="audit xlsx to join against (reads its Pages sheet)")
    source.add_argument("--url", help="crawl this site now instead of reading an xlsx")
    source.add_argument("--max-urls", type=int, default=500,
                        help="URL cap when crawling with --url")
    source.add_argument("--delay", type=float, default=0.7,
                        help="request delay when crawling with --url")

    clean = parser.add_argument_group("cleaning (applied in this order)")
    clean.add_argument("--contains", action="append", metavar="TEXT",
                       help="keep only keywords containing this (repeatable, OR-joined)")
    clean.add_argument("--not-contains", action="append", metavar="TEXT",
                       help="drop keywords containing this (repeatable)")
    clean.add_argument("--brand", action="append", metavar="TERMS",
                       help="drop branded keywords; accepts a comma list")
    clean.add_argument("--min-volume", type=int, default=0,
                       help="drop keywords below this volume_mid")

    matching = parser.add_argument_group("matching")
    matching.add_argument("--no-idf", dest="use_idf", action="store_false",
                          help="weigh every keyword token equally instead of by how "
                               "much it distinguishes pages")
    matching.add_argument("--include-non-indexable", action="store_true",
                          help="also match against noindex/redirected pages")

    output = parser.add_argument_group("output")
    output.add_argument("-o", "--output", type=Path,
                        help="workbook to write (default: alongside --crawl, or "
                             "keyword_map.xlsx)")
    output.add_argument("--standalone", action="store_true",
                        help="write a new workbook instead of appending to --crawl")
    output.add_argument("--geo-note", default="",
                        help="geo/locale label to print with the volume caveat")
    output.add_argument("-q", "--quiet", action="store_true",
                        help="only print the funnel and verdict")

    return parser


def _print_ingest(console: Console, report, geo_note: str) -> None:
    """Say what had to be guessed about the file before trusting its numbers."""
    console.print(
        "[dim]Read %s: %s, %s-delimited, header on row %d, %d columns mapped"
        "%s[/dim]"
        % (Path(report.path).name, report.encoding, report.delimiter,
           report.header_row + 1, len(report.mapped),
           ", %d unparseable volumes" % report.unparseable_volumes
           if report.unparseable_volumes else "")
    )
    caveat = VOLUME_CAVEAT
    if geo_note:
        caveat = "Geo: %s. %s" % (geo_note, caveat)
    console.print("[yellow]%s[/yellow]\n" % caveat)


def _print_verdict(console: Console, keyword_map: KeywordMap, funnel: str) -> None:
    """The funnel plus the four-bucket verdict, in one glance."""
    counts = keyword_map.counts
    top_cluster = next((c for c in keyword_map.clusters if c.size > 1), None)
    cluster_note = (
        " (top cluster: '%s', %d kws)" % (top_cluster.head, top_cluster.size)
        if top_cluster else ""
    )

    console.print(Panel(
        "%s\n[green]%d OWNED[/green], [cyan]%d WEAK[/cyan], "
        "[bold red]%d GAP[/bold red]%s, [yellow]%d CANNIBAL[/yellow]"
        % (funnel, counts[OWNED], counts[WEAK], counts[GAP], cluster_note,
           counts[CANNIBAL]),
        title="Keyword map", border_style="blue",
    ))


def _print_gap_clusters(console: Console, keyword_map: KeywordMap, limit: int = 5) -> None:
    """The biggest unowned demand, which is what the sheet is for."""
    clusters = [c for c in keyword_map.clusters if c.size > 1][:limit]
    if not clusters:
        console.print("[green]No multi-keyword gap clusters.[/green]")
        return

    table = Table(title="Biggest gap clusters", title_justify="left",
                  header_style="bold", expand=True)
    table.add_column("Cluster", ratio=2)
    table.add_column("Kws", width=5, justify="right")
    table.add_column("Volume", width=9, justify="right")
    table.add_column("Top keywords", ratio=3, overflow="fold")

    for cluster in clusters:
        ranked = sorted(cluster.keywords, key=lambda m: -m.volume_mid)
        table.add_row(cluster.head, str(cluster.size), "{:,}".format(cluster.volume),
                      ", ".join(m.keyword for m in ranked[:4]))
    console.print()
    console.print(table)


def _print_cannibalization(console: Console, keyword_map: KeywordMap, limit: int = 5) -> None:
    """Contested keywords, biggest first."""
    contested = sorted(keyword_map.by_status(CANNIBAL), key=lambda m: -m.volume_mid)
    if not contested:
        console.print("\n[green]No cannibalization detected.[/green]")
        return

    table = Table(title="Cannibalization", title_justify="left",
                  header_style="bold", expand=True)
    table.add_column("Keyword", ratio=2)
    table.add_column("Volume", width=8, justify="right")
    table.add_column("Competing pages", ratio=4, overflow="fold")

    for match in contested[:limit]:
        table.add_row(
            match.keyword, "{:,}".format(match.volume_mid),
            "\n".join("%s (%.2f)" % (c.url, c.score) for c in match.contenders),
        )
    console.print()
    console.print(table)
    if len(contested) > limit:
        console.print("[dim]... and %d more in the Cannibalization sheet.[/dim]"
                      % (len(contested) - limit))


def _print_inverted(console: Console, keyword_map: KeywordMap, limit: int = 5) -> None:
    """Head terms aimed at products, long-tail terms aimed at categories."""
    inverted = [m for m in keyword_map.matches if m.inverted]
    if not inverted:
        return
    console.print("\n[bold]Inverted targeting[/bold] (%d): the healthy pattern is "
                  "heads -> collections, specifics -> products" % len(inverted))
    for match in sorted(inverted, key=lambda m: -m.volume_mid)[:limit]:
        console.print("  [yellow]%-34s[/yellow] %s -> %s"
                      % (match.keyword, match.inverted, match.owner))


def _resolve_pages(args: argparse.Namespace, console: Console):
    """Load match targets from an audit workbook, or crawl the site now."""
    if args.crawl:
        if not args.crawl.is_file():
            raise FileNotFoundError("--crawl file not found: %s" % args.crawl)
        return load_pages_from_xlsx(
            args.crawl, indexable_only=not args.include_non_indexable
        )

    if not args.url:
        raise ValueError("Pass --crawl <audit.xlsx> or --url <site> to match against.")

    from .config import Config
    from .crawler import Crawler
    from .urls import normalize_url

    config = Config.load(None, start_url=normalize_url(args.url),
                         max_urls=args.max_urls, delay=args.delay,
                         spellcheck=False)
    crawler = Crawler(config)
    try:
        with console.status("Crawling %s..." % args.url):
            results = crawler.run()
        return pages_from_crawl(results.values())
    finally:
        crawler.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``seocrawl keywords``."""
    args = build_keyword_parser().parse_args(argv)
    console = Console()

    try:
        rows, report = load_gkp(args.gkp)
    except (KeywordFileError, OSError) as exc:
        console.print("[red]Could not read the keyword file:[/red] %s" % exc)
        return 2

    if not args.quiet:
        _print_ingest(console, report, args.geo_note)

    rows = clean_keywords(
        rows, report,
        contains=args.contains or (),
        not_contains=args.not_contains or (),
        brands=args.brand or (),
        min_volume=args.min_volume,
    )
    if not rows:
        console.print("[red]No keywords left after filtering.[/red] Funnel: %s"
                      % report.funnel())
        return 1

    try:
        pages = _resolve_pages(args, console)
    except (ValueError, FileNotFoundError) as exc:
        console.print("[red]%s[/red]" % exc)
        return 2

    if not pages:
        console.print("[red]No indexable pages to match against.[/red]")
        return 1

    keyword_map = map_keywords(rows, pages, use_idf=args.use_idf)

    if args.output:
        output = args.output
    elif args.crawl and not args.standalone:
        output = args.crawl
    else:
        output = Path("keyword_map.xlsx")

    append_to = None if args.standalone else args.crawl
    write_keyword_sheets(output, keyword_map, append_to=append_to)

    _print_verdict(console, keyword_map, report.funnel())
    if not args.quiet:
        console.print("[dim]Matched against %d indexable pages%s.[/dim]"
                      % (keyword_map.page_count,
                         "" if keyword_map.idf_enabled
                         else "; --no-idf: every token weighted equally"))
        _print_gap_clusters(console, keyword_map)
        _print_cannibalization(console, keyword_map)
        _print_inverted(console, keyword_map)

    console.print("\n[bold]Keyword map:[/bold] %s" % output)
    console.print("[dim]Read the Gaps sheet first.[/dim]")
    return 0
