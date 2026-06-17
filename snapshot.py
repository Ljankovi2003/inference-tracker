#!/usr/bin/env python3
"""Cross-signal snapshot: reads all three DBs and prints a unified bear/bull scorecard.

Run after run_all.py to get the full picture.
  - A1: availability signal (sold-out at $2.50 = demand confirmed; opening up = soft)
  - A2: price signal (hold/raise = bullish; discount = demand at price is weak)
  - A3: realized CIS rev/MW trend (the truth; A1/A2 lead it)
"""

import sys
from pathlib import Path
from datetime import datetime, timezone, date

# Windows consoles default to cp1252, which can't encode the arrows/mid-dots
# this script prints. Force UTF-8 so output never raises UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

AKAM_DIR = Path(__file__).parent


def read_db(db_name):
    try:
        import duckdb
        return duckdb.connect(str(AKAM_DIR / db_name), read_only=True)
    except Exception as e:
        print(f"  Cannot open {db_name}: {e}")
        return None


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def run():
    now = datetime.now(timezone.utc)
    print(f"\nAKAM Signal Snapshot  {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    # ── A1: Availability ──────────────────────────────────────────
    section("A1 · GPU Availability (akam_demand.duckdb)")
    con = read_db("akam_demand.duckdb")
    if con:
        try:
            # Latest snapshot summary
            df = con.execute("""
                SELECT region, plan, available, region_name
                FROM snapshots
                WHERE ts = (SELECT MAX(ts) FROM snapshots)
                ORDER BY region, plan
            """).fetchdf()

            if df.empty:
                print("  No data yet — run demand.py first")
            else:
                total = len(df)
                avail = df["available"].sum()
                print(f"  Plans tracked: {total}  |  Available: {avail}  |  Sold-out: {total - avail}")
                print()
                for rname in df["region_name"].unique():
                    sub = df[df["region_name"] == rname]
                    a = sub["available"].sum()
                    plans = ", ".join(sub["plan"].tolist())
                    flag = "AVAIL" if a > 0 else "SOLD-OUT"
                    print(f"  [{flag:9s}] {rname:12s}  {plans}")

            # Transitions (last 30 days)
            transitions = con.execute("""
                SELECT ts, region_name, plan, from_avail, to_avail
                FROM transitions
                ORDER BY ts DESC
                LIMIT 20
            """).fetchdf()

            if not transitions.empty:
                print("\n  Recent transitions (sold-out ↔ available):")
                for _, row in transitions.iterrows():
                    direction = "CAPACITY ADDED" if row["to_avail"] else "SOLD-OUT"
                    print(f"    {row['ts'].strftime('%Y-%m-%d')}  {direction:14s}  {row['region_name']} / {row['plan']}")

            # Blackwell detection
            blackwell = con.execute("""
                SELECT DISTINCT plan_id, label, price_hourly
                FROM plan_catalog
                WHERE generation = 'blackwell'
                LIMIT 10
            """).fetchdf()
            if blackwell.empty:
                print("\n  Blackwell/RTX PRO 6000: NOT in public catalog")
            else:
                print("\n  *** BLACKWELL PLANS DETECTED ***")
                for _, row in blackwell.iterrows():
                    print(f"    {row['plan_id']}  ${row['price_hourly']:.2f}/hr")

        except Exception as e:
            print(f"  Query error: {e}")
        con.close()

    # ── A2: Prices ───────────────────────────────────────────────
    section("A2 · GPU Prices (gpu_rates.duckdb)")
    con = read_db("gpu_rates.duckdb")
    if con:
        try:
            df = con.execute("""
                SELECT plan_id, label, price_hourly, price_monthly
                FROM gpu_rates
                WHERE tier = 'list'
                  AND ts = (SELECT MAX(ts) FROM gpu_rates WHERE tier = 'list')
                ORDER BY price_hourly
            """).fetchdf()

            if df.empty:
                print("  No data yet — run rates.py first")
            else:
                print(f"  {'Plan':30s}  {'$/hr':7s}  {'$/mo':8s}  Label")
                for _, row in df.iterrows():
                    hr = f"${row['price_hourly']:.2f}" if row["price_hourly"] else "-"
                    mo = f"${row['price_monthly']:.0f}" if row["price_monthly"] else "-"
                    print(f"  {row['plan_id']:30s}  {hr:7s}  {mo:8s}  {row['label']}")

            # Price change history
            changes = con.execute("""
                SELECT ts, plan_id, old_price, new_price, pct_change, direction
                FROM price_changes
                ORDER BY ts DESC
                LIMIT 10
            """).fetchdf()

            if not changes.empty:
                print("\n  Price changes detected:")
                for _, row in changes.iterrows():
                    print(f"    {row['ts'].strftime('%Y-%m-%d')}  {row['direction']:4s}  {row['plan_id']}  ${row['old_price']:.2f} → ${row['new_price']:.2f}  ({row['pct_change']:+.1f}%)")
            else:
                print("\n  No price changes detected (stable)")

            hrs = df["price_hourly"].dropna()
            if not hrs.empty:
                print(f"\n  Floor: ${hrs.min():.2f}/hr  |  Ceiling: ${hrs.max():.2f}/hr")
                print(f"  Mgmt target ($2.50 Blackwell): NOT IN CATALOG")

        except Exception as e:
            print(f"  Query error: {e}")
        con.close()

    # ── A3: CIS Revenue/MW ───────────────────────────────────────
    section("A3 · Realized CIS Revenue/MW (sec.duckdb)")
    con = read_db("sec.duckdb")
    if con:
        try:
            df = con.execute("""
                SELECT period, end_date, metric_type, rev_k, total_rev_k, rev_share_pct,
                       capex_qtr_k, deployed_mw, rev_per_mw_ann_k, yoy_pct
                FROM cis_quarterly
                ORDER BY end_date
            """).fetchdf()

            if df.empty:
                print("  No data yet — run cis.py first")
            else:
                for mtype, label in [("cis", "CIS (new segment, comparable)"),
                                      ("cloud_computing", "Cloud Computing (old segment — not comparable to CIS)")]:
                    sub = df[df["metric_type"] == mtype]
                    if sub.empty:
                        continue
                    print(f"\n  {label}:")
                    print(f"  {'Period':10s}  {'Rev':9s}  {'Rev%':6s}  {'~MW':6s}  {'Rev/MW/yr':10s}  {'Capex':8s}  {'YoY':7s}")
                    print(f"  {'-'*72}")
                    for _, row in sub.iterrows():
                        rev = f"${row['rev_k']/1000:.0f}M" if row["rev_k"] else "?"
                        share = f"{row['rev_share_pct']:.1f}%" if row["rev_share_pct"] else "?"
                        mw = f"{row['deployed_mw']:.0f}" if row["deployed_mw"] else "?"
                        rpm = f"${row['rev_per_mw_ann_k']/1000:.2f}M" if row["rev_per_mw_ann_k"] else "?"
                        capex = f"${row['capex_qtr_k']/1000:.0f}M" if row["capex_qtr_k"] else "?"
                        yoy = row["yoy_pct"]
                        yoy_s = f"{yoy:+.1f}%" if yoy is not None and yoy == yoy else "-"
                        print(f"  {row['period']:10s}  {rev:9s}  {share:6s}  {mw:6s}  {rpm:10s}  {capex:8s}  {yoy_s:7s}")

                print()
                print("  Rev/MW trend = realized fill rate; rising = utilization improving")
                print("  MW proxy = Gross PP&E ÷ $15M/MW (total company, overstates denominator)")

        except Exception as e:
            print(f"  Query error: {e}")
        con.close()

    # ── Signal summary ───────────────────────────────────────────
    section("Signal Summary")
    print("""  A1 (Availability): all GPU regions sold-out → demand exceeds supply at current price
  A2 (Price):        $2.50 Blackwell not yet in public catalog; RTX 4000 Ada floor $0.52/hr
  A3 (CIS Rev/MW):   Q1-2026 $94.6M CIS rev (+40% YoY); Rev/MW trend = leading fill signal

  Bear case triggers to watch:
    [ ] A1: available=True appears in GPU regions (capacity glut)
    [ ] A2: price cut or promotional SKU below $2.50 (discount-to-fill)
    [ ] A3: Rev/MW declining despite MW addition (fill rate deteriorating)
    [ ] Blackwell launch delayed past H2-2026 (execution risk)
""")


if __name__ == "__main__":
    run()
