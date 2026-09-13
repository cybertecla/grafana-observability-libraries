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
CURSOR_FILE = os.path.join(HERMES_HOME, "state", "flow-exporter-cursors.json")

_lock = threading.Lock()
_metrics = {}  # full prometheus line (with labels) -> value
_error = 0
_totals = {}  # cumulative running totals keyed by (kind, profile, ...)
_seeded = set()  # profiles whose full history has been folded into _totals this process


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


def _collect_sessions():
    """Per-profile session counts from state.db — the 'is orchestration real' metric."""
    targets = [("default", os.path.join(HERMES_HOME, "state.db"))]
    for pdir in glob.glob(os.path.join(HERMES_HOME, "profiles", "*")):
        if os.path.isdir(pdir):
            targets.append((os.path.basename(pdir), os.path.join(pdir, "state.db")))
    for profile, db in targets:
        try:
            if not os.path.exists(db):
                continue
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            row = con.execute("SELECT COUNT(*) FROM sessions").fetchone()
            con.close()
            _emit("hermes_sessions_total", {"profile": profile}, row[0] if row else 0)
        except Exception as exc:
            print(f"[flow] sessions {profile}: {exc!r}", flush=True)


def _load_cursors():
    try:
        with open(CURSOR_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cursors(cursors):
    try:
        os.makedirs(os.path.dirname(CURSOR_FILE), exist_ok=True)
        tmp = CURSOR_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cursors, f)
        os.replace(tmp, CURSOR_FILE)
    except Exception as exc:
        print(f"[flow] cursor save error: {exc!r}", flush=True)


def _collect_session_usage():
    """Per-profile × model usage from state.db sessions (tokens, cost, tool calls)
    plus per-profile × source (which gateway/channel drives sessions).

    Cumulative gauges, seeded on first poll with a full aggregate pass, then
    incremental via a per-profile started_at cursor (idx_sessions_started keeps
    the delta queries cheap even on the 400MB+ root db).
    """
    targets = [("default", os.path.join(HERMES_HOME, "state.db"))]
    for pdir in glob.glob(os.path.join(HERMES_HOME, "profiles", "*")):
        if os.path.isdir(pdir):
            targets.append((os.path.basename(pdir), os.path.join(pdir, "state.db")))

    cursors = _load_cursors()
    for profile, db in targets:
        try:
            if not os.path.exists(db):
                continue
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            cur = con.cursor()
            if profile in _seeded:
                cursor_ts = cursors.get(profile, 0)
                where = "WHERE started_at > ?" if cursor_ts else ""
                params = (cursor_ts,) if cursor_ts else ()
            else:
                cursor_ts = 0
                where = ""
                params = ()
                _seeded.add(profile)
            row = cur.execute(
                "SELECT MAX(started_at) FROM sessions" + (" " + where if where else ""), params
            ).fetchone()
            max_ts = row[0] if row else None
            if max_ts is None:
                con.close()
                continue

            cost_sql = (
                "SELECT model, COUNT(*), "
                "COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                "COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_write_tokens),0), "
                "COALESCE(SUM(reasoning_tokens),0), COALESCE(SUM(estimated_cost_usd),0), "
                "COALESCE(SUM(tool_call_count),0), COALESCE(SUM(message_count),0) "
                "FROM sessions " + where + " GROUP BY model"
            )
            cur.execute(cost_sql, params)
            model_rows = cur.fetchall()
            cur.execute("SELECT source, COUNT(*) FROM sessions " + where + " GROUP BY source", params)
            src_rows = cur.fetchall()
            con.close()

            for model, n, itok, otok, crtok, cwtok, rtok, cost, tools, msgs in model_rows:
                m = model or "unknown"
                k = ("count", profile, m)
                _totals[k] = _totals.get(k, 0.0) + n
                _emit("hermes_sessions_by_model_total", {"profile": profile, "model": m}, _totals[k])
                for kind, val in (("input", itok), ("output", otok), ("cache_read", crtok),
                                  ("cache_write", cwtok), ("reasoning", rtok)):
                    kk = ("tokens", profile, m, kind)
                    _totals[kk] = _totals.get(kk, 0.0) + (val or 0)
                    _emit("hermes_session_tokens_total", {"profile": profile, "model": m, "kind": kind}, _totals[kk])
                kc = ("cost", profile, m)
                _totals[kc] = _totals.get(kc, 0.0) + (cost or 0)
                _emit("hermes_session_cost_total", {"profile": profile, "model": m}, _totals[kc])
                for extra_key, extra_name, extra_val in (
                    (("tools", profile), "hermes_session_tool_calls_total", tools),
                    (("msgs", profile), "hermes_session_messages_total", msgs),
                ):
                    _totals[extra_key] = _totals.get(extra_key, 0.0) + (extra_val or 0)
                    _emit(extra_name, {"profile": profile}, _totals[extra_key])

            for source, n in src_rows:
                sk = ("src", profile, source or "unknown")
                _totals[sk] = _totals.get(sk, 0.0) + n
                _emit("hermes_sessions_by_source_total",
                      {"profile": profile, "source": source or "unknown"}, _totals[sk])

            cursors[profile] = max_ts
        except Exception as exc:
            print(f"[flow] session usage {profile}: {exc!r}", flush=True)
    _save_cursors(cursors)


def _collect():
    global _error
    try:
        _collect_skills()
        _collect_cron()
        _collect_gateways()
        _collect_sessions()
        _collect_session_usage()
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