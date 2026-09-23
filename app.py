#!/usr/bin/env python3
"""Free localhost dashboard for auditing URL status from uploaded CSVs.

Requires Python 3.9+ and the Python standard library only.
Run: python app.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import socket
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from seo_url_checker import FIELDS, URL_HEADERS, inspect_url, looks_like_url

HOST = "127.0.0.1"  # accessible only on this computer
PORT = int(os.environ.get("SEO_DASHBOARD_PORT", "8765"))
MAX_UPLOAD = 2 * 1024 * 1024
MAX_URLS = 5000
WORKERS = 4
# Retry mode: one request at a time; never hammer a rate-limiting host.
RETRY_HOST_DELAY = 5.0
RETRY_429_PAUSE = 15.0
MAX_RETRY_AFTER = 120.0
BASE = Path(__file__).resolve().parent
STATIC = BASE / "static" / "index.html"
EXPORT_DIR = BASE / "reports"
EXPORT_DIR.mkdir(exist_ok=True)

lock = threading.RLock()
job = None


def extract_urls(csv_text: str) -> list[str]:
    """Accept a URL column or a one-column CSV without a heading."""
    if not csv_text.strip():
        raise ValueError("The CSV is empty.")
    try:
        reader = csv.reader(io.StringIO(csv_text.lstrip("\ufeff"), newline=""))
        rows = [[cell.strip() for cell in row] for row in reader if any(cell.strip() for cell in row)]
    except csv.Error as exc:
        raise ValueError(f"Unable to read CSV: {exc}") from exc
    if not rows:
        raise ValueError("The CSV is empty.")
    header = [cell.lower() for cell in rows[0]]
    index = next((i for i, name in enumerate(header) if name in URL_HEADERS), None)
    if index is not None:
        data = rows[1:]
    elif any(looks_like_url(cell) for cell in rows[0]):
        index = next(i for i, value in enumerate(rows[0]) if looks_like_url(value))
        data = rows
    else:
        data = rows[1:]
        index = next((i for row in data[:20] for i, value in enumerate(row) if looks_like_url(value)), 0)
    urls = [row[index] for row in data if len(row) > index and row[index]]
    if not urls:
        raise ValueError("No URLs found. Add a 'url' column with one address per row.")
    if len(urls) > MAX_URLS:
        raise ValueError(f"Too many URLs ({len(urls):,}). Maximum per scan: {MAX_URLS:,}.")
    return urls


def load_previous_report(csv_text: str) -> list[dict]:
    """Read a previous all_results.csv, preserving non-review rows unchanged."""
    if not csv_text.strip():
        raise ValueError("The previous results CSV is empty.")
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"), newline=""))
    if not reader.fieldnames or not {"url", "result", "http_status"}.issubset(
        {column.strip().lower() for column in reader.fieldnames if column}
    ):
        raise ValueError("For Retry, upload the previously downloaded all_results.csv (columns: url, result, http_status).")
    if not set(FIELDS).issubset(set(reader.fieldnames)):
        raise ValueError("The previous results file is missing required columns. Upload all_results.csv from SEO Pulse.")
    rows = []
    for row in reader:
        if not any((value or "").strip() for value in row.values() if isinstance(value, str)):
            continue
        if any(row.get(field) is None for field in FIELDS):
            raise ValueError("The previous report contains incomplete rows.")
        result = row["result"].strip().lower()
        if result not in ("working", "not_found", "needs_review") or not row["url"].strip():
            raise ValueError("Invalid result or missing URL in previous report.")
        rows.append({field: row[field] for field in FIELDS} | {"result": result})
        if len(rows) > MAX_URLS:
            raise ValueError(f"Too many URLs. Maximum: {MAX_URLS:,}.")
    if not rows:
        raise ValueError("The previous report contains no URLs.")
    if not any(row["result"] == "needs_review" for row in rows):
        raise ValueError("This report has no Needs Review URLs to retry.")
    return rows


def new_job(name: str, rows: list[dict], mode: str) -> dict:
    target_total = len(rows) if mode == "scan" else sum(r["result"] == "needs_review" for r in rows)
    return {"id": uuid.uuid4().hex[:16], "state": "running", "name": Path(name).name[:100],
            "mode": mode, "total": len(rows), "target_total": target_total, "completed": 0,
            "attempted": 0, "skipped": 0,
            "counts": {category: sum(row["result"] == category for row in rows) if mode == "retry" else 0
                       for category in ("working", "not_found", "needs_review")},
            "recent": [], "rows": rows, "error": "", "notice": "",
            "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": ""}


def record_result(current: dict, index: int, result: dict, *, attempted=True, skipped=False) -> None:
    result.pop("_retry_after_seconds", None)
    category = result.get("result", "needs_review")
    if category not in ("working", "not_found", "needs_review"):
        category = "needs_review"
        result["result"] = category
    with lock:
        previous = current["rows"][index]
        if previous is not None:
            current["counts"][previous["result"]] -= 1
        current["rows"][index] = result
        current["counts"][category] += 1
        current["completed"] += 1
        current["attempted"] += int(attempted)
        current["skipped"] += int(skipped)
        current["recent"].insert(0, result)
        del current["recent"][40:]


def save_reports(current: dict) -> None:
    scan_dir = EXPORT_DIR / current["id"]
    scan_dir.mkdir(parents=True, exist_ok=True)
    rows = current["rows"]
    export_csv(rows, scan_dir / "all_results.csv")
    for category in ("working", "not_found", "needs_review"):
        export_csv([r for r in rows if r["result"] == category], scan_dir / f"{category}.csv")
    with lock:
        current["state"] = "done"
        current["finished_at"] = datetime.now(timezone.utc).isoformat()


def retry_one(current: dict, index: int, last_request: dict, blocked: dict) -> None:
    previous = current["rows"][index]
    url = previous["url"]
    host = (urlsplit("https://" + url if url.startswith("www.") else url).netloc or url).lower()
    if host in blocked:
        result = dict(previous)
        result["details"] = ("Not retried: website still limits automated requests; "
                             "use approved access or website logs. " + blocked[host])[:400]
        record_result(current, index, result, attempted=False, skipped=True)
        return
    elapsed = time.monotonic() - last_request.get(host, -10**9)
    if elapsed < RETRY_HOST_DELAY:
        time.sleep(RETRY_HOST_DELAY - elapsed)
    last_request[host] = time.monotonic()
    try:
        result = inspect_url(url, 12)
        if result.get("http_status") == 429:
            after = result.get("_retry_after_seconds")
            if after is not None and after > MAX_RETRY_AFTER:
                blocked[host] = f"Server Retry-After is {after}s; no further requests were made."
            else:
                # Retry the same URL once, with a pause; respect server Retry-After if supplied.
                time.sleep(max(RETRY_429_PAUSE, after or 0))
                last_request[host] = time.monotonic()
                result = inspect_url(url, 12)
                if result.get("http_status") == 429:
                    after2 = result.get("_retry_after_seconds")
                    blocked[host] = (f"Server Retry-After is {after2}s." if after2 is not None else
                                     "Repeated HTTP 429 after a slower retry.")
            if result.get("http_status") == 429:
                result["details"] = "Rate limited again. " + blocked.get(host, "HTTP 429")
        elif result.get("http_status") in (403,):
            result["details"] = "Access denied by website (403); use approved access or server logs."
    except Exception as exc:
        result = {"url": url, "result": "needs_review", "http_status": "", "final_url": "",
                  "details": f"Retry request failed: {type(exc).__name__}"}
    record_result(current, index, result)


def process_retry_job(current: dict) -> None:
    try:
        last_request, blocked = {}, {}
        for index, row in enumerate(current["rows"]):
            if row["result"] == "needs_review":
                retry_one(current, index, last_request, blocked)
        if blocked:
            current["notice"] = ("Some websites continued returning HTTP 429. "
                                 "Unchecked URLs remain Needs Review; request approved access or server logs.")
        save_reports(current)
    except Exception as exc:
        with lock:
            current["state"] = "failed"
            current["error"] = f"Could not complete the retry: {type(exc).__name__}: {exc}"


def csv_safe(value: object) -> str:
    """Prevent spreadsheet formula execution in files opened in Excel/Sheets."""
    string = str(value if value is not None else "")
    if string and (string[0] in "=+-@\t\r\n" or string[0].isspace()):
        return "'" + string
    return string


def export_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_safe(row.get(key, "")) for key in FIELDS})


def process_job(current: dict, urls: list[str]) -> None:
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            pending = {pool.submit(inspect_url, url, 10): (i, url) for i, url in enumerate(urls)}
            for future in as_completed(pending):
                index, raw_url = pending[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"url": raw_url, "result": "needs_review", "http_status": "",
                              "final_url": "", "details": f"Unexpected check error: {type(exc).__name__}"}
                record_result(current, index, result)
        save_reports(current)
    except Exception as exc:
        with lock:
            current["state"] = "failed"
            current["error"] = f"Could not complete the scan: {type(exc).__name__}: {exc}"


def snapshot() -> dict:
    with lock:
        if not job:
            return {"state": "idle", "mode": "scan", "completed": 0, "total": 0,
                    "target_total": 0, "attempted": 0, "skipped": 0, "notice": "",
                    "counts": {"working": 0, "not_found": 0, "needs_review": 0}, "recent": []}
        result = {key: job[key] for key in ("id", "state", "name", "mode", "completed", "total", "target_total", "attempted", "skipped", "notice", "error", "started_at", "finished_at")}
        result["counts"] = dict(job["counts"])
        result["recent"] = [dict(row) for row in job["recent"]]
        return result


class Handler(BaseHTTPRequestHandler):
    server_version = "SEOAuditLocal/2.0"

    def log_message(self, format, *args):
        # Skip rapid status polling in terminal logs.
        if not self.path.startswith("/api/status"):
            super().log_message(format, *args)

    def _headers(self, status=200, type_="application/json; charset=utf-8", length=None, attachment=None):
        self.send_response(status)
        self.send_header("Content-Type", type_)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, length=len(body))
        self.wfile.write(body)

    def _allow_host(self):
        # Defense in depth against DNS rebinding on a localhost-only tool.
        hostname = self.headers.get("Host", "").split(":")[0].lower()
        return hostname in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._allow_host():
            return self._json({"error": "Only localhost connections are allowed."}, 403)
        path = urlsplit(self.path)
        if path.path in ("/", "/index.html"):
            page = STATIC.read_bytes()
            self._headers(type_="text/html; charset=utf-8", length=len(page))
            return self.wfile.write(page)
        if path.path == "/api/status":
            return self._json(snapshot())
        if path.path == "/api/results":
            args = parse_qs(path.query)
            category = args.get("category", ["all"])[0]
            search = args.get("search", [""])[0].strip().lower()[:150]
            if category not in ("all", "working", "not_found", "needs_review"):
                return self._json({"error": "Invalid category"}, 400)
            with lock:
                rows = list(job["rows"]) if job else []
            filtered = [r for r in rows if r is not None and
                        (category == "all" or r["result"] == category) and
                        (not search or search in r["url"].lower() or search in str(r["http_status"]))]
            return self._json({"rows": filtered[:200], "matching": len(filtered), "display_limit": 200})
        if path.path.startswith("/api/download/"):
            kind = path.path.removeprefix("/api/download/")
            if kind not in ("all_results", "working", "not_found", "needs_review"):
                return self._json({"error": "Invalid report"}, 404)
            with lock:
                current = job
                if not current or current["state"] != "done":
                    return self._json({"error": "The scan has not finished."}, 409)
                scan_id = current["id"]
            file_path = EXPORT_DIR / scan_id / (kind + ".csv")
            if not file_path.is_file():
                return self._json({"error": "Report not found."}, 404)
            data = file_path.read_bytes()
            self._headers(type_="text/csv; charset=utf-8", length=len(data), attachment=kind + ".csv")
            return self.wfile.write(data)
        return self._json({"error": "Not found"}, 404)

    def do_POST(self):
        global job
        if not self._allow_host():
            return self._json({"error": "Only localhost connections are allowed."}, 403)
        origin = self.headers.get("Origin", "")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self._json({"error": "Cross-origin requests are not allowed."}, 403)
        endpoint = urlsplit(self.path).path
        if endpoint not in ("/api/start", "/api/retry"):
            return self._json({"error": "Not found"}, 404)
        if self.headers.get("Content-Type", "").split(";")[0].lower() != "application/json":
            return self._json({"error": "Expected JSON."}, 415)
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._json({"error": "Invalid upload size."}, 400)
        if size <= 0 or size > MAX_UPLOAD * 2:
            return self._json({"error": "CSV too large. Limit: 2 MB."}, 413)
        try:
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object.")
            name = payload.get("filename", "urls.csv")
            data = payload.get("csv", "")
            if not isinstance(data, str) or len(data.encode("utf-8")) > MAX_UPLOAD:
                return self._json({"error": "CSV too large. Limit: 2 MB."}, 413)
            if not isinstance(name, str):
                return self._json({"error": "Invalid file name."}, 400)
            if endpoint == "/api/start":
                urls = extract_urls(data)
                rows = [None] * len(urls)
                mode = "scan"
            else:
                mode = "retry"
                if data:
                    rows = load_previous_report(data)
                else:
                    with lock:
                        if not job or job["state"] != "done":
                            raise ValueError("Upload a previous all_results.csv, or complete a scan first.")
                        rows = [dict(r) for r in job["rows"]]
                        name = job["name"]
                    if not any(r["result"] == "needs_review" for r in rows):
                        raise ValueError("The last report has no Needs Review URLs to retry.")
                urls = []
        except (json.JSONDecodeError, UnicodeError, ValueError, TypeError, AttributeError, csv.Error) as exc:
            return self._json({"error": str(exc)}, 400)
        with lock:
            if job and job["state"] == "running":
                return self._json({"error": "A scan is already running. Please check progress."}, 409)
            job = new_job(name, rows, mode)
            current = job
        if mode == "retry":
            threading.Thread(target=process_retry_job, args=(current,), daemon=True).start()
        else:
            threading.Thread(target=process_job, args=(current, urls), daemon=True).start()
        return self._json({"ok": True, "total": current["total"],
                           "target_total": current["target_total"], "id": current["id"]}, 202)



def main():
    if not STATIC.exists():
        raise SystemExit("Missing static/index.html. Keep all app files together.")
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        raise SystemExit(f"Cannot open localhost port {PORT}: {exc}") from exc
    address = f"http://{HOST}:{PORT}"
    print("\n" + "=" * 55)
    print(" SEO URL Checker — Free Local Dashboard")
    print(f" Open this address in your browser: {address}")
    print(" No account, hosting or paid API required")
    print(" Press Ctrl+C in this window to stop the dashboard")
    print("=" * 55 + "\n", flush=True)
    # Open only on user's computer, after the local server starts.
    threading.Timer(0.9, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
