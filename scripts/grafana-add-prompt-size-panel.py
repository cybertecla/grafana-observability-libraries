#!/usr/bin/env python3
"""Attach the 'System prompt budget' panel to the live Flow dashboard.

Reusable/testable Grafana wiring for the prompt-size metric family:
1. Creates (or detects) a LIBRARY panel element ``prompt-size-v1`` — the
   panel definition others can reference or bake (library elements are
   create-only with this token; a conflict means it already exists).
2. Appends a full panel to the LIVE flow dashboard (DB-managed: GET → patch →
   POST /api/dashboards/db → re-GET assert). The dashboard embeds the panel
   JSON; the library element stays the durable, versionable definition.

Depends on: prometheusuid datasource (provisioned name), $profile template
variable on the flow dashboard, hermes_prompt_size_bytes / _tools_total
families from hermes-flow-exporter :9102.

Usage: python3 scripts/grafana-add-prompt-size-panel.py
Reads GRAFANA_API_TOKEN + GRAFANA_URL from the repo .env — never prints it.
"""
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIBRARY_UID = "prompt-size-v1"
PANEL_TITLE = "System prompt budget per profile"


def _load_env():
    env = {}
    try:
        with open(os.path.join(HERE, ".env")) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as exc:
        sys.exit(f"cannot read repo .env: {exc!r}")
    token = env.get("GRAFANA_API_TOKEN")
    url = env.get("GRAFANA_URL", "http://localhost:3001").rstrip("/")
    if not token:
        sys.exit("GRAFANA_API_TOKEN missing from .env")
    return token, url


def _req(token, url, path, method="GET", body=None):
    req = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def _targets(block, legend):
    return {
        "expr": f"sum(hermes_prompt_size_bytes{{block=\"{block}\",profile=~\"$profile\"}}) by (profile)",
        "format": "table",
        "instant": True,
        "legendFormat": legend,
    }


def _panel_model(next_id):
    return {
        "datasource": {"type": "prometheus", "uid": "prometheusuid"},
        "fieldConfig": {"defaults": {"unit": "decbytes"}, "overrides": []},
        "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
        "id": next_id,
        "targets": [
            _targets("system_prompt", "{{profile}}"),
            _targets("skills_index", "{{profile}}"),
            _targets("memory", "{{profile}}"),
            _targets("user_profile", "{{profile}}"),
            _targets("tool_schemas", "{{profile}}"),
            {
                "expr": "sum(hermes_prompt_tools_total{profile=~\"$profile\"}) by (profile)",
                "format": "table",
                "instant": True,
                "legendFormat": "{{profile}}",
            },
        ],
        "title": PANEL_TITLE,
        "transformations": [
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time": True},
                    "renameByName": {
                        "Value #A": "System prompt",
                        "Value #B": "Skills index",
                        "Value #C": "Memory",
                        "Value #D": "User profile",
                        "Value #E": "Tool schemas",
                        "Value #F": "Tools",
                    },
                },
            },
            {"id": "sortBy", "options": {"fields": {}, "sort": [{"desc": True, "field": "System prompt"}]}},
        ],
        "type": "table",
    }


def main():
    token, url = _load_env()

    # 1. Library element: create or skip.
    status, _ = _req(token, url, f"/api/library-elements/{LIBRARY_UID}")
    if status == 200:
        print(f"library element {LIBRARY_UID}: already exists — skip")
    else:
        library = {
            "name": "Prompt size per profile",
            "model": _panel_model(1),
            "kind": 1,          # 1 = panel
            "uid": LIBRARY_UID,
            "folderUid": "",
        }
        status, resp = _req(token, url, "/api/library-elements", "POST", library)
        if status in (200, 201):
            print(f"library element {LIBRARY_UID} created")
        else:
            print(f"library element create failed: {status} {resp}", file=sys.stderr)
            return 2

    # 2. Find the live flow dashboard (title may not contain 'flow' — the
    # live one is titled 'Hermes-Main' with uid hermes-flow).
    status, dashboards = _req(token, url, "/api/search?type=dash-db")
    flow = next(
        (d for d in dashboards
         if "hermes-flow" in d.get("uid", "").lower()
         or "flow" in d.get("title", "").lower()
         or "main" in d.get("title", "").lower()),
        None,
    )
    if not flow:
        print("flow dashboard not found via /api/search", file=sys.stderr)
        return 2
    dash_uid = flow["uid"]
    status, db = _req(token, url, f"/api/dashboards/uid/{dash_uid}")
    if status != 200:
        print(f"flow dashboard GET failed: {status}", file=sys.stderr)
        return 2
    dashboard = db["dashboard"]
    if any(p.get("title") == PANEL_TITLE for p in dashboard.get("panels", [])):
        print(f"'{PANEL_TITLE}' already on dashboard {dash_uid} — nothing to do")
        return 0
    next_id = max((p.get("id", 0) for p in dashboard.get("panels", [])), default=0) + 1
    panel = _panel_model(next_id)
    panel["gridPos"]["y"] = 24  # append below existing panels (x=12 row)
    dashboard["panels"].append(panel)
    status, resp = _req(token, url, "/api/dashboards/db", "POST", {
        "dashboard": dashboard,
        "folderUid": db.get("meta", {}).get("folderUid", ""),
        "overwrite": True,
    })
    if status != 200:
        print(f"dashboard POST failed: {status} {resp}", file=sys.stderr)
        return 2

    # 3. Re-GET and assert the panel landed.
    status, db2 = _req(token, url, f"/api/dashboards/uid/{dash_uid}")
    found = [p for p in db2.get("dashboard", {}).get("panels", []) if p.get("title") == PANEL_TITLE]
    if not found:
        print("panel not found after POST — failed to land", file=sys.stderr)
        return 2
    targets = len(found[0].get("targets", []))
    print(f"OK — '{PANEL_TITLE}' on {flow['title']} ({dash_uid}), {targets} targets")
    return 0


if __name__ == "__main__":
    sys.exit(main())