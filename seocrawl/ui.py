"""A local web UI for seocrawl: ``seocrawl ui``.

The crawler has to run on this machine -- it makes thousands of requests to the
audited site and writes files here -- so the UI is a small server on localhost
rather than anything hosted. It is built on the standard library only, keeping
the project's dependency list as short as it was.

One job runs at a time, deliberately: two concurrent crawls would share nothing
except the target site's patience.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from .config import Config
from .crawler import CrawlBlocked, Crawler
from .export import report_name, write_html_report, write_keyword_sheets, write_workbook
from .findings import HIGH, LOW, MED, Audit, anchor_report
from .ui_page import PAGE
from .urls import normalize_url, registrable_host


@dataclass
class Job:
    """State of the one crawl or keyword job the UI is running."""

    kind: str = "crawl"                     # "crawl" | "keywords"
    state: str = "idle"                     # idle | running | done | error | stopped
    label: str = ""
    started: float = 0.0
    finished: float = 0.0
    done: int = 0
    total: int = 0
    current: str = ""
    message: str = ""
    error: str = ""
    #: Recorded during the run; the terminal state is only set once the job has
    #: finished writing. Flipping ``state`` early would make the runner look
    #: idle while the report was still being written.
    stopped_early: bool = False
    outputs: list[dict[str, str]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    funnel: str = ""

    @property
    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.started if self.started else 0.0

    def as_dict(self) -> dict[str, Any]:
        data = {
            "kind": self.kind, "state": self.state, "label": self.label,
            "done": self.done, "total": self.total, "current": self.current,
            "message": self.message, "error": self.error,
            "elapsed": round(self.elapsed, 1),
            "outputs": self.outputs, "stats": self.stats,
            "findings": self.findings, "funnel": self.funnel,
        }
        return data


class Runner:
    """Owns the current job and the thread running it."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.job = Job()
        self.crawler: Crawler | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self.job.state == "running"

    def start(self, target, label: str, kind: str) -> bool:
        """Begin a job unless one is already running."""
        with self._lock:
            if self.busy:
                return False
            self.job = Job(kind=kind, state="running", label=label, started=time.time())
        self._thread = threading.Thread(target=self._wrap, args=(target,), daemon=True)
        self._thread.start()
        return True

    def _wrap(self, target) -> None:
        try:
            target(self.job)
            if self.job.state == "running":
                self.job.state = "stopped" if self.job.stopped_early else "done"
        except CrawlBlocked as exc:
            self.job.state = "error"
            self.job.error = str(exc)
        except Exception as exc:                    # surfaced in the UI, not swallowed
            self.job.state = "error"
            self.job.error = "%s: %s" % (type(exc).__name__, exc)
            traceback.print_exc()
        finally:
            self.job.finished = time.time()
            crawler, self.crawler = self.crawler, None
            if crawler is not None:
                try:
                    crawler.close()
                except Exception:
                    pass

    def stop(self) -> bool:
        """Ask the running crawl to finish early and still write its report."""
        crawler = self.crawler
        if crawler is None or not self.busy:
            return False
        crawler.request_stop()
        self.job.message = "Stopping -- finishing in-flight requests, then writing the report."
        return True


# --- the jobs ---------------------------------------------------------------


def _finding_rows(findings, limit: int = 40) -> list[dict[str, Any]]:
    """Findings shaped for the page."""
    return [
        {
            "severity": f.severity, "issue": f.issue, "count": f.count,
            "detail": f.detail, "why": f.why_it_matters,
            "urls": f.urls[:8],
        }
        for f in findings[:limit]
    ]


