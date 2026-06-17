#!/usr/bin/env python3
"""Inference Tracker — AKAM Cloud Infrastructure Signal Terminal.

A local, dependency-light dashboard (Python stdlib + duckdb only) that reads the
three signal databases and serves a themed, charted web UI.

Run:
    python dashboard.py            # serves http://localhost:8800
    python dashboard.py 9000       # custom port

Endpoints:
    GET  /              -> dashboard.html
    GET  /api/data      -> all signals as JSON
    POST /api/refresh   -> re-poll the live APIs (A1+A2), then return fresh JSON
"""

import json
import sys
import importlib.util
import webbrowser
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import duckdb

HERE = Path(__file__).parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8800

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Friendly names + a rough "second-generation data center" classification.
REGION_NAMES = {
    "eu-central": "Frankfurt", "de-fra-2": "Frankfurt 2", "eu-west": "London",
    "gb-lon": "London 2", "ap-south": "Singapore", "sg-sin-2": "Singapore 2",
    "us-west": "Fremont", "us-east": "Newark", "us-central": "Dallas",
    "us-southeast": "Atlanta", "fr-par": "Paris", "fr-par-2": "Paris 2",
    "jp-osa": "Osaka", "jp-tyo-3": "Tokyo 3", "ap-northeast": "Tokyo 2",
    "ap-southeast": "Sydney", "au-mel": "Melbourne", "ap-west": "Mumbai",
    "in-bom-2": "Mumbai 2", "in-maa": "Chennai", "id-cgk": "Jakarta",
    "nl-ams": "Amsterdam", "se-sto": "Stockholm", "es-mad": "Madrid",
    "it-mil": "Milan", "br-gru": "São Paulo", "ca-central": "Toronto",
    "us-iad": "Washington DC", "us-iad-2": "Washington 2", "us-lax": "Los Angeles",
    "us-mia": "Miami", "us-ord": "Chicago", "us-sea": "Seattle",
}
NEW_DC = {
    "de-fra-2", "fr-par", "fr-par-2", "sg-sin-2", "in-bom-2", "jp-osa", "jp-tyo-3",
    "us-iad-2", "us-ord", "us-sea", "us-lax", "us-mia", "au-mel", "id-cgk", "in-maa",
    "es-mad", "it-mil", "se-sto", "nl-ams", "ca-central", "ap-northeast",
    "ap-southeast", "br-gru",
}


def _db(name):
    return duckdb.connect(str(HERE / name), read_only=True)


def _availability():
    try:
        con = _db("akam_demand.duckdb")
    except Exception as e:
        return {"error": str(e), "regions": [], "series": [], "transitions": []}
    try:
        latest_ts = con.execute("SELECT MAX(ts) FROM snapshots").fetchone()[0]
        rows = con.execute("""
            SELECT region,
                   SUM(CASE WHEN available THEN 1 ELSE 0 END) AS avail,
                   COUNT(*) AS total
            FROM snapshots
            WHERE ts = (SELECT MAX(ts) FROM snapshots)
            GROUP BY region
            ORDER BY avail DESC, region
        """).fetchall()
        regions = [{
            "region": r[0],
            "name": REGION_NAMES.get(r[0], r[0]),
            "avail": int(r[1]),
            "total": int(r[2]),
            "isNew": r[0] in NEW_DC,
        } for r in rows]

        ts_rows = con.execute("""
            SELECT ts,
                   SUM(CASE WHEN available THEN 1 ELSE 0 END) AS avail,
                   COUNT(*) AS total
            FROM snapshots GROUP BY ts ORDER BY ts
        """).fetchall()
        series = [{"ts": str(r[0]), "avail": int(r[1]), "soldout": int(r[2]) - int(r[1]),
                   "total": int(r[2])} for r in ts_rows]

        tr = con.execute("""
            SELECT ts, region_name, plan, from_avail, to_avail
            FROM transitions ORDER BY ts DESC LIMIT 40
        """).fetchall()
        transitions = [{"ts": str(r[0]), "region": r[1], "plan": r[2],
                        "from": bool(r[3]), "to": bool(r[4])} for r in tr]

        bw = con.execute("""
            SELECT DISTINCT plan_id, label, price_hourly
            FROM plan_catalog WHERE generation = 'blackwell'
        """).fetchall()
        return {
            "latest_ts": str(latest_ts) if latest_ts else None,
            "regions": regions, "series": series, "transitions": transitions,
            "blackwell": [{"plan": b[0], "label": b[1], "hourly": b[2]} for b in bw],
        }
    finally:
        con.close()


