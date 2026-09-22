# Grafana Observability Libraries

Reusable Grafana dashboard/panel library for the LGTM observability stack — sessions, skill usage, cost and health visibility for a multi-agent Hermes deployment.

Status: **v0 — dashboard panels only**. The wiring (docker-compose, datasource provisioning, alert rules, Prometheus / Loki configs) and the exporters themselves are **not published yet**. This repo intentionally ships only the dashboard JSON and this reuse contract.

## Layout

| File | Dashboard |
|---|---|
| `dashboards/hermes-main.json` | **Hermes-Main** (uid `hermes-flow`, folder `Hermes`) — the single consolidated dashboard: sessions, tokens/spend, health, breakdown |

29 panels in 5 rows:

| Row | Panels |
|---|---|
| Overall | (reserved, currently empty) |
| Sessions | Sessions per profile · Sessions per profile / day · Sessions per model (all profiles) · Last 24h Sessions · Top Per-profile skill usage |
| Tokens | Spend per profile · All-time OpenRouter (USD) · Spent this month / today / this week (USD) · Cost per model / day · Cron job tokens — per job · Tokens by kind / day (incl. cache) |
| Health | Exporter health — poll age & errors · Cron runs vs failures (24h) · Gateway log lines mentioning Matrix rooms (15m) · Error lines by source (5m) · Log lines by source (5m) |
| Breakdown | Sessions · Tool calls · Tokens · Messages · Cost last 24h · Cost all-time · Sessions per model per profile · Avg cost per session by model · Skill use trends — top 8 / day · Skill usage — top 15 · Sessions by source (which gateway/channel) |

## Datasource contract

Dashboards reference datasources by **fixed UID** (no `__inputs` — Grafana will not prompt on import; the datasources must exist with these exact UIDs):

| UID | Type | Used by |
|---|---|---|
| `prometheusuid` | Prometheus | all metric panels |
| `lokiuid` | Loki | Health row — the three log panels (`job="hermes"` stream) |

Template variable: `$profile` (dropdown fed from Prometheus label values; all panels filter on it).

## Metric families and producers

| Family | Producer |
|---|---|
| `hermes_cost_daily_usd`, `hermes_cost_weekly_usd`, `hermes_cost_monthly_usd`, `hermes_cost_total_usd` | hermes-cost-exporter (`:9101`) |
| `hermes_cost_last_success_timestamp_seconds`, `hermes_cost_exporter_errors_total` | hermes-cost-exporter (`:9101`) — poll health |
| `hermes_sessions_total`, `hermes_sessions_by_model_total`, `hermes_sessions_by_source_total` | hermes-flow-exporter (`:9102`) |
| `hermes_session_cost_total`, `hermes_session_tokens_total`, `hermes_session_messages_total`, `hermes_session_tool_calls_total` | hermes-flow-exporter (`:9102`) |
| `hermes_skill_uses_total` | hermes-flow-exporter (`:9102`) |
| `hermes_cron_runs_total`, `hermes_cron_failures_total`, `hermes_cron_session_tokens_total` | hermes-flow-exporter (`:9102`) |
| `hermes_gateway_up` | hermes-flow-exporter (`:9102`) |
| `hermes_flow_last_success_timestamp_seconds`, `hermes_flow_exporter_errors_total` | hermes-flow-exporter (`:9102`) — poll health |
| Loki stream `{job="hermes"}` (gateway/agent logs; the Health row log panels) | stack log pipeline (shipped into Loki; wiring not yet published) |

Notes:
- The session-level counters (cost/tokens/messages/tool_calls) come from **hermes-flow-exporter**, not the cost exporter. The cost exporter produces only the `hermes_cost_*_usd` spend gauges plus its health metrics.
- Prometheus scrapes exporters on `:9101` / `:9102` (scrape config not published).

## Import instructions

### Grafana UI
`Dashboards -> Import -> Upload dashboard JSON file` (or paste JSON), then Import. Datasources are matched by UID — create `prometheusuid` (Prometheus) and `lokiuid` (Loki) first, or panels will show "datasource not found".

### Provisioning
1. Copy the JSON into your dashboard provider folder, e.g. `/var/lib/grafana/dashboards/`.
2. Add/adjust a provisioning provider, e.g. `/etc/grafana/provisioning/dashboards/hermes.yml`:

```yaml
apiVersion: 1
providers:
  - name: hermes
    folder: Hermes
    type: file
    options:
      path: /var/lib/grafana/dashboards
```

3. Datasource provisioning (not shipped in this repo) must register Prometheus with `uid: prometheusuid` and Loki with `uid: lokiuid`.

## Reuse contract / status

- **v0**: wiring, compose, exporters, alerts, and datasource configs are NOT published. Only dashboard layout + queries.
- **Self-contained**: the source dashboard uses Grafana *library panels*; for this export all 19 library references were expanded into standalone panels, so the file imports anywhere without needing the original library elements.
- Dashboards contain queries/layout only — no credentials, no real cost numbers, no data.
- Export freshness: exported from the live Grafana instance on 2026-09-23.
- No LICENSE file (decision pending — ask before reusing).
