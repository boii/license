#!/usr/bin/env bash
# Smoke test the license server with curl. Useful for quick verification
# or server-to-server use. Requires: bash, curl, jq.
#
# Usage:
#   ./curl.sh activate KEY
#   ./curl.sh validate KEY
#   ./curl.sh deactivate KEY

set -euo pipefail

LICENSE_API="${LICENSE_API:-https://license.kin.my.id}"
PRODUCT="${PRODUCT:-myapp}"
MACHINE_ID="${MACHINE_ID:-$(cat ~/.config/$PRODUCT/machine.id 2>/dev/null || \
    (mkdir -p ~/.config/$PRODUCT && cat /proc/sys/kernel/random/uuid > ~/.config/$PRODUCT/machine.id && \
     cat ~/.config/$PRODUCT/machine.id))}"

cmd="${1:-}"
key="${2:-}"

if [[ -z "$cmd" || -z "$key" ]]; then
    echo "Usage: $0 {activate|validate|deactivate} <KEY>"
    exit 1
fi

curl -sS -X POST "$LICENSE_API/v1/$cmd" \
    -H "content-type: application/json" \
    -d "{\"key\":\"$key\",\"machine_id\":\"$MACHINE_ID\",\"product\":\"$PRODUCT\"}" \
    | jq .
