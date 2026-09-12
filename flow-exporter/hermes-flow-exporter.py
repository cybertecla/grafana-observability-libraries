#!/usr/bin/env python3
"""Hermes Flow exporter — skills usage, cron health, gateway status → Prometheus.

Reads real Hermes state on this box:
  - ~/.hermes/skills/.usage.json            (root)   → skill use/view counts
  - ~/.hermes/profiles/<p>/skills/.usage.json         → per-profile counts
  - ~/.hermes/cron/executions.db (sqlite)   → runs / failures per job
  - ~/.hermes/cron/jobs.json                → enabled flags
  - systemctl --user is-active hermes-gateway*.service  → gateway up/down

Serves Prometheus text format on :9102. Metric names are dot-free (Prometheus
drops dotted names silently). Run as a HOST systemd unit — it reads host files
and DBs; no container networking involved.
"""
import glob
import json
import os
import sqlite3
import subprocess
import threading
import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
POLL_INTERVAL = int(os.environ.get("FLOW_POLL_SECONDS", "30"))
PORT = int(os.environ.get("FLOW_EXPORTER_PORT", "9102"))

_lock = threading.Lock()
_metrics = {}  # full prometheus line (with labels) -> value
_error = 0


def _emit(name, labels, value):
    if labels:
        lbl = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()) if v != "")
        _metrics[f"{name}{{{lbl}}}"] = float(value)
    else:
        _metrics[name] = float(value)


def _read_usage(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _collect_skills():
    # root scope
    usage = _read_usage(os.path.join(HERMES_HOME, "skills", ".usage.json"))
    for skill, meta in usage.items():
        _emit("hermes_skill_uses_total", {"skill": skill, "scope": "root"}, meta.get("use_count", 0))
        _emit("hermes_skill_views_total", {"skill": skill, "scope": "root"}, meta.get("view_count", 0))
        _emit("hermes_skill_active", {"skill": skill, "scope": "root"}, 1 if meta.get("state") == "active" else 0)
    # per-profile scopes
    profile_dirs = [
        d for d in glob.glob(os.path.join(HERMES_HOME, "profiles", "*"))
        if os.path.isdir(d)
    ]
    for pdir in profile_dirs:
        profile = os.path.basename(pdir)
        usage = _read_usage(os.path.join(pdir, "skills", ".usage.json"))
        for skill, meta in usage.items():
            _emit("hermes_skill_uses_total", {"skill": skill, "scope": profile}, meta.get("use_count", 0))
            _emit("hermes_skill_views_total", {"skill": skill, "scope": profile}, meta.get("view_count", 0))
            _emit("hermes_skill_active", {"skill": skill, "scope": profile}, 1 if meta.get("state") == "active" else 0)


def _collect_cron():
    db = os.path.join(HERMES_HOME, "cron", "executions.db")
    if not os.path.exists(db):
        return
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        cur = con.cursor()
        try:
            cur.execute(
                "SELECT job_id, status, COUNT(*) FROM executions "
                "GROUP BY job_id, status"
            )
            for job_id, status, n in cur.fetchall():
                key = "hermes_cron_failures_total" if status != "completed" else "hermes_cron_runs_total"
                _emit(key, {"job": job_id}, n)
            cur.execute(
                "SELECT job_id, COUNT(*) FROM executions WHERE status='completed' "
                "GROUP BY job_id"
            )
            con.close()
        except sqlite3.Error as exc:
            print(f"[flow] cron query error: {exc}", flush=True)
    except Exception as exc:
        print(f"[flow] cron open error: {exc!r}", flush=True)


def _collect_gateways():
    units = ["hermes-gateway.service", "hermes-gateway-guru.service", "hermes-gateway-sniper.service"]
    for unit in units:
        try:
            r = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                capture_output=True, text=True, timeout=10,
            )
            up = 1 if r.stdout.strip() == "active" else 0
        except Exception:
            up = 0
        _emit("hermes_gateway_up", {"unit": unit.split(".")[0]}, up)


def _collect():
    global _error
    try:
        _collect_skills()
        _collect_cron()
        _collect_gateways()
        _metrics["hermes_flow_last_success_timestamp_seconds"] = time.time()
    except Exception as exc:  # never die
        _error += 1
        print(f"[flow] collect failed: {exc!r}", flush=True)


def _loop():
    while True:
        _collect()
        time.sleep(POLL_INTERVAL)


def _render():
    with _lock:
        lines = [f"{k} {v}" for k, v in _metrics.items()]
        lines.append(f"hermes_flow_exporter_errors_total {_error}")
        return "\n".join(sorted(lines)) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = _render().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def main():
    threading.Thread(target=_loop, daemon=True).start()
    print(f"[flow] listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()