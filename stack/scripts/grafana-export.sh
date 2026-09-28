#!/usr/bin/env bash
# Snapshot live (DB-managed) dashboards from Grafana into grafana/exported/.
# Run after UI tuning sessions; commit the exported JSONs to keep the repo in sync.
set -euo pipefail
cd "$(dirname "$0")/.."
TOKEN=$(grep GRAFANA_API_TOKEN .env | cut -d= -f2)
[ -n "$TOKEN" ] || { echo "GRAFANA_API_TOKEN missing in .env"; exit 1; }
mkdir -p grafana/exported
for uid in hermes-overview hermes-flow hermes-profiles; do
  curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:3001/api/dashboards/uid/$uid" \
    | jq '.dashboard' > "grafana/exported/$uid.json"
  echo "exported $uid ($(wc -c < "grafana/exported/$uid.json") bytes)"
done