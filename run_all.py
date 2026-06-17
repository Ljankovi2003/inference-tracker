#!/usr/bin/env python3
"""Daily runner: polls A1 + A2 live APIs, parses A3 from local filings, then prints snapshot."""

import sys
import traceback

def run_module(name, module_path):
    print(f"\n--- Running {name} ---")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, module_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.run(verbose=True)
        print(f"    {name} OK")
    except Exception as e:
        print(f"    {name} FAILED: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    from pathlib import Path
    here = Path(__file__).parent

    run_module("demand (A1)", here / "demand.py")
    run_module("rates  (A2)", here / "rates.py")
    run_module("cis    (A3)", here / "cis.py")

    print("\n" + "="*60)
    print("  Running snapshot...")
    print("="*60)

    import importlib.util
    spec = importlib.util.spec_from_file_location("snapshot", here / "snapshot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.run()
