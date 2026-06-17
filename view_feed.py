#!/usr/bin/env python3
"""Ad-hoc viewer for the Linode/Akamai public availability feed.

No token, no account, no cost. Shows what the feed actually returns:
which regions appear, and which plan families are sold-out vs available.

Usage:
    python view_feed.py            # formatted breakdown
    python view_feed.py raw        # raw JSON (all entries)
    python view_feed.py gpu        # GPU-only rows
    python view_feed.py regions    # ALL regions + GPU capability flag
"""

import json
import sys
from collections import defaultdict

import linode_api

URL = "https://api.linode.com/v4/regions/availability"
UA = "akam-feed-viewer/1.0"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def fetch():
    """Fetch ALL pages — robust, throttled, paginated (see linode_api.py)."""
    return linode_api.get_all(URL, user_agent=UA)


def family(plan: str) -> str:
    p = plan.lower()
    if "gpu" in p:
        return "GPU"
    if "netint" in p or "accelerated" in p or "vpu" in p:
        return "Accelerated/VPU"
    if "premium" in p:
        return "Premium CPU"
    if "dedicated" in p:
        return "Dedicated CPU"
    if "highmem" in p:
        return "High Memory"
    return "Other/Standard"


def fetch_regions():
    req = urllib.request.Request(
        "https://api.linode.com/v4/regions",
        headers={"User-Agent": "akam-feed-viewer/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    return data.get("data", data) if isinstance(data, dict) else data


def show_regions():
    regs = fetch_regions()
    gpu = [r for r in regs if any("GPU" in c for c in r.get("capabilities", []))]
    print(f"TOTAL regions: {len(regs)}   |   GPU-capable: {len(gpu)}\n")
    print(f"  {'id':16s} {'label':26s} {'country':7s}  GPU?")
    print("  " + "-" * 58)
    for r in sorted(regs, key=lambda x: x["id"]):
        flag = "YES" if any("GPU" in c for c in r.get("capabilities", [])) else ""
        print(f"  {r['id']:16s} {r.get('label',''):26s} {r.get('country',''):7s}  {flag}")


def main():
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else ""

    if arg == "regions":
        show_regions()
        return

    items = fetch()

    if arg == "raw":
        print(json.dumps(items, indent=2))
        return

    if arg == "gpu":
        items = [x for x in items if "gpu" in x["plan"].lower()]

    print(f"Availability feed: {len(items)} entries")
    regions = sorted({x["region"] for x in items})
    print(f"Regions in feed: {len(regions)} -> {', '.join(regions)}\n")

    # region -> family -> [available, sold_out]
    table = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for x in items:
        table[x["region"]][family(x["plan"])][0 if x["available"] else 1] += 1

    print(f"  {'region':16s} {'family':18s} {'avail':>6s} {'sold':>6s}")
    print("  " + "-" * 50)
    for r in sorted(table):
        for f in sorted(table[r]):
            av, so = table[r][f]
            print(f"  {r:16s} {f:18s} {av:6d} {so:6d}")

    # Sold-out plan list
    sold = sorted({x["plan"] for x in items if not x["available"]})
    avail = sorted({x["plan"] for x in items if x["available"]})
    print(f"\n  SOLD OUT plans ({len(sold)}):")
    for p in sold:
        print(f"    {p}")
    print(f"\n  AVAILABLE plans ({len(avail)}):")
    for p in avail:
        print(f"    {p}")


if __name__ == "__main__":
    main()
