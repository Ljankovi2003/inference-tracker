# Inference Tracker

A signal terminal for Akamai's (AKAM) Cloud Infrastructure / GPU-inference business,
built entirely on **public, unauthenticated data** — no account or API token required.

It tracks three signals:

| Signal | Source | Measures |
|--------|--------|----------|
| **A1 — Availability** (`demand.py`) | Linode `/v4/regions/availability` | GPU plan in-stock vs sold-out, per region, over time |
| **A2 — Price** (`rates.py`) | Linode `/v4/linode/types` | GPU list price ($/hr) and changes over time |
| **A3 — Realized rev/MW** (`cis.py`) | local SEC 10-K/10-Q text | CIS revenue ÷ deployed-MW proxy *(optional; off unless filings are present)* |

All fetching goes through `linode_api.py`, which paginates fully, throttles, and
retries with backoff so a snapshot is never silently truncated.

## Run locally

```bash
pip install -r requirements.txt
python dashboard.py            # → http://localhost:8800
```

- The dashboard reads the DuckDB files and serves a live, charted UI.
- The **↻ Poll live APIs** button re-polls A1/A2 and writes a fresh snapshot.
- To refresh data from the command line: `python run_all.py` (or `run.bat` on Windows).

Tunables (env vars, optional): `AKAM_MIN_INTERVAL` (throttle seconds, default 0.4),
`AKAM_MAX_RETRIES` (default 5), `AKAM_PAGE_SIZE` (default 500).

## How publishing works (GitHub Pages)

This repo publishes as a **static site** — no always-on server needed:

1. `export_static.py` writes `site/index.html` + `site/data.json` from the databases.
2. `dashboard.html` loads `/api/data` when served live, and falls back to `./data.json`
   when served static (and hides the server-only refresh button).
3. `.github/workflows/deploy.yml` runs daily: it polls the public APIs, rebuilds the
   site, commits the refreshed `*.duckdb` history back, and deploys to Pages.

**One-time setup:** push this repo to GitHub, then enable
**Settings → Pages → Source: GitHub Actions**. After that it updates itself.