def crawl_job(runner: Runner, options: dict[str, Any]):
    """Build the callable that runs one crawl."""

    def run(job: Job) -> None:
        url = normalize_url(str(options.get("url", "")).strip())
        if not url:
            raise ValueError("Enter a site URL.")

        max_urls = options.get("max_urls") or 10_000
        config = Config.load(
            None,
            start_url=url,
            max_urls=int(max_urls),
            delay=float(options.get("delay") or 0.7),
            workers=int(options.get("workers") or 4),
            image_sample_per_template=int(options.get("image_sample") or 25),
            spellcheck=bool(options.get("spellcheck", True)),
            psi=bool(options.get("psi")),
            psi_api_key=options.get("psi_key") or os.environ.get("PSI_API_KEY"),
            html_report=True,
            recrawl=bool(options.get("recrawl", True)),
            output_dir=runner.output_dir,
            cache_dir=runner.output_dir / ".cache",
        )

        job.label = registrable_host(url)
        job.total = config.max_urls
        job.message = "Reading robots.txt and sitemaps..."

        crawler = Crawler(config)
        runner.crawler = crawler

        def progress(current: str, done: int, total: int) -> None:
            job.done, job.total, job.current = done, max(total, done), current
            job.message = "Crawling"

        crawler.on_progress = progress
        started = time.time()
        pages = crawler.run()

        job.stopped_early = crawler.stop_requested.is_set()
        if not pages:
            raise ValueError("No pages were crawled. Check the URL and robots.txt.")

        job.message = "Analysing %d pages..." % len(pages)
        audit = Audit(pages, crawler.links, crawler.sitemap_urls,
                      near_duplicate_distance=config.near_duplicate_distance,
                      destination_anchors=config.destination_anchors)
        findings = audit.run()

        psi_results = []
        if config.psi:
            from .psi import psi_findings, run_psi

            job.message = "Running PageSpeed Insights..."
            psi_results = run_psi(list(pages.values()), config.psi_api_key,
                                  top=config.psi_top, strategy=config.psi_strategy,
                                  delay=config.psi_delay)
            findings.extend(psi_findings(psi_results))
            findings.sort(key=lambda f: f.sort_key())

        stats = audit.stats(findings)
        job.message = "Writing the report..."

        xlsx = runner.output_dir / report_name(url, "xlsx")
        write_workbook(xlsx, list(pages.values()), findings, stats,
                       anchor_report(crawler.links), psi_results,
                       site=job.label, duration_s=time.time() - started)
        html = runner.output_dir / report_name(url, "html")
        write_html_report(html, stats, findings, site=job.label, psi_results=psi_results)

        counts = {s: sum(1 for f in findings if f.severity == s) for s in (HIGH, MED, LOW)}
        job.stats = {
            "URLs crawled": stats.total_urls,
            "Indexable": stats.indexable,
            "Products": stats.products,
            "Orphans": stats.orphans,
            "Ghosts": stats.ghosts,
            "High": counts[HIGH], "Medium": counts[MED], "Low": counts[LOW],
        }
        job.findings = _finding_rows(findings)
        job.outputs = [
            {"label": "spreadsheet", "path": str(xlsx), "kind": "xlsx",
             "download": "/api/download?path=%s" % quote(xlsx.name)},
            {"label": "report", "path": str(html), "kind": "html",
             "view": "/api/view?path=%s" % quote(html.name)},
        ]
        job.message = "Done"

    return run


