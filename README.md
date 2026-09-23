# SEO Pulse — free localhost dashboard

A CSV URL audit tool. Checks URL HTTP status and provides separate CSV reports. **No pip installs, API keys, accounts, or hosting costs.** Python 3.9 or newer required.

## Launch

1. Extract the entire ZIP somewhere on your own computer; keep `app.py`, `seo_url_checker.py`, and `static/index.html` together.
2. Windows: double-click `start_windows.bat` (or open Command Prompt in the folder and run `py -3 app.py`). macOS/Linux: run `python3 app.py` in a terminal within this folder.
3. Open `http://127.0.0.1:8765` if your browser does not open automatically. Keep the terminal open while using the dashboard.
4. Upload a `.csv` file containing a `url` column, click **Start checking URLs**, and download results at the bottom after completion.
5. Press Ctrl+C in the terminal to stop. You can re-open the app next time with the same command.

## Example input

```csv
url
https://example.com/
https://example.com/not-a-real-page
https://www.wikipedia.org/
```

One-column CSVs without a header are also accepted. URLs beginning with `www.` are supported.

## Reports

- `all_results.csv`: every scanned URL and its status, final URL and notes.
- `working.csv`: HTTP status 200–299 (may include redirected pages; inspect the **final URL** and **details** columns).
- `not_found.csv`: 404 and 410.
- `needs_review.csv`: other HTTP responses (including 403, 429 and 5xx), timeouts, inaccessible websites and invalid URLs.

Saved automatically inside `reports/<scan_id>/` in your dashboard folder. The browser also has download buttons. Prior report folders stay on your computer.

## Notes

- The server binds to `127.0.0.1` only; another computer cannot access it over the network. To use it on another employee's computer, copy the ZIP there and start it locally.
- The dashboard makes HTTP requests **directly to the URLs in your CSV**; those sites see requests from your computer, and internet access is needed to check public pages.
- Checks are HTTP requests, not a Chrome/Google search or a complete visual rendering. Some sites block automated visits or return HTTP 200 on an error page. Examine 'needs review' and important edge cases manually.
- Four workers check in parallel; a website may rate-limit or forbid automated access. Check only URLs you are authorized to audit and respect the site's rate limits.
- Default maximum: 5,000 URLs and a 2 MB CSV per scan. Reports are local files. One scan at a time.
- If port 8765 is in use, set the `SEO_DASHBOARD_PORT` environment variable to another port and open the matching localhost address.

## NEW — Retry Needs Review (existing report supported)

1. Open the upgraded dashboard and upload the **all_results.csv** you downloaded from the earlier scan (not the `needs_review.csv`, because the full report preserves your other results).
2. Click **Retry Needs Review**. Only rows marked `needs_review` in that report are eligible for a new HTTP check. The existing working and 404/410 results remain in the updated complete report.
3. Alternatively, after a new scan finishes, click **Retry Needs Review** without uploading an earlier results report, to retry unresolved URLs from the latest scan.
4. Download the refreshed **All results** CSV (and the refreshed category CSVs). The previous reports are preserved in their old `reports/<scan_id>/` folder.

**Rate-limit safeguards:** Retry is sequential and waits at least 5 seconds between requests to the same host. After HTTP 429 it pauses and retries that URL once if the website's `Retry-After` does not exceed 120 seconds. If the website requests a longer wait, or the second attempt is also 429, the tool makes no more requests to that host in that retry job; remaining host URLs are marked "Not retried" and stay under Needs Review. The dashboard distinguishes URLs actually requested from ones skipped. This cannot bypass access restrictions or guarantee a definitive status if a site disallows automated checks. For persistent rate limits, ask your company website administrator for approved access or server-side logs. If checking a third-party site, obtain its permission.

Note: The original scan still runs with four workers. When a site is rate-limiting, use the controlled Retry button instead of repeatedly starting whole-file scans.
