#!/usr/bin/env bash
set -euo pipefail

LABEL="com.elasticsearch.watchdog.weekly-monday"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
if [[ -f "${PLIST_DST}" ]]; then
  rm -f "${PLIST_DST}"
  echo "Removed ${PLIST_DST}"
else
  echo "No plist at ${PLIST_DST}"
fi
echo "Unloaded ${DOMAIN}/${LABEL}"