def keywords_job(runner: Runner, options: dict[str, Any], csv_bytes: bytes | None):
    """Build the callable that joins a keyword export against an audit."""

    def run(job: Job) -> None:
        from .kwmatch import CANNIBAL, GAP, OWNED, WEAK, load_pages_from_xlsx, map_keywords
        from .kwplanner import clean_keywords, load_gkp

        crawl_path = Path(str(options.get("crawl") or "").strip())
        if not crawl_path.is_file():
            raise ValueError("Pick an audit spreadsheet to join against.")

        if csv_bytes:
            gkp_path = runner.output_dir / ".uploaded_keywords.csv"
            gkp_path.write_bytes(csv_bytes)
        else:
            gkp_path = Path(str(options.get("gkp") or "").strip())
            if not gkp_path.is_file():
                raise ValueError("Choose a Keyword Planner export.")

        job.label = crawl_path.name
        job.message = "Reading the keyword export..."
        rows, report = load_gkp(gkp_path)
        job.total = len(rows)

        rows = clean_keywords(
            rows, report,
            contains=[t for t in (options.get("contains") or "").split(",") if t.strip()],
            not_contains=[t for t in (options.get("not_contains") or "").split(",") if t.strip()],
            brands=[t for t in (options.get("brand") or "").split(",") if t.strip()],
            min_volume=int(options.get("min_volume") or 0),
        )
        job.funnel = report.funnel()
        if not rows:
            raise ValueError("No keywords left after filtering. Funnel: %s" % report.funnel())

        job.message = "Matching %d keywords against the crawl..." % len(rows)
        pages = load_pages_from_xlsx(crawl_path)
        if not pages:
            raise ValueError("That spreadsheet has no indexable pages to match against.")

        keyword_map = map_keywords(rows, pages, use_idf=bool(options.get("use_idf", True)))
        job.done = len(rows)

        write_keyword_sheets(crawl_path, keyword_map, append_to=crawl_path)

        counts = keyword_map.counts
        clusters = [c for c in keyword_map.clusters if c.size > 1][:8]
        job.stats = {
            "Keywords": len(rows),
            "Owned": counts[OWNED], "Weak": counts[WEAK],
            "Gaps": counts[GAP], "Cannibal": counts[CANNIBAL],
            "Pages matched": keyword_map.page_count,
        }
        job.findings = [
            {
                "severity": "GAP", "issue": c.head, "count": c.size,
                "detail": "combined volume %s" % "{:,}".format(c.volume),
                "why": c.suggested_action(),
                "urls": [k.keyword for k in
                         sorted(c.keywords, key=lambda m: -m.volume_mid)[:6]],
            }
            for c in clusters
        ]
        job.outputs = [{"label": "spreadsheet (3 sheets added)",
                        "path": str(crawl_path), "kind": "xlsx",
                        "download": "/api/download?path=%s" % quote(crawl_path.name)}]
        job.message = "Done"

    return run


# --- http -------------------------------------------------------------------


def has_file_handler(suffix: str) -> bool:
    """True when this desktop has an application registered for ``suffix``.

    Only meaningful on Windows, where ``os.startfile`` happily *succeeds* for an
    unregistered extension -- it pops an "open with" dialog, or does nothing
    visible at all. Reporting that as "opened" is a lie, and a machine with no
    spreadsheet app installed is not unusual.
    """
    if sys.platform != "win32":
        return True
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, suffix) as key:
            value, _type = winreg.QueryValueEx(key, "")
            return bool(value)
    except OSError:
        return False


