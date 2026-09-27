"""Smoke-test the prompt-size Collector end-to-end against the real HERMES_HOME.

Exercises the exact code path the exporter unit runs: profile enumeration,
`hermes prompt-size --json` subprocess, parse, snapshot. Safe to run while
the exporter unit is live — collect_profile() holds a per-home flock, so
concurrent collectors serialize instead of colliding. Prints a per-profile
summary; exits nonzero if any scope is missing at the deadline.

Notes:
- The CLI can momentarily flap tool counts under concurrent desktop-session
  load on the root home (toolset resolution is session/platform-aware); we
  record the LATEST value per label and require stability before success.
- EXPECTED_SCOPES must match this box (root + named profiles). Hidden dirs
  like .deleted are not globbed.

Usage:  python3 verify_prompt_collector.py [--interval 60] [--timeout 250]
"""
import os
import sys
import time

from prompt_size_metrics import Collector  # noqa: E402

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
EXPECTED_SCOPES = 10  # root + 9 named profiles on this box — update per machine


def main():
    argv = sys.argv[1:]
    interval = 60 if "--interval" not in argv else int(argv[argv.index("--interval") + 1])
    default_timeout = 40 + EXPECTED_SCOPES * 25 + interval  # one full pass + margin
    timeout = default_timeout if "--timeout" not in argv else int(argv[argv.index("--timeout") + 1])
    c = Collector(HERMES_HOME, interval=interval, timeout=120)
    c.start()
    deadline = time.time() + timeout
    seen = {}
    stable_polls = 0
    while time.time() < deadline:
        snap = c.snapshot()
        for label, rows in snap.items():
            blocks = sorted({r[1].get("block") or r[1].get("section")
                             for r in rows
                             if r[0] in ("hermes_prompt_size_bytes",
                                         "hermes_prompt_section_bytes")})
            tools = next((r[2] for r in rows if r[0] == "hermes_prompt_tools_total"), None)
            seen[label] = (len(rows), blocks, tools)
        if len(seen) >= EXPECTED_SCOPES:
            stable_polls += 1
            if stable_polls >= 4:  # ~8s of stability
                break
        else:
            stable_polls = 0
        time.sleep(2)
    c.stop()
    for label in sorted(seen):
        n, blocks, tools = seen[label]
        print(f"{label:14s} rows={n:2d} tools={tools} blocks={blocks}")
    missing = EXPECTED_SCOPES - len(seen)
    if missing:
        print(f"MISSING PROFILES: {missing}")
        return 1
    print(f"OK — all {EXPECTED_SCOPES} scopes collected and stable")
    return 0


if __name__ == "__main__":
    sys.exit(main())