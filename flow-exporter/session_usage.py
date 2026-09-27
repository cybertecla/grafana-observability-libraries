#!/usr/bin/env python3
"""Read a CURRENT cumulative Hermes session snapshot, not individual calls.

M=input+cache_write, H=cache_read, O=output, I=M+H.
User-supplied DeepSeek v1 scenario: marker=10*M+44*O; A wins iff H>marker.
Savings B-A=(.01*H-.10*M-.44*O)/1e6; equality is a tie.
A=(.15*M+.003*H+.60*O)/1e6; B=(.05*M+.013*H+.16*O)/1e6.
No price verification, inference calls, writes, or cursor across invocations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from contextlib import contextmanager
from fractions import Fraction as F
from pathlib import Path
import sqlite3
import sys

_LIMITATION = (
    "Current persisted cumulative snapshot, not individual calls or chart history. "
    "May lag queued writer updates; missing upstream telemetry cannot be recovered. "
    "Exact session ID only: no parent/child/compression-lineage traversal."
)
_TASKS = "all tasks included: empty task is main loop; nonempty task is auxiliary"
_SESSIONS_SQL = """SELECT id, model, started_at, ended_at, last_activity_at,
    api_call_count, input_tokens, output_tokens, cache_read_tokens
    FROM sessions WHERE id = ?"""
_USAGE_SQL = """SELECT session_id, model, billing_provider, billing_base_url,
    billing_mode, task, api_call_count, input_tokens, output_tokens,
    cache_read_tokens, cache_write_tokens, reasoning_tokens, first_seen, last_seen
    FROM session_model_usage WHERE session_id = ?"""
_COUNTERS = ("api_call_count", "input_tokens", "output_tokens", "cache_read_tokens",
             "cache_write_tokens", "reasoning_tokens")


@contextmanager
def _snapshot(db_path, timeout, trace):
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError("database not found")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                           timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        if trace is not None:
            conn.set_trace_callback(trace if callable(trace) else trace.append)
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    finally:
        conn.close()  # Rolls back an incomplete read transaction on error.


def _int_count(value, field):
    if type(value) is not int or value < 0:
        # Do not echo corrupt field contents, which could contain private text.
        raise ValueError(f"{field} must be a nonnegative integer; missing/invalid telemetry")
    return value


def _timestamp(value):
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
        raise ValueError("source timestamp must be finite numeric Unix seconds or null")
    return value


def _deepseek_derived(m, h, o):
    marker = 10*m + 44*o
    cost_a = (150*m + 3*h + 600*o) / F(10**9)
    cost_b = (50*m + 13*h + 160*o) / F(10**9)
    return {"marker": marker, "savings_usd": float(cost_b-cost_a),
            "verdict": "A" if h > marker else "B" if h < marker else "tie",
            "basis": "H > marker" if h > marker else "H < marker" if h < marker else "H == marker",
            "cost_A_usd": float(cost_a), "cost_B_usd": float(cost_b),
            "M": m, "H": h, "O": o, "I": m+h}


def _is_deepseek(model):
    return model.lower().startswith(("deepseek/", "deepseek-"))


def _bucket(r):
    for field in ("model", "billing_provider", "billing_base_url", "billing_mode", "task"):
        if not isinstance(r[field], str):
            raise ValueError(f"missing/invalid bucket dimension: {field}")
    counts = {k: _int_count(r[k], k) for k in _COUNTERS}
    m = counts["input_tokens"] + counts["cache_write_tokens"]
    h, o = counts["cache_read_tokens"], counts["output_tokens"]
    d = {"session_id": r["session_id"], "model": r["model"],
         "billing_provider": r["billing_provider"],
         # Keep route distinctions without printing URL credentials or private paths.
         "billing_base_url_id": hashlib.sha256(r["billing_base_url"].encode()).hexdigest(),
         "billing_mode": r["billing_mode"], "task": r["task"],
         "calls": counts["api_call_count"], "M": m, "H": h, "O": o, "I": m+h,
         "cache_write": counts["cache_write_tokens"], "reasoning": counts["reasoning_tokens"],
         "source_first_seen": _timestamp(r["first_seen"]),
         "source_last_seen": _timestamp(r["last_seen"])}
    if _is_deepseek(r["model"]):
        d["deepseek_v1"] = _deepseek_derived(m, h, o)
    return d


def read_session(db_path, session_id, model=None, timeout=5.0, trace=None):
    """Select one ID and optional exact model, in one read-only transaction.

    No sessions-counter fallback: those mix model routes and exclude auxiliaries.
    trace optionally accepts a list or SQLite trace callback for offline tests.
    """
    out = {"db": str(db_path), "selection": {"session": session_id, "model": model},
           "scope": "session+model" if model is not None else "session",
           "status": "error", "error": None, "session": None, "model": None,
           "rows": [], "aggregation": None, "zero_totals": None,
           "source_last_seen": None, "timestamp_unit": "Unix seconds (raw)",
           "timestamp_meaning": "writer wall clock at bucket upsert, not API-call time",
           "tasks": _TASKS, "deepseek_v1": None, "deepseek_v1_note": None,
           "limitation": _LIMITATION}
    try:
        if not session_id or model == "":
            raise ValueError("nonempty explicit session/model selection required")
        with _snapshot(db_path, timeout, trace) as conn:
            session = conn.execute(_SESSIONS_SQL, (session_id,)).fetchone()
            sql = _USAGE_SQL + (" AND model = ?" if model is not None else "")
            sql += " ORDER BY model, billing_provider, billing_base_url, billing_mode, task"
            raw = conn.execute(sql, (session_id, model) if model is not None else (session_id,)).fetchall()
        out["session"] = dict(session) if session is not None else None
        if not raw:
            out.update(status="no_data", error="no usage rows for explicit selection; telemetry unknown")
            return out
        if session is None:
            raise ValueError("usage rows lack sessions metadata; orphaned telemetry")
        rows = [_bucket(r) for r in raw]  # Validate every bucket before exposing any totals.
        totals = {k: sum(r[k] for r in rows)
                  for k in ("calls", "M", "H", "O", "I", "cache_write", "reasoning")}
        models = sorted({r["model"] for r in rows})
        out.update(status="ok", rows=rows, model=models[0] if len(models) == 1 else models,
                   zero_totals=all(v == 0 for v in totals.values()))
        times = [r["source_last_seen"] for r in rows]
        out["source_last_seen"] = max(times) if all(t is not None for t in times) else None
        out["aggregation"] = {"source": "session_model_usage", "scope": out["scope"],
                              "buckets": len(rows), "aggregated": len(rows) > 1,
                              "note": "sum across selected model/provider/base/mode/task buckets; " + _TASKS,
                              "totals": totals}
        if all(_is_deepseek(m) for m in models):
            out["deepseek_v1"] = _deepseek_derived(totals["M"], totals["H"], totals["O"])
            out["deepseek_v1_note"] = (
                "User-supplied equal-usage price scenario, not verified pricing or measured switching savings. "
                "A USD/M: miss .15, hit .003, output .60; B: miss .05, hit .013, output .16. "
                "Reasoning is reported separately, not added to output. Zero observed totals give a mathematical tie only."
            )
        else:
            out["deepseek_v1_note"] = "No aggregate DeepSeek scenario across non-DeepSeek models; see individual buckets."
    except (sqlite3.Error, ValueError, OSError) as exc:
        out["error"] = str(exc)
    return out


def list_sessions(db_path, limit=10, timeout=5.0, trace=None):
    """Bounded recent metadata ordered by activity (then start and ID)."""
    out = {"db": str(db_path), "status": "error", "error": None, "limit": limit,
           "sessions": [], "note": "metadata only; session model is not a full route history; " + _LIMITATION}
    try:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        with _snapshot(db_path, timeout, trace) as conn:
            rows = conn.execute("""SELECT s.id, s.model, s.started_at, s.ended_at,
                s.last_activity_at, s.api_call_count, s.input_tokens,
                s.output_tokens, s.cache_read_tokens,
                EXISTS(SELECT 1 FROM session_model_usage u WHERE u.session_id=s.id) AS has_usage_rows
                FROM sessions s ORDER BY COALESCE(s.last_activity_at,s.started_at) DESC,
                s.started_at DESC, s.id DESC LIMIT ?""", (limit,)).fetchall()
        out["sessions"] = [dict(r, has_usage_rows=bool(r["has_usage_rows"])) for r in rows]
        out["status"] = "ok"
    except (sqlite3.Error, ValueError, OSError) as exc:
        out["error"] = str(exc)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(prog="session_usage.py", description=__doc__)
    p.add_argument("--db", required=True, help="explicit path to state.db (mode=ro)")
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument("--session", help="exact session ID; no lineage traversal")
    selection.add_argument("--list", action="store_true", help="recent metadata only")
    p.add_argument("--model", help="optional exact model within --session")
    p.add_argument("--limit", type=int, default=10, help="--list rows, 1..100 (default 10)")
    args = p.parse_args(argv)
    if args.list and args.model is not None:
        p.error("--model requires --session")
    result = (list_sessions(args.db, args.limit) if args.list
              else read_session(args.db, args.session, args.model))
    return {"ok": 0, "no_data": 1, "error": 2}[result["status"]], json.dumps(result, indent=2, allow_nan=False)


if __name__ == "__main__":
    rc, output = main()
    print(output)
    sys.exit(rc)
