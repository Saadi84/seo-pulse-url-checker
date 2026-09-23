#!/usr/bin/env python3
"""Free, local CSV URL status checker. Requires Python 3; no pip installs.

Run: python seo_url_checker.py
or:  python seo_url_checker.py --input links.csv
"""

import argparse
import csv
import socket
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen

FIELDS = ["url", "result", "http_status", "final_url", "details"]
URL_HEADERS = {"url", "urls", "link", "links", "page", "page url", "page_url", "address", "website"}
USER_AGENT = "SEO-URL-Checker/1.0 (manual CSV audit)"


def pick_csv():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        file_name = filedialog.askopenfilename(
            title="Select CSV file containing website URLs",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        root.destroy()
        return file_name
    except (ImportError, RuntimeError, Exception) as exc:
        print(f"File picker unavailable ({exc}). Supply --input links.csv instead.", file=sys.stderr)
        return ""


def looks_like_url(value):
    value = value.strip()
    return value.startswith(("http://", "https://", "www."))


def load_urls(path):
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise ValueError("CSV file is empty.")
    header = [value.strip().lower() for value in rows[0]]
    index = next((i for i, name in enumerate(header) if name in URL_HEADERS), None)
    if index is not None:
        data_rows = rows[1:]
    elif any(looks_like_url(cell) for cell in rows[0]):
        data_rows = rows
        index = next(i for i, cell in enumerate(rows[0]) if looks_like_url(cell))
    else:
        # For uncommon header names, use the first column with a URL in the data.
        data_rows = rows[1:]
        index = next((i for row in data_rows for i, cell in enumerate(row) if looks_like_url(cell)), 0)
    urls = [row[index].strip() for row in data_rows if len(row) > index and row[index].strip()]
    if not urls:
        raise ValueError("No URLs found. Add a 'url' column with one address per row.")
    return urls


def classify(status):
    if 200 <= status <= 299:
        return "working"
    if status in (404, 410):
        return "not_found"
    return "needs_review"


def retry_after_seconds(value):
    """Interpret Retry-After numeric seconds or an HTTP date; None means not given."""
    if not value:
        return None
    try:
        return max(0, int(value.strip()))
    except (TypeError, ValueError, AttributeError):
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0, int((when - datetime.now(timezone.utc)).total_seconds()) + 1)
        except (ValueError, TypeError, OverflowError, IndexError):
            return None


def inspect_url(raw_url, timeout):
    url = raw_url.strip()
    if url.startswith("www."):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return {"url": raw_url, "result": "needs_review", "http_status": "", "final_url": "", "details": "Invalid URL; include http:// or https://"}
    req = Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        # We need the HTTP status and final location, not the entire page body.
        with urlopen(req, timeout=timeout) as response:
            status = response.status
            final_url = response.geturl()
        note = "Redirected" if final_url != url else ""
        return {"url": raw_url, "result": classify(status), "http_status": status, "final_url": final_url, "details": note}
    except HTTPError as error:
        # A 404/500 is still a valid HTTP response, so record the status.
        result = {"url": raw_url, "result": classify(error.code), "http_status": error.code,
                  "final_url": error.geturl(), "details": str(error.reason)}
        if error.code in (429, 503):
            result["_retry_after_seconds"] = retry_after_seconds(error.headers.get("Retry-After"))
        error.close()
        return result
    except (URLError, TimeoutError, socket.timeout, OSError, ValueError) as error:
        return {"url": raw_url, "result": "needs_review", "http_status": "", "final_url": "", "details": f"Request failed: {error}"}


def run(input_file, timeout=12, delay=0.15):
    csv_path = Path(input_file).expanduser().resolve()
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    urls = load_urls(csv_path)
    output_dir = csv_path.parent / f"{csv_path.stem}_url_check_results"
    output_dir.mkdir(exist_ok=True)
    files = {name: (output_dir / f"{name}.csv").open("w", encoding="utf-8-sig", newline="")
             for name in ("all_results", "working", "not_found", "needs_review")}
    counts = {"working": 0, "not_found": 0, "needs_review": 0}
    try:
        writers = {name: csv.DictWriter(stream, fieldnames=FIELDS) for name, stream in files.items()}
        for writer in writers.values():
            writer.writeheader()
        print(f"Checking {len(urls)} URLs from {csv_path.name}", flush=True)
        for number, url in enumerate(urls, start=1):
            result = inspect_url(url, timeout)
            writers["all_results"].writerow(result)
            writers[result["result"]].writerow(result)
            counts[result["result"]] += 1
            print(f"[{number}/{len(urls)}] {result['result']} {result['http_status']} {url}", flush=True)
            if number < len(urls) and delay:
                time.sleep(delay)
    finally:
        for stream in files.values():
            stream.close()
    print(f"\nDone. Working: {counts['working']}; Not found (404/410): {counts['not_found']}; Needs review: {counts['needs_review']}")
    print(f"Results saved to: {output_dir}")
    return output_dir, counts


def main():
    parser = argparse.ArgumentParser(description="Check a CSV of URLs and create separate working, not-found and needs-review CSV reports.")
    parser.add_argument("--input", help="Path to the CSV file; without this, a file picker opens")
    parser.add_argument("--timeout", type=float, default=12, help="Timeout in seconds for each URL (default 12)")
    parser.add_argument("--delay", type=float, default=0.15, help="Pause between requests in seconds (default 0.15)")
    args = parser.parse_args()
    if args.timeout <= 0 or args.delay < 0:
        parser.error("--timeout must be positive and --delay cannot be negative")
    csv_file = args.input or pick_csv()
    if not csv_file:
        print("No CSV selected. Nothing was checked.")
        return 1
    try:
        run(csv_file, timeout=args.timeout, delay=args.delay)
    except (OSError, csv.Error, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
