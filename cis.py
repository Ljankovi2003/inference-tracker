#!/usr/bin/env python3
"""A3 - Realized CIS revenue/MW (sec.duckdb).

Parses AKAM 10-K/10-Q filings from /mnt/c/WSL/Cyber/filings/AKAM/.

Segment note:
  - Pre-2026 filings use "cloud computing" (includes delivery apps + CIS)
  - Q1-2026+ filings use three segments: security / delivery & cloud apps / CIS
  - Q1-2026 10-Q provides recast Q1-2025 CIS comparison = $67.6M
  - DO NOT compare old "cloud computing" to new "cloud infrastructure services"

CIS as standalone metric only starts Q1-2026.
Old "cloud computing" stored separately for trend context only.

Deployed MW proxy = Gross PP&E ÷ $15M/MW (total company — overstates denominator).
Rev/MW annualized only for quarterly filings (×4); annual 10-K used as-is.
"""

import os
import re
import sys
from datetime import date
from pathlib import Path

import duckdb

# Windows consoles default to cp1252, which can't encode the symbols this
# script prints. Force UTF-8 so output never raises UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DB_PATH = Path(__file__).parent / "sec.duckdb"


def _resolve_filings_root() -> Path:
    """Locate the AKAM SEC filings directory across environments.

    Honors $AKAM_FILINGS_DIR first, then tries the original WSL path, its
    Windows equivalent, and a local ./filings/AKAM fallback. Returns the first
    that exists; otherwise the env/default so missing filings just get SKIPped.
    """
    candidates = []
    env = os.environ.get("AKAM_FILINGS_DIR")
    if env:
        candidates.append(Path(env))
    candidates += [
        Path("/mnt/c/WSL/Cyber/filings/AKAM"),   # original WSL path
        Path(r"C:/WSL/Cyber/filings/AKAM"),       # Windows equivalent
        Path(__file__).parent / "filings" / "AKAM",  # local fallback
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


FILINGS_ROOT = _resolve_filings_root()

MW_COST = 15_000  # $K per MW


def init_db(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS cis_quarterly (
            period          TEXT PRIMARY KEY,
            end_date        DATE,
            filing_type     TEXT,
            metric_type     TEXT,   -- 'cis' | 'cloud_computing' | 'cis_recast'
            rev_k           DOUBLE, -- Quarterly revenue ($K) — CIS or cloud_computing
            total_rev_k     DOUBLE,
            rev_share_pct   DOUBLE,
            capex_qtr_k     DOUBLE, -- Quarter PP&E purchases ($K)
            gross_ppe_k     DOUBLE, -- Gross PP&E at period end ($K)
            deployed_mw     DOUBLE, -- Gross PP&E / $15M/MW
            rev_per_mw_ann_k DOUBLE,-- Annualized quarterly rev / deployed MW
            yoy_pct         DOUBLE, -- YoY only when same metric_type
            filing_path     TEXT
        )
    """)


def parse_num(s: str) -> float:
    return float(s.replace(",", "").replace("(", "").replace(")", ""))


def extract_filing(path: Path, period: str, filing_type: str):
    """
    Returns dict with all extracted metrics from a single filing.
    For Q1-2026 also extracts the recast prior-year CIS comparison.
    """
    txt = path.read_text(errors="replace")
    # Flatten multi-line whitespace for easier matching
    flat = re.sub(r"\n\s*\n", "\n", txt)

    result = {
        "period": period,
        "filing_type": filing_type,
        "metric_type": None,
        "rev_k": None,
        "prior_yr_rev_k": None,   # prior year column from same table
        "total_rev_k": None,
        "capex_k": None,
        "gross_ppe_k": None,
        "recast_rows": [],         # list of (period_label, rev_k) for recast priors
    }

    # ── CIS revenue (2026+ three-segment reporting) ──────────────
    # Table format: "Cloud infrastructure services\n94,612 67,601"
    m = re.search(
        r"Cloud infrastructure services\s*\n?\s*\$?\s*([\d,]+)\s+\$?\s*([\d,]+)",
        flat, re.IGNORECASE,
    )
    if m:
        result["metric_type"] = "cis"
        result["rev_k"] = parse_num(m.group(1))
        result["prior_yr_rev_k"] = parse_num(m.group(2))  # recast prior year
    else:
        # 2025: renamed "cloud computing"; 2022-2024: "compute"
        m = re.search(
            r"Cloud computing\s+([\d,]+)\s+([\d,]+)",
            flat, re.IGNORECASE,
        )
        if m:
            result["metric_type"] = "cloud_computing"
            result["rev_k"] = parse_num(m.group(1))
            result["prior_yr_rev_k"] = parse_num(m.group(2))
        else:
            # 2022-2024: standalone "Compute" line (not "cloud computing")
            # Match: "Compute 630,376 504,219 ..." (ignore trailing %)
            m = re.search(
                r"^Compute\s+([\d,]+)\s+([\d,]+)",
                flat, re.IGNORECASE | re.MULTILINE,
            )
            if m:
                result["metric_type"] = "cloud_computing"  # store under same bucket
                result["rev_k"] = parse_num(m.group(1))
                result["prior_yr_rev_k"] = parse_num(m.group(2))

    # ── Total revenue ─────────────────────────────────────────────
    m = re.search(
        r"Total revenue\s*\$?\s*([\d,]+)\s+\$?\s*([\d,]+)",
        flat, re.IGNORECASE,
    )
    if m:
        result["total_rev_k"] = parse_num(m.group(1))

    # ── Capex: PP&E purchases from cash flow ──────────────────────
    # "Purchases of property and equipment ( 101,686 ) ( 117,776 )"
    m = re.search(
        r"Purchases of property and equipment\s*\(\s*([\d,]+)\s*\)\s*\(\s*([\d,]+)\s*\)",
        flat, re.IGNORECASE,
    )
    if m:
        result["capex_k"] = parse_num(m.group(1))

    # ── Gross PP&E ────────────────────────────────────────────────
    m = re.search(
        r"Property and equipment,?\s*(?:at cost|gross)\s+([\d,]+)\s+([\d,]+)",
        flat, re.IGNORECASE,
    )
    if m:
        result["gross_ppe_k"] = parse_num(m.group(1))
    else:
        # Fallback: use net PP&E (understates; noted in MW proxy caveat)
        m = re.search(
            r"Property and equipment,?\s*net\s+([\d,]+)\s+([\d,]+)",
            flat, re.IGNORECASE,
        )
        if m:
            result["gross_ppe_k"] = parse_num(m.group(1))

    return result


FILING_REGISTRY = [
    # (period_label, end_date, filing_type, relative_path, quarterly)
    ("Q1-2026", date(2026, 3, 31), "10-Q", "10-Q/2026-05-08_0001086222-26-000058/akam-20260331.txt", True),
    ("FY2025",  date(2025,12, 31), "10-K", "10-K/2026-02-20_0001086222-26-000022/akam-20251231.txt", False),
    ("Q3-2025", date(2025, 9, 30), "10-Q", "10-Q/2025-11-07_0001086222-25-000256/akam-20250930.txt", True),
    ("Q2-2025", date(2025, 6, 30), "10-Q", "10-Q/2025-08-08_0001086222-25-000218/akam-20250630.txt", True),
    ("Q1-2025", date(2025, 3, 31), "10-Q", "10-Q/2025-05-09_0001086222-25-000149/akam-20250331.txt", True),
    ("FY2024",  date(2024,12, 31), "10-K", "10-K/2025-02-24_0001086222-25-000028/akam-20241231.txt", False),
    ("Q3-2024", date(2024, 9, 30), "10-Q", "10-Q/2024-11-08_0001086222-24-000216/akam-20240930.txt", True),
    ("Q2-2024", date(2024, 6, 30), "10-Q", "10-Q/2024-08-08_0001086222-24-000199/akam-20240630.txt", True),
]


def run(verbose=True):
    con = duckdb.connect(str(DB_PATH))
    init_db(con)

    parsed = []

    for period, end_date, ftype, rel_path, is_quarterly in FILING_REGISTRY:
        fpath = FILINGS_ROOT / rel_path
        if not fpath.exists():
            if verbose:
                print(f"  SKIP {period}: not found")
            continue

        r = extract_filing(fpath, period, ftype)
        rev_k = r["rev_k"]
        gross_ppe = r["gross_ppe_k"]
        deployed_mw = gross_ppe / MW_COST if gross_ppe else None
        share = (rev_k / r["total_rev_k"] * 100) if (rev_k and r["total_rev_k"]) else None
        # Annualize only quarterly filings
        rev_per_mw = (rev_k * 4 / deployed_mw) if (rev_k and deployed_mw and is_quarterly) else \
                     (rev_k / deployed_mw) if (rev_k and deployed_mw) else None

        row = {
            "period": period,
            "end_date": end_date,
            "filing_type": ftype,
            "metric_type": r["metric_type"],
            "rev_k": rev_k,
            "total_rev_k": r["total_rev_k"],
            "rev_share_pct": share,
            "capex_k": r["capex_k"],
            "gross_ppe_k": gross_ppe,
            "deployed_mw": deployed_mw,
            "rev_per_mw_ann_k": rev_per_mw,
            "yoy_pct": None,
            "filing_path": str(fpath),
        }
        parsed.append(row)

        # Q1-2026 also gives recast Q1-2025 CIS for apples-to-apples YoY
        if r["prior_yr_rev_k"] and r["metric_type"] == "cis":
            # Store the recast prior-year CIS inline as annotation on current row
            # YoY = (current - recast_prior) / recast_prior
            row["yoy_pct"] = (rev_k - r["prior_yr_rev_k"]) / r["prior_yr_rev_k"] * 100 if rev_k else None

        # For cloud_computing, compute YoY from paired rows (both cloud_computing)
        # (handled after all rows are collected)

    # YoY for cloud_computing periods (within-metric only)
    by_period = {r["period"]: r for r in parsed}
    cc_yoy = {
        "Q3-2025": "Q3-2024",
        "Q2-2025": "Q2-2024",
        "Q1-2025": None,  # no Q1-2024 filing downloaded
        "FY2025": "FY2024",
        "Q3-2024": None,
        "Q2-2024": None,
    }
    for curr, prior_label in cc_yoy.items():
        if prior_label and curr in by_period and prior_label in by_period:
            c = by_period[curr]
            p = by_period[prior_label]
            if c["metric_type"] == p["metric_type"] == "cloud_computing" and c["rev_k"] and p["rev_k"]:
                c["yoy_pct"] = (c["rev_k"] - p["rev_k"]) / p["rev_k"] * 100

    # Upsert into DB
    for r in parsed:
        con.execute("""
            INSERT OR REPLACE INTO cis_quarterly VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            r["period"], r["end_date"], r["filing_type"], r["metric_type"],
            r["rev_k"], r["total_rev_k"], r["rev_share_pct"],
            r["capex_k"], r["gross_ppe_k"], r["deployed_mw"],
            r["rev_per_mw_ann_k"], r["yoy_pct"], r["filing_path"],
        ])

    con.close()

    if verbose:
        _print_summary(parsed)