def reveal_in_file_manager(target: Path) -> None:
    """Show a file in the desktop's file manager, selected."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(target)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target.parent)])


def open_in_os(path: str) -> dict[str, Any]:
    """Open a file with whatever the desktop uses for it.

    Falls back to revealing the file in the file manager when nothing is
    registered for its type, and says which of the two happened so the page can
    tell the truth rather than claiming success.
    """
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(path)

    if not has_file_handler(target.suffix.lower()):
        reveal_in_file_manager(target)
        return {
            "opened": False,
            "revealed": True,
            "path": str(target.resolve()),
            "note": "Nothing on this machine is registered to open %s files, so "
                    "the folder was opened instead." % target.suffix,
        }

    if sys.platform == "win32":
        os.startfile(str(target))                      # noqa: S606 - local, user-initiated
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])
    return {"opened": True, "revealed": False, "path": str(target.resolve())}


class Handler(BaseHTTPRequestHandler):
    """Routes for the single-page app."""

    runner: Runner = None          # type: ignore[assignment]
    server_version = "seocrawl"

    def log_message(self, *args):  # keep the console for the crawl, not for GETs
        pass

    # --- helpers ---

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _from_loopback(self) -> bool:
        """True when the request came from this machine."""
        import ipaddress

        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # --- routes ---

    def _safe_path(self, raw: str) -> Path:
        """Resolve a requested path, refusing anything outside the reports folder.

        These endpoints are plain GETs, so any page the browser visits can
        trigger one against localhost. It cannot *read* the response across
        origins, but serving arbitrary files on request is not a thing to leave
        open: the reports directory is the only place worth exposing.
        """
        root = self.runner.output_dir.resolve()
        target = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()

        if root != target and root not in target.parents:
            raise PermissionError("Only files in %s can be served." % root)
        if not target.is_file():
            raise FileNotFoundError(raw)
        return target

    def _send_file(self, target: Path, *, as_attachment: bool) -> None:
        """Stream a report to the browser, to download or to view."""
        content_types = {
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".html": "text/html; charset=utf-8",
            ".csv": "text/csv; charset=utf-8",
        }
        payload = target.read_bytes()

        self.send_response(200)
        self.send_header("Content-Type",
                         content_types.get(target.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(payload)))
        if as_attachment:
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % target.name)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/api/status":
            self._json(self.runner.job.as_dict())
        elif route == "/api/reports":
            self._json({"reports": self._recent_reports()})
        elif route in ("/api/download", "/api/view"):
            query = parse_qs(parsed.query)
            try:
                target = self._safe_path((query.get("path") or [""])[0])
            except (PermissionError, FileNotFoundError, OSError) as exc:
                self._send(404, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
                return
            self._send_file(target, as_attachment=route == "/api/download")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path

        try:
            if route == "/api/crawl":
                options = json.loads(self._body() or b"{}")
                started = self.runner.start(
                    crawl_job(self.runner, options),
                    label=str(options.get("url", "")), kind="crawl",
                )
                self._json({"started": started,
                            "error": "" if started else "A job is already running."})

            elif route == "/api/keywords":
                options = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                options["use_idf"] = options.get("use_idf", "1") != "0"
                csv_bytes = self._body() or None
                started = self.runner.start(
                    keywords_job(self.runner, options, csv_bytes),
                    label="keywords", kind="keywords",
                )
                self._json({"started": started,
                            "error": "" if started else "A job is already running."})

            elif route == "/api/stop":
                self._json({"stopped": self.runner.stop()})

            elif route == "/api/open":
                # This hands a path to the desktop's file handler, so it is
                # restricted to requests that came from this machine. Harmless
                # on the default loopback bind; the guard matters when someone
                # passes --host to reach the UI from another device.
                if not self._from_loopback():
                    self._json({"error": "Opening files is only allowed from this "
                                         "machine."}, status=403)
                    return
                payload = json.loads(self._body() or b"{}")
                self._json(open_in_os(str(payload.get("path", ""))))

            else:
                self._send(404, b"not found", "text/plain")

        except Exception as exc:
            self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, status=400)

    def _recent_reports(self) -> list[dict[str, str]]:
        """Audit workbooks already sitting in the output directory."""
        directory = self.runner.output_dir
        files = sorted(directory.glob("audit_*.xlsx"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        return [
            {
                "name": path.name,
                "path": str(path),
                "download": "/api/download?path=%s" % quote(path.name),
                "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime)),
                "size": "%.0f KB" % (path.stat().st_size / 1024),
            }
            for path in files[:25]
        ]


def serve(host: str = "127.0.0.1", port: int = 8724,
          output_dir: Path | None = None, open_browser: bool = True) -> int:
    """Run the UI until interrupted."""
    directory = Path(output_dir or Path.cwd())
    directory.mkdir(parents=True, exist_ok=True)

    Handler.runner = Runner(directory)
    server = ThreadingHTTPServer((host, port), Handler)
    address = "http://%s:%d/" % (host, port)

    print("seocrawl UI  ->  %s" % address)
    print("Reports are written to %s" % directory.resolve())
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: bound to %s, so anyone who can reach this machine on the "
              "network can start crawls with it. Opening files stays restricted "
              "to this machine." % host)
    print("Press Ctrl+C to stop.\n")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(address)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
    return 0
