#!/usr/bin/env python3
"""A1 - Live GPU availability tracker (akam_demand.duckdb).

Polls Linode /v4/regions/availability daily, tracks GPU plan availability
transitions (sold-out vs available) as the leading signal for CIS demand.

Available=False can mean sold-out OR not-offered, so we track transitions,
not snapshots. A transition False→True = capacity added. True→False = sold out.
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

DB_PATH = Path(__file__).parent / "akam_demand.duckdb"
API_URL = "https://api.linode.com/v4/regions/availability"

# Regions where AKAM has committed GPU infrastructure
GPU_REGIONS = {
    "eu-central": "Frankfurt",
    "eu-west": "London",
    "ap-south": "Singapore",
    "us-west": "Fremont",
    "us-east": "Newark",
    "us-central": "Dallas",
    "us-southeast": "Atlanta",
}

# Known Blackwell SKU prefix — will appear when launched
BLACKWELL_PREFIX = "g3-gpu"
RTX_PRO_6000_PATTERN = "rtx6000pro"


UA = "akam-demand-tracker/1.0"


def fetch_availability():
    # Robust, throttled, fully-paginated fetch (see linode_api.py).
    return linode_api.get_all(API_URL, user_agent=UA)


def fetch_gpu_types():
    """Return current GPU type catalog from /v4/linode/types."""
    items = linode_api.get_all("https://api.linode.com/v4/linode/types", user_agent=UA)
    return [x for x in items if x.get("class") == "gpu" or "gpu" in x.get("id", "").lower()]


def is_gpu_plan(plan_id: str) -> bool:
    return "gpu" in plan_id.lower()


def init_db(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            ts          TIMESTAMPTZ NOT NULL,
            region      TEXT NOT NULL,
            plan        TEXT NOT NULL,
            available   BOOLEAN NOT NULL,
            region_name TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS transitions (
            ts          TIMESTAMPTZ NOT NULL,
            region      TEXT NOT NULL,
            plan        TEXT NOT NULL,
            from_avail  BOOLEAN NOT NULL,
            to_avail    BOOLEAN NOT NULL,
            region_name TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS plan_catalog (
            ts          TIMESTAMPTZ NOT NULL,
            plan_id     TEXT NOT NULL,
            label       TEXT,
            vcpus       INT,
            memory_gb   INT,
            price_hourly DOUBLE,
            gpus        INT,
            generation  TEXT
        )
    """)


def get_last_snapshot(con):
    """Return dict {(region, plan): available} from the most recent snapshot."""
    try:
        rows = con.execute("""
            SELECT region, plan, available
            FROM snapshots
            WHERE ts = (SELECT MAX(ts) FROM snapshots)
        """).fetchall()
        return {(r, p): a for r, p, a in rows}
    except Exception:
        return {}


def detect_generation(plan_id: str) -> str:
    if plan_id.startswith("g1-"):
        return "rtx6000-legacy"
    if plan_id.startswith("g2-"):
        return "rtx4000-ada"
    if plan_id.startswith("g3-") or "blackwell" in plan_id.lower() or "rtx6000pro" in plan_id.lower():
        return "blackwell"
    return "unknown"


def run(verbose=True):
    ts = datetime.now(timezone.utc)
    con = duckdb.connect(str(DB_PATH))
    init_db(con)

    # --- Availability snapshot ---
    try:
        items = fetch_availability()
    except Exception as e:
        print(f"ERROR fetching availability: {e}", file=sys.stderr)
        con.close()
        return

    gpu_items = [x for x in items if is_gpu_plan(x["plan"])]
    last = get_last_snapshot(con)

    new_snapshots = []
    new_transitions = []
    new_plans_seen = set()

    for item in gpu_items:
        region = item["region"]
        plan = item["plan"]
        avail = item["available"]
        rname = GPU_REGIONS.get(region, region)
        new_snapshots.append((ts, region, plan, avail, rname))

        key = (region, plan)
        if key in last and last[key] != avail:
            new_transitions.append((ts, region, plan, last[key], avail, rname))

        if detect_generation(plan) == "blackwell":
            new_plans_seen.add(plan)

    con.executemany(
        "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)",
        new_snapshots,
    )

    if new_transitions:
        con.executemany(
            "INSERT INTO transitions VALUES (?, ?, ?, ?, ?, ?)",
            new_transitions,
        )

    # --- Type catalog (detect new SKUs like Blackwell) ---
    try:
        gpu_types = fetch_gpu_types()
        catalog_rows = []
        for t in gpu_types:
            plan_id = t["id"]
            catalog_rows.append((
                ts,
                plan_id,
                t.get("label", ""),
                t.get("vcpus"),
                (t.get("memory", 0) or 0) // 1024,
                (t.get("price", {}) or {}).get("hourly"),
                t.get("gpus", {}).get("count") if isinstance(t.get("gpus"), dict) else None,
                detect_generation(plan_id),
            ))
        con.executemany(
            "INSERT INTO plan_catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            catalog_rows,
        )
        blackwell = [r for r in catalog_rows if r[7] == "blackwell"]
    except Exception as e:
        print(f"WARN: could not fetch type catalog: {e}", file=sys.stderr)
        blackwell = []

    con.close()

    if verbose:
        _print_summary(ts, gpu_items, new_transitions, blackwell)


def _print_summary(ts, gpu_items, transitions, blackwell):
    print(f"\n=== A1 Availability Snapshot {ts.strftime('%Y-%m-%d %H:%M UTC')} ===")

    # Group by region
    from collections import defaultdict
    by_region = defaultdict(list)
    for item in gpu_items:
        by_region[item["region"]].append(item)

    total_plans = len(gpu_items)
    total_avail = sum(1 for x in gpu_items if x["available"])
    print(f"GPU plan×region entries: {total_plans}  |  Available: {total_avail}  |  Sold-out: {total_plans - total_avail}")
    print()

    for region in sorted(by_region):
        items = by_region[region]
        rname = GPU_REGIONS.get(region, region)
        avail = sum(1 for x in items if x["available"])
        plans = [x["plan"] for x in items]
        status = "AVAIL" if avail > 0 else "SOLD-OUT"
        print(f"  {rname:12s} ({region:14s})  {status:9s}  {avail}/{len(items)} plans")

    if transitions:
        print("\n*** TRANSITIONS DETECTED ***")
        for ts_, region, plan, from_a, to_a, rname in transitions:
            direction = "SOLD-OUT" if not to_a else "CAPACITY ADDED"
            print(f"  {direction:15s}  {rname} / {plan}  ({from_a} → {to_a})")

    if blackwell:
        print("\n*** BLACKWELL SKUs DETECTED ***")
        for row in blackwell:
            print(f"  {row[1]}  {row[2]}  ${row[5]}/hr")
    else:
        print("\n  Blackwell/RTX PRO 6000: NOT YET in public catalog")


def query(sql: str):
    """Run arbitrary SQL against the DB for ad-hoc analysis."""
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
