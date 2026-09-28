"""Prompt-size + skills-disabled metrics library for the Hermes flow exporter.

Two jobs, both derived from real on-disk Hermes state:

1. ``hermes_prompt_size_bytes{profile, block}`` — the fixed prompt budget of a
   fresh session per profile (system_prompt / skills_index / memory /
   user_profile / tool_schemas) plus ``hermes_prompt_section_bytes{profile,
   section}`` (stable/context/volatile) and ``hermes_prompt_tools_total``.
   Source: ``hermes prompt-size --json`` (offline, no API call) run on a SLOW
   cadence — the fixed budget changes rarely, so polling it every 30s next to
   the other collectors would burn CPU for nothing. The Collector thread keeps
   the last good snapshot; the exporter merges it on every poll for free.

2. ``read_disabled_skills(config_path)`` — minimal stdlib scan of a Hermes
   config.yaml ``skills.disabled`` block. The exporter uses it to FILTER the
   skill-usage families: disabled skills must never emit, no matter what junk
   still sits in ``.usage.json`` (cloned profiles carry phantom entries).
   Parsing just this one block with a scanner keeps the unit stdlib-only —
   importing yaml would violate the stack's dependency rule.

Prometheus hygiene (house rules): metric names are dot-free (Prometheus drops
dotted names silently), values are emitted via ``str()`` floats so timestamps
never lose precision, and the module must stay importable by plain unittest.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import threading
import time
from ast import literal_eval

PROMPT_SIZE_POLL_SECONDS = int(os.environ.get("PROMPT_SIZE_POLL_SECONDS", "900"))

_BLOCKS = (
    ("system_prompt", "system_prompt", "bytes"),
    ("skills_index", "skills_index", "bytes"),
    ("memory", "memory", "bytes"),
    ("user_profile", "user_profile", "bytes"),
    ("tool_schemas", "tools", "json_bytes"),
)

_SECTION_SLUGS = (
    ("stable (identity/guidance/skills)", "stable"),
    ("context (AGENTS.md/cwd files)", "context"),
    ("volatile (memory/profile/timestamp)", "volatile"),
)


def read_disabled_skills(config_path):
    """Set of skill names under ``skills.disabled`` in a Hermes config.yaml.

    Standard library scan, tolerant of the shapes atomic_config_writer
    produces (plain YAML list, ``[]`` empty, key absent). Any unparseable
    state returns an empty set — the caller's job is availability, not
    fail-closed correctness.
    """
    disabled = set()
    try:
        with open(config_path) as f:
            lines = f.read().splitlines()
    except Exception:
        return disabled
    in_skills = False
    in_disabled = False
    for line in lines:
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if indent == 0 and stripped.endswith(":"):
            in_skills = stripped == "skills:"
            in_disabled = False
            continue
        if not in_skills:
            continue
        if indent == 2 and stripped == "disabled:":
            in_disabled = True
            continue
        if indent < 2 and stripped.endswith(":"):
            in_disabled = False
            continue
        # Inline form written by `hermes config set skills.disabled`:
        #   disabled: '["airtable", "codex"]'  (quoted JSON/Python literal)
        # The inline line IS the declaration — parse it without requiring the
        # block-mode state set by a preceding bare `disabled:` line.
        if indent == 2 and stripped.startswith("disabled:"):
            raw_value = stripped.split(":", 1)[1].strip()
            if raw_value:
                in_disabled = True
                for name in _parse_inline_list(raw_value):
                    if name:
                        disabled.add(name)
            continue
        if indent == 2 or (indent < 2 and stripped.endswith(":")):
            in_disabled = False
            continue
        if in_disabled and indent == 4 and stripped.startswith("- "):
            name = stripped[2:].strip().strip("'\"")
            if name and name != "[]":
                disabled.add(name)
    return disabled


def _parse_inline_list(raw_value):
    """Parse an inline list value: '["a","b"]', ['a','b'], or "a","b"."""
    raw_value = raw_value.strip().strip("'\"").strip()
    if not raw_value:
        return []
    if raw_value.startswith("["):
        try:
            parsed = literal_eval(raw_value)
            if isinstance(parsed, (list, tuple, set)):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except (ValueError, SyntaxError):
            pass
        try:
            parsed = json.loads(raw_value)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except (ValueError, TypeError):
            pass
        return []
    return [x.strip() for x in raw_value.split(",") if x.strip()]


def _profile_targets(hermes_home):
    """(label, dir, cli_args) for the root profile + every named profile dir."""
    root_label = "default"
    try:
        with open(os.path.join(hermes_home, "profile.yaml")) as f:
            for line in f:
                if line.startswith("display_name:"):
                    root_label = line.split(":", 1)[1].strip().strip("'\"") or "default"
                    break
    except Exception:
        pass
    yield root_label, hermes_home, []
    for pdir in sorted(glob.glob(os.path.join(hermes_home, "profiles", "*"))):
        if os.path.isdir(pdir):
            yield os.path.basename(pdir), pdir, ["-p", os.path.basename(pdir)]


def parse_prompt_size(raw):
    """Parsed ``hermes prompt-size --json`` -> list of (metric, labels, value).

    Pure function — unit-testable with a saved fixture, no subprocess.
    """
    out = []
    for label, key, value_key in _BLOCKS:
        val = (raw.get(key) or {}).get(value_key, 0)
        out.append(("hermes_prompt_size_bytes", {"block": label}, float(val)))
    tools = raw.get("tools") or {}
    out.append(("hermes_prompt_tools_total", {}, float(tools.get("count", 0))))
    for section_name, slug in _SECTION_SLUGS:
        val = 0.0
        for name, _chars, _bytes in raw.get("sections") or []:
            if name == section_name:
                val = float(_bytes)
        out.append(("hermes_prompt_section_bytes", {"section": slug}, val))
    return out


def collect_profile(hermes_home, profile_dir, cli_args, timeout=120):
    """Run ``hermes prompt-size --json`` for one profile; None on any failure.

    A per-home flock serializes concurrent collectors (the exporter unit and
    a manual verification run): parallel ``hermes`` CLI invocations fail
    nondeterministically (the trio of profiles that failed in the first
    verification run all passed standalone). Wait up to 15s for the lock,
    then give up — the next pass retries.
    """
    import fcntl
    lock_path = os.path.join(hermes_home, "state", "flow-prompt-size.lock")
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        lock_fh = open(lock_path, "a+")
        deadline = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print(f"[flow] prompt-size {cli_args or ['root']}: lock held, retrying next pass",
                          flush=True)
                    return None
                time.sleep(0.5)
    except Exception as exc:
        print(f"[flow] prompt-size lock: {exc!r}", flush=True)
        lock_fh = None
    try:
        r = subprocess.run(
            ["hermes"] + cli_args + ["prompt-size", "--json"],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "HERMES_HOME": hermes_home},
        )
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip().splitlines()
            print(f"[flow] prompt-size {cli_args or ['root']}: exit {r.returncode}: "
                  f"{detail[-1][:200] if detail else 'no output'}", flush=True)
            return None
        return json.loads(r.stdout)
    except Exception as exc:
        print(f"[flow] prompt-size {cli_args or ['root']}: {exc!r}", flush=True)
        return None
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
            except Exception:
                pass


class Collector:
    """Background thread keeping the last good prompt-size snapshot per profile.

    ``snapshot()`` returns {label: [(metric, labels, value), ...]} from the
    most recent successful pass; failures keep the previous snapshot (a
    transient CLI hiccup must never blank the series). Call ``start()`` once
    from the exporter main; the thread is daemon and self-staggering.
    """

    def __init__(self, hermes_home, interval=PROMPT_SIZE_POLL_SECONDS,
                 timeout=120):
        self._hermes_home = hermes_home
        self._interval = max(60, interval)
        self._timeout = timeout
        self._snapshot = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        targets = list(_profile_targets(self._hermes_home))
        if not targets:
            return
        # One full pass immediately, then one every interval. Passes are
        # SEQUENTIAL on purpose: the per-home flock serializes `hermes` CLI
        # spawns across processes too, so a threaded pass would just queue on
        # its own lock. One profile pass takes ~10-15s per scope; pick an
        # interval that outlasts it (default 900s >> 10 scopes × 15s).
        self._pass(targets)
        while not self._stop.wait(self._interval):
            self._pass(targets)

    def _pass(self, targets):
        for target in targets:
            self._pass_once(target)
            time.sleep(1)

    def _pass_once(self, target):
        label, _dir, cli = target
        raw = collect_profile(self._hermes_home, _dir, cli, timeout=self._timeout)
        if raw is None:
            print(f"[flow] prompt-size {label}: collect failed, keeping last snapshot", flush=True)
            return
        parsed = parse_prompt_size(raw)
        with self._lock:
            self._snapshot[label] = parsed

    def snapshot(self):
        """{profile label: [(metric, labels, value), ...]} — latest good pass."""
        with self._lock:
            return {k: list(v) for k, v in self._snapshot.items()}