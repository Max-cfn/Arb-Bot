#!/usr/bin/env bash
set -euo pipefail
STATE_PATH=${ROLLING_STATS_STATE:-data/rolling_stats.json}
mkdir -p "$(dirname "$STATE_PATH")"
if [ ! -f "$STATE_PATH" ]; then
  cat > "$STATE_PATH" <<'JSON'
{
  "last_24h_total": null,
  "last_24h_actionable": null,
  "updated_at": null
}
JSON
  echo "Created $STATE_PATH"
else
  echo "Exists $STATE_PATH"
fi
