#!/bin/bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
TRIGGER=$REPO/data/.update-trigger
RESTART_TRIGGER=$REPO/data/.restart-trigger
STATUS=$REPO/data/.update-status
HEARTBEAT=$REPO/data/.update-heartbeat

write_status() { printf "%s" "$1" > "$STATUS"; chown 1000:1000 "$STATUS" 2>/dev/null || true; }
write_heartbeat() { date -u +%Y-%m-%dT%H:%M:%SZ > "$HEARTBEAT"; chown 1000:1000 "$HEARTBEAT" 2>/dev/null || true; }

do_git_update() {
  if [ "$CHANNEL" = "release" ]; then
    cd "$REPO" && git fetch --tags 2>&1
    if [ -n "${TARGET_VERSION:-}" ]; then
      TAG="v${TARGET_VERSION#v}"
      if ! git rev-parse "$TAG" >/dev/null 2>&1; then
        echo "FEHLER: Tag $TAG nicht gefunden (git fetch --tags ausgeführt)"
        return 1
      fi
      git reset --hard "$TAG" 2>&1
      echo "→ Release $TAG ausgecheckt (gezielt gewählt — ggf. Rollback)"
    else
      LATEST=$(git tag -l 'v*' | sort -V | tail -1)
      if [ -z "$LATEST" ]; then echo "FEHLER: Keine Release-Tags gefunden"; return 1; fi
      git reset --hard "$LATEST" 2>&1
      echo "→ Release $LATEST ausgecheckt"
    fi
  else
    cd "$REPO" && git fetch origin main 2>&1 && git reset --hard origin/main 2>&1
  fi
}

export HOME="${HOME:-/root}"
git config --global --add safe.directory "$REPO" 2>/dev/null || true

# Veraltetes "running" vom letzten Absturz wegräumen
if python3 -c "import json,sys; d=json.load(open('$STATUS')); sys.exit(0 if d.get('state')=='running' else 1)" 2>/dev/null; then
  rm -f "$STATUS"
fi

write_heartbeat  # sofort beim Start schreiben, nicht erst nach 60s

_hb_counter=0
while true; do
  _hb_counter=$(( _hb_counter + 1 ))
  if [ $(( _hb_counter % 12 )) -eq 0 ]; then write_heartbeat; fi

  if [ -f "$RESTART_TRIGGER" ]; then
    rm -f "$RESTART_TRIGGER"
    cd "$REPO" && docker compose restart 2>&1 || true
  fi

  if [ -f "$TRIGGER" ]; then
    VER_BEFORE=$(cat "$REPO/VERSION" 2>/dev/null | tr -d "[:space:]" || echo "?")
    CHANNEL=$(python3 -c "import json; print(json.load(open('$TRIGGER')).get('channel','main'))" 2>/dev/null || echo "main")
    TARGET_VERSION=$(python3 -c "import json; print(json.load(open('$TRIGGER')).get('target_version','') or '')" 2>/dev/null || echo "")
    rm -f "$TRIGGER"
    _log_target=""; [ -n "$TARGET_VERSION" ] && _log_target=" (Ziel: v${TARGET_VERSION})"
    write_status "{\"state\":\"running\",\"log\":\"Kanal ${CHANNEL}${_log_target}: wird aktualisiert...\",\"version_before\":\"$VER_BEFORE\"}"
    LOG=$(do_git_update 2>&1) && \
      LOG2=$(cd "$REPO" && docker compose up -d --build 2>&1) && \
      VER_AFTER=$(cat "$REPO/VERSION" 2>/dev/null | tr -d "[:space:]" || echo "?") && \
      write_status "{\"state\":\"success\",\"finished\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"version_before\":\"$VER_BEFORE\",\"version_after\":\"$VER_AFTER\",\"channel\":\"$CHANNEL\",\"log\":$(printf "%s\n%s" "$LOG" "$LOG2" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}" && \
      write_heartbeat && exec "$0" || \
      write_status "{\"state\":\"error\",\"finished\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"log\":$(printf "%s\n%s" "$LOG" "${LOG2:-}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}"
  fi

  sleep 5
done