def _rates():
    try:
        con = _db("gpu_rates.duckdb")
    except Exception as e:
        return {"error": str(e), "ladder": [], "series": [], "changes": []}
    try:
        ladder = con.execute("""
            SELECT plan_id, label, gpu_count, price_hourly, price_monthly
            FROM gpu_rates
            WHERE tier = 'list' AND ts = (SELECT MAX(ts) FROM gpu_rates WHERE tier='list')
            ORDER BY price_hourly
        """).fetchall()
        ladder = [{"plan": r[0], "label": r[1], "gpu": r[2],
                   "hourly": r[3], "monthly": r[4]} for r in ladder]

        sr = con.execute("""
            SELECT ts, MIN(price_hourly), MAX(price_hourly), MEDIAN(price_hourly)
            FROM gpu_rates WHERE tier='list' AND price_hourly IS NOT NULL
            GROUP BY ts ORDER BY ts
        """).fetchall()
        series = [{"ts": str(r[0]), "floor": r[1], "ceiling": r[2], "median": r[3]} for r in sr]

        ch = con.execute("""
            SELECT ts, plan_id, old_price, new_price, pct_change, direction
            FROM price_changes ORDER BY ts DESC LIMIT 30
        """).fetchall()
        changes = [{"ts": str(r[0]), "plan": r[1], "old": r[2], "new": r[3],
                    "pct": r[4], "dir": r[5]} for r in ch]
        return {"ladder": ladder, "series": series, "changes": changes}
    finally:
        con.close()


def _cis():
    try:
        con = _db("sec.duckdb")
    except Exception as e:
        return {"empty": True, "rows": [], "note": str(e)}
    try:
        rows = con.execute("""
            SELECT period, end_date, metric_type, rev_k, rev_share_pct,
                   deployed_mw, rev_per_mw_ann_k, yoy_pct
            FROM cis_quarterly ORDER BY end_date
        """).fetchall()
        data = [{"period": r[0], "end_date": str(r[1]), "metric": r[2], "rev_k": r[3],
                 "share": r[4], "mw": r[5], "rev_per_mw": r[6], "yoy": r[7]} for r in rows]
        return {"empty": len(data) == 0, "rows": data}
    except Exception as e:
        return {"empty": True, "rows": [], "note": str(e)}
    finally:
        con.close()


def build_data():
    return {"availability": _availability(), "rates": _rates(), "cis": _cis()}


def _run_module(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.run(verbose=False)


def refresh():
    errors = {}
    for mod in ("demand", "rates"):
        try:
            _run_module(mod)
        except Exception as e:
            errors[mod] = str(e)
    return errors


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            html = (HERE / "dashboard.html").read_text(encoding="utf-8")
            return self._send(200, html, "text/html; charset=utf-8")
        if self.path.startswith("/api/data"):
            return self._send(200, json.dumps(build_data()))
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path.startswith("/api/refresh"):
            errors = refresh()
            payload = build_data()
            payload["refresh_errors"] = errors
            return self._send(200, json.dumps(payload))
        self._send(404, json.dumps({"error": "not found"}))


def main():
    url = f"http://localhost:{PORT}"
    print(f"\n  Inference Tracker · AKAM Signal Terminal")
    print(f"  Serving at {url}   (Ctrl+C to stop)\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
