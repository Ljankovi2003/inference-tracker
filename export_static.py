#!/usr/bin/env python3
"""Build the static publishable site/ folder.

Reads the live signal databases via dashboard.build_data(), then writes:
    site/index.html   (a copy of dashboard.html)
    site/data.json    (the current snapshot the page loads when served static)

This is what GitHub Pages serves. Run it after polling (demand.py / rates.py)
so data.json reflects the latest snapshot.
"""

import json
import sys
from pathlib import Path

import dashboard  # reuses the exact same query layer as the live server

HERE = Path(__file__).parent
SITE = HERE / "site"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main():
    SITE.mkdir(exist_ok=True)
    (SITE / "index.html").write_text(
        (HERE / "dashboard.html").read_text(encoding="utf-8"), encoding="utf-8"
    )
    data = dashboard.build_data()
    (SITE / "data.json").write_text(json.dumps(data), encoding="utf-8")

    regs = data.get("availability", {}).get("regions", [])
    instock = sum(1 for r in regs if r.get("avail", 0) > 0)
    print(f"  built site/ -> index.html + data.json")
    print(f"  {len(regs)} regions, {instock} in stock, "
          f"as of {data.get('availability', {}).get('latest_ts')}")


if __name__ == "__main__":
    main()
