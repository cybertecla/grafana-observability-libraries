#!/usr/bin/env python3
"""Hermes cost exporter — exposes OpenRouter usage as Prometheus metrics.

Polls GET https://openrouter.ai/api/v1/auth/key every POLL_INTERVAL_SECONDS
and serves Prometheus text format on EXPORTER_PORT (9101).

Gauges (cumulative, from the shared key):
  hermes_cost_usd{key="usage_total|usage_daily|usage_weekly|usage_monthly"}
  hermes_cost_free_tier
  hermes_cost_last_success_timestamp_seconds
  hermes_cost_exporter_errors_total   (counter)

Per-agent attribution needs per-profile API keys — v1 is shared-key only.
"""
import json
import os
import threading
import time
import urllib.request

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
ENDPOINT = "https://openrouter.ai/api/v1/auth/key"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
PORT = int(os.environ.get("EXPORTER_PORT", "9101"))

_lock = threading.Lock()
_metrics = {}  # name -> (gauge, dict-of-labels)
_errors = 0


def _fetch() -> dict:
    req = urllib.request.Request(ENDPOINT, headers={"Authorization": f"Bearer {API_KEY}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def poll_once() -> None:
    global _errors
    try:
        data = _fetch().get("data", {})
        with _lock:
            _metrics["hermes_cost_usd_total"] = float(data.get("usage") or 0)
            if "usage_daily" in data and data["usage_daily"] is not None:
                _metrics["hermes_cost_usd_daily"] = float(data["usage_daily"])
            if "usage_weekly" in data and data["usage_weekly"] is not None:
                _metrics["hermes_cost_usd_weekly"] = float(data["usage_weekly"])
            if "usage_monthly" in data and data["usage_monthly"] is not None:
                _metrics["hermes_cost_usd_monthly"] = float(data["usage_monthly"])
            _metrics["hermes_cost_free_tier"] = 1.0 if data.get("is_free_tier", False) else 0.0
            _metrics["hermes_cost_last_success_timestamp_seconds"] = time.time()
    except Exception as exc:  # noqa: BLE001 — exporter must never die
        _errors += 1
        print(f"[cost-exporter] poll failed: {exc!r}", flush=True)


def render() -> str:
    with _lock:
        lines = []
        for name, value in _metrics.items():
            lines.append(f"{name} {value}")
        lines.append(f"hermes_cost_exporter_errors_total {_errors}")
        return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/metrics":
            body = render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args) -> None:
        pass


def poll_loop() -> None:
    while True:
        poll_once()
        time.sleep(POLL_INTERVAL)


def main() -> None:
    if not API_KEY:
        raise SystemExit("OPENROUTER_API_KEY not set")
    threading.Thread(target=poll_loop, daemon=True).start()
    print(f"[cost-exporter] listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()