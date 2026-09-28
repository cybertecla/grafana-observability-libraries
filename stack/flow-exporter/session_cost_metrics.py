#!/usr/bin/env python3
"""Bounded root-home main-task cumulative gauges; no prices, cursor or listener.

Uses the unchanged session_usage reader. Replaces this module's complete family
set in one exporter store under its render lock. One store represents ONE DB
scope; repeated calls refresh it, including removal on failure/eviction.
Billing modes sharing model/provider/base URL are summed; any missing timestamp
omits the whole grouped bucket. Source times are writer upsert times, not calls.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

import session_usage as _reader

_FAMILY_TOKENS = "hermes_workflow_tokens"
_FAMILY_CALLS = "hermes_workflow_api_calls"
_FAMILY_SEEN = "hermes_workflow_source_last_seen_seconds"
_FAMILY_OK = "hermes_workflow_collection_success"
_FAMILY_TS = "hermes_workflow_collection_timestamp_seconds"
_FAMILIES = (_FAMILY_TOKENS, _FAMILY_CALLS, _FAMILY_SEEN, _FAMILY_OK, _FAMILY_TS)


def _escape_label(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _line(name, labels):
    return name + "{" + ",".join(
        f'{k}="{_escape_label(v)}"' for k, v in sorted(labels.items())) + "}"


def collect_session_cost_metrics(metrics, lock, profile_label, db_path,
                                 reader=_reader, limit=20, now=time.time):
    """Fresh read of <=20 recent sessions; False indicates incomplete telemetry.

    No-usage and auxiliary-only sessions are absent, not synthetic zero usage.
    Unknown timestamps omit grouped buckets and mark the pass incomplete.
    Valid other sessions survive a per-session reader error. Unexpected adapter
    errors discard this entire pass rather than exposing a partial bucket.
    """
    fresh = {}
    ok = True
    try:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be 1..20")
        listing = reader.list_sessions(db_path, limit=limit)
        if listing["status"] != "ok":
            ok = False
        else:
            for sess in listing["sessions"]:
                snap = reader.read_session(db_path, sess["id"])
                if snap["status"] == "no_data":
                    continue
                if snap["status"] != "ok":
                    ok = False
                    continue
                groups = {}
                for row in snap["rows"]:
                    if row["task"] != "":
                        continue
                    key = (row["model"], row["billing_provider"], row["billing_base_url_id"])
                    groups.setdefault(key, []).append(row)
                for (model, provider, route), rows in groups.items():
                    if any(r["source_last_seen"] is None for r in rows):
                        ok = False
                        continue
                    labels = dict(profile=profile_label, session_id=sess["id"],
                                  model=model, provider=provider, route=route)
                    for kind, field in (("uncached", "M"), ("cached", "H"), ("output", "O")):
                        fresh[_line(_FAMILY_TOKENS, dict(labels, kind=kind))] = float(
                            sum(r[field] for r in rows))
                    fresh[_line(_FAMILY_CALLS, labels)] = float(sum(r["calls"] for r in rows))
                    fresh[_line(_FAMILY_SEEN, labels)] = float(max(
                        r["source_last_seen"] for r in rows))
    except Exception:
        fresh.clear()
        ok = False
    fresh[_line(_FAMILY_OK, {"profile": profile_label})] = float(ok)
    fresh[_line(_FAMILY_TS, {"profile": profile_label})] = float(now())
    with lock:
        for key in list(metrics):
            if any(key.startswith(name + "{") for name in _FAMILIES):
                metrics.pop(key)
        metrics.update(fresh)
        # _render formats every mapping entry as 'key value'; metadata uses
        # exactly that contract, without changing legacy _emit or _render.
        metrics.update({"# TYPE " + name: "gauge" for name in _FAMILIES})
    return ok


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, help="state.db opened read-only")
    p.add_argument("--profile", required=True, help="source profile label, not model identity")
    p.add_argument("--limit", type=int, choices=range(1, 21), default=20)
    args = p.parse_args(argv)
    metrics = {}
    ok = collect_session_cost_metrics(metrics, threading.Lock(), args.profile,
                                      args.db, limit=args.limit)
    print("\n".join(f"{key} {value}" for key, value in sorted(metrics.items())))
    if not ok:
        print("session_cost_metrics: incomplete collection; affected samples omitted", file=sys.stderr)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
