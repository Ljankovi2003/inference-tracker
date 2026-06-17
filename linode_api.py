#!/usr/bin/env python3
"""Robust, throttled client for the public Linode / Akamai Connected Cloud API v4.

Every collector (demand.py, rates.py, view_feed.py) fetches through here so that
pagination, retry/backoff, and rate-limiting live in ONE place and can't drift.

Robustness guarantees:
  * Pagination — follows the API's own `pages` count to the end; never stops at
    page 1 (the bug that hid 31 of 33 regions). Verifies the final row count
    against the API-reported `results` and warns loudly on any mismatch.
  * Throttling — a process-wide minimum interval between requests (shared across
    all collectors in a run), so we stay a polite, well-behaved client.
  * Retries — exponential backoff on timeouts / connection errors / 5xx, and
    honors `Retry-After` on HTTP 429 (rate-limited). Non-retryable 4xx raise.

No authentication is required for the endpoints used here.

Tunable via env vars:
  AKAM_MIN_INTERVAL  seconds between requests   (default 0.4)
  AKAM_MAX_RETRIES   attempts per request        (default 5)
  AKAM_PAGE_SIZE     rows per page (25..500)     (default 500)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.linode.com/v4"

MIN_INTERVAL = float(os.environ.get("AKAM_MIN_INTERVAL", "0.4"))
MAX_RETRIES = int(os.environ.get("AKAM_MAX_RETRIES", "5"))
PAGE_SIZE = max(25, min(500, int(os.environ.get("AKAM_PAGE_SIZE", "500"))))
TIMEOUT = 30
BACKOFF_BASE = 1.6          # seconds; delay = BACKOFF_BASE ** attempt
MAX_PAGES = 10_000          # infinite-loop backstop

_last_ts = 0.0              # monotonic timestamp of the last request (process-wide)


def _throttle():
    """Block until at least MIN_INTERVAL has elapsed since the last request."""
    global _last_ts
    wait = MIN_INTERVAL - (time.monotonic() - _last_ts)
    if wait > 0:
        time.sleep(wait)
    _last_ts = time.monotonic()


def _request(url, user_agent):
    """Single throttled GET with retry/backoff. Returns parsed JSON."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            last_err = e
            transient = e.code == 429 or 500 <= e.code < 600
            if transient and attempt < MAX_RETRIES:
                ra = e.headers.get("Retry-After") if e.headers else None
                delay = float(ra) if ra and ra.isdigit() else BACKOFF_BASE ** attempt
                print(f"  [linode_api] HTTP {e.code} on {url} — retry "
                      f"{attempt}/{MAX_RETRIES} in {delay:.1f}s", file=sys.stderr)
                time.sleep(delay)
                continue
            raise  # non-retryable (e.g. 400/401/404) or out of retries
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                json.JSONDecodeError) as e:
            last_err = e
            if attempt < MAX_RETRIES:
                delay = BACKOFF_BASE ** attempt
                print(f"  [linode_api] {type(e).__name__} on {url} — retry "
                      f"{attempt}/{MAX_RETRIES} in {delay:.1f}s", file=sys.stderr)
                time.sleep(delay)
                continue
            raise
    raise last_err  # pragma: no cover


def _page_url(url, page):
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}page={page}&page_size={PAGE_SIZE}"


def get_all(path, user_agent="akam-tracker/1.0"):
    """Fetch EVERY page of a paginated v4 list endpoint and return all items.

    `path` may be a /v4 path ("/regions/availability") or a full URL.
    """
    url = path if path.startswith("http") else f"{BASE}{path}"

    first = _request(_page_url(url, 1), user_agent)
    if not isinstance(first, dict) or "pages" not in first:
        # Non-paginated endpoint — return whatever it gave us.
        return first.get("data", first) if isinstance(first, dict) else first

    pages = min(int(first.get("pages", 1)), MAX_PAGES)
    results = int(first.get("results", 0))
    items = list(first.get("data", []))

    for page in range(2, pages + 1):
        chunk = _request(_page_url(url, page), user_agent)
        items.extend(chunk.get("data", []))

    # Loud sanity guard: a silent truncation must never pass again.
    if results and len(items) != results:
        print(f"WARN [linode_api] {url}: fetched {len(items)} rows but API "
              f"reported {results} across {pages} pages — possible truncation.",
              file=sys.stderr)
    return items


if __name__ == "__main__":
    # Quick self-test against the availability feed.
    t0 = time.monotonic()
    rows = get_all("/regions/availability", "akam-selftest/1.0")
    print(f"availability: {len(rows)} rows in {time.monotonic()-t0:.1f}s "
          f"(page_size={PAGE_SIZE}, min_interval={MIN_INTERVAL}s)")
