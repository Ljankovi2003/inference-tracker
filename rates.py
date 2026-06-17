#!/usr/bin/env python3
"""A2 - GPU price elasticity watch (gpu_rates.duckdb).

Polls Linode /v4/linode/types daily. Tracks GPU list price over time.
Signal: does AKAM hold/raise $2.50 (mgmt guidance) or discount-to-fill?
Discount-to-fill = demand-at-price is weak.

Three tiers tracked:
  - list:         public API hourly rate
  - enterprise:   $2.50+ committed SKU band (manual entry when available)
  - commodity:    RTX 4000 Ada floor (~$0.52/hr)
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

import linode_api

# Windows consoles default to cp1252, which can't encode the symbols this
# script prints. Force UTF-8 so output never raises UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DB_PATH = Path(__file__).parent / "gpu_rates.duckdb"
TYPES_URL = "https://api.linode.com/v4/linode/types"

# Manual price observations (promotions, enterprise quotes) can be inserted here
# Format: (date_str, plan_id, tier, price_hourly, source, notes)
MANUAL_ENTRIES = [
    # ("2026-06-16", "g3-gpu-rtx6000pro-1", "enterprise", 2.50, "mgmt-guidance", "Q1-2026 earnings call target"),
]


def fetch_gpu_types():
    # Robust, throttled, fully-paginated fetch (see linode_api.py).
    items = linode_api.get_all(TYPES_URL, user_agent="akam-rates-tracker/1.0")
    return [x for x in items if x.get("class") == "gpu" or "gpu" in x.get("id", "").lower()]


def init_db(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS gpu_rates (
            ts            TIMESTAMPTZ NOT NULL,
            plan_id       TEXT NOT NULL,
            label         TEXT,
            vcpus         INT,
            memory_gb     INT,
            gpu_count     INT,
            price_hourly  DOUBLE,
            price_monthly DOUBLE,
            tier          TEXT DEFAULT 'list',
            source        TEXT DEFAULT 'linode-api',
            notes         TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS price_changes (
            ts            TIMESTAMPTZ NOT NULL,
            plan_id       TEXT NOT NULL,
            old_price     DOUBLE,
            new_price     DOUBLE,
            pct_change    DOUBLE,
            direction     TEXT
        )
    """)


def get_last_prices(con):
    """Return {plan_id: price_hourly} from most recent list-tier snapshot."""
    try:
        rows = con.execute("""
            SELECT plan_id, price_hourly
            FROM gpu_rates
            WHERE tier = 'list'
              AND ts = (SELECT MAX(ts) FROM gpu_rates WHERE tier = 'list')
        """).fetchall()
        return {p: h for p, h in rows}
    except Exception:
        return {}


def run(verbose=True):
    ts = datetime.now(timezone.utc)
    con = duckdb.connect(str(DB_PATH))
    init_db(con)

    try:
        gpu_types = fetch_gpu_types()
    except Exception as e:
        print(f"ERROR fetching types: {e}", file=sys.stderr)
        con.close()
        return

    last_prices = get_last_prices(con)
    new_rates = []
    price_changes = []

    for t in gpu_types:
        plan_id = t["id"]
        label = t.get("label", "")
        vcpus = t.get("vcpus")
        memory_gb = (t.get("memory", 0) or 0) // 1024
        price = t.get("price", {}) or {}
        price_hourly = price.get("hourly")
        price_monthly = price.get("monthly")
        gpu_info = t.get("gpus", {})
        gpu_count = gpu_info.get("count") if isinstance(gpu_info, dict) else None

        new_rates.append((
            ts, plan_id, label, vcpus, memory_gb, gpu_count,
            price_hourly, price_monthly, "list", "linode-api", None,
        ))

        if plan_id in last_prices and price_hourly is not None:
            old = last_prices[plan_id]
            if old != price_hourly:
                pct = (price_hourly - old) / old * 100 if old else None
                direction = "UP" if price_hourly > old else "DOWN"
                price_changes.append((ts, plan_id, old, price_hourly, pct, direction))

    con.executemany(
        "INSERT INTO gpu_rates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        new_rates,
    )

    if price_changes:
        con.executemany(
            "INSERT INTO price_changes VALUES (?, ?, ?, ?, ?, ?)",
            price_changes,
        )

    # Insert any manual entries (enterprise/promo observations)
    for entry in MANUAL_ENTRIES:
        date_str, plan_id, tier, price_hourly, source, notes = entry
        entry_ts = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        # Only insert if not already present
        exists = con.execute(
            "SELECT COUNT(*) FROM gpu_rates WHERE ts=? AND plan_id=? AND tier=?",
            [entry_ts, plan_id, tier],
        ).fetchone()[0]
        if not exists:
            con.execute(
                "INSERT INTO gpu_rates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (entry_ts, plan_id, None, None, None, None, price_hourly, None, tier, source, notes),
            )

    con.close()

    if verbose:
        _print_summary(ts, new_rates, price_changes)


def _print_summary(ts, rates, changes):
    print(f"\n=== A2 GPU Rates Snapshot {ts.strftime('%Y-%m-%d %H:%M UTC')} ===")
    print(f"{'Plan':30s}  {'Label':35s}  {'GPU':4s}  {'$/hr':7s}  {'$/mo':8s}")
    print("-" * 95)
    for row in sorted(rates, key=lambda r: (r[6] or 0)):
        plan_id, label, vcpus, mem, gpus, hr, mo = row[1], row[2], row[3], row[4], row[5], row[6], row[7]
        print(f"  {plan_id:30s}  {label:35s}  {str(gpus):4s}  {f'${hr:.2f}':7s}  {f'${mo:.0f}' if mo else '-':8s}")

    print()
    # Pricing tiers summary
    hrs = [r[6] for r in rates if r[6] is not None]
    if hrs:
        print(f"  Floor (cheapest GPU):    ${min(hrs):.2f}/hr")
        print(f"  Ceiling (priciest GPU):  ${max(hrs):.2f}/hr")
        print(f"  Mgmt target (Blackwell): $2.50/hr  [NOT YET IN PUBLIC CATALOG]")

    if changes:
        print("\n*** PRICE CHANGES DETECTED ***")
        for ts_, plan_id, old, new, pct, direction in changes:
            print(f"  {direction:4s}  {plan_id}  ${old:.2f} → ${new:.2f}  ({pct:+.1f}%)")
    else:
        print("\n  No price changes since last run.")


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