def _print_summary(rows):
    print("\n=== A3 CIS / Cloud Revenue × MW (SEC filings) ===")
    print()
    print("  CIS (new segment, comparable):")
    print(f"  {'Period':10s}  {'Rev':9s}  {'Rev%':6s}  {'YoY':8s}  {'~MW':6s}  {'Rev/MW/yr':10s}  {'Capex':8s}")
    print(f"  {'-'*75}")

    for r in sorted(rows, key=lambda x: x["end_date"]):
        if r["metric_type"] != "cis":
            continue
        _print_row(r)

    print()
    print("  Cloud Computing (old segment — NOT comparable to CIS above):")
    print(f"  {'Period':10s}  {'Rev':9s}  {'Rev%':6s}  {'YoY':8s}  {'~MW':6s}  {'Rev/MW/yr':10s}  {'Capex':8s}")
    print(f"  {'-'*75}")

    for r in sorted(rows, key=lambda x: x["end_date"]):
        if r["metric_type"] != "cloud_computing":
            continue
        _print_row(r)

    print()
    print("  MW proxy = Gross PP&E ÷ $15M/MW  (total company capex; overstates denom)")
    print("  Rev/MW annualized (×4) for quarterly; annual 10-K used as-is")
    print("  CIS YoY uses Q1-2026 filing's recast prior-year comparison ($67.6M)")


def _print_row(r):
    rev = f"${r['rev_k']/1000:.0f}M" if r["rev_k"] else "?"
    share = f"{r['rev_share_pct']:.1f}%" if r["rev_share_pct"] else "?"
    yoy_val = r.get("yoy_pct")
    yoy = f"{yoy_val:+.1f}%" if yoy_val is not None and yoy_val == yoy_val else "-"
    mw = f"{r['deployed_mw']:.0f}" if r["deployed_mw"] else "?"
    rpm = f"${r['rev_per_mw_ann_k']/1000:.2f}M" if r["rev_per_mw_ann_k"] else "?"
    capex = f"${r['capex_k']/1000:.0f}M" if r["capex_k"] else "?"
    print(f"  {r['period']:10s}  {rev:9s}  {share:6s}  {yoy:8s}  {mw:6s}  {rpm:10s}  {capex:8s}")


def query(sql: str):
    con = duckdb.connect(str(DB_PATH), read_only=True)
    result = con.execute(sql).fetchdf()
    con.close()
    return result


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "query":
        sql = " ".join(sys.argv[2:])
        print(query(sql).to_string())
    else:
        run()
