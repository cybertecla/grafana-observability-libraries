#!/usr/bin/env bash
# Push dashboard JSONs from the repo into Grafana (DB-managed, Hermes folder).
# Usage: scripts/grafana-import.sh [folderUid]   (default: Hermes folder)
set -euo pipefail
cd "$(dirname "$0")/.."
TOKEN=$(grep GRAFANA_API_TOKEN .env | cut -d= -f2)
[ -n "$TOKEN" ] || { echo "GRAFANA_API_TOKEN missing in .env"; exit 1; }
FOLDER="${1:-afxv3f33ivvnka}"
for f in grafana/provisioning/dashboards/hermes-overview.json \
         grafana/provisioning/dashboards/hermes-flow.json \
         grafana/provisioning/dashboards/hermes-profiles.json; do
  uid=$(jq -r .uid "$f")
  jq --arg folder "$FOLDER" --arg msg "import from repo" \
     '{dashboard: ., folderUid: $folder, overwrite: true, message: $msg}' "$f" \
    | curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
         http://localhost:3001/api/dashboards/db -d @- \
    | jq -r '"\(.status): \(.uid) \(.url // "")"'
done