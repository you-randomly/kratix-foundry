#!/bin/bash
set -euo pipefail

# check-players.sh
# Queries Foundry VTT API to check for connected players
# Called periodically by a controller to update instance status

HOSTNAME=$1
ADMIN_KEY=$2

if [[ -z "$HOSTNAME" || -z "$ADMIN_KEY" ]]; then
  echo "Usage: check-players.sh <hostname> <admin-key>"
  exit 1
fi

# Query Foundry API for active users
# Note: Foundry's API requires authentication
RESPONSE=$(curl -s -X GET "https://${HOSTNAME}/api/status" \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  --connect-timeout 5 \
  --max-time 10 || echo '{"error": "connection failed"}')

if echo "$RESPONSE" | jq -e '.error' > /dev/null 2>&1; then
  echo "ERROR: Failed to connect to Foundry at ${HOSTNAME}"
  echo '{"connectedPlayers": -1, "error": "connection failed"}'
  exit 0
fi

# Extract user count
ACTIVE_USERS=$(echo "$RESPONSE" | jq '.activeUsers // 0')
WORLD_ACTIVE=$(echo "$RESPONSE" | jq '.active // false')

cat <<EOF
{
  "connectedPlayers": ${ACTIVE_USERS},
  "worldActive": ${WORLD_ACTIVE},
  "checkedAt": "$(date -Iseconds)"
}
EOF
