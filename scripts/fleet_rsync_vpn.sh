#!/usr/bin/env bash
# Sync data/fleet między VPS/klientem a Dellem przez Cybertech VPN (rsync/SSH).
#
# Wymaga w .env lub środowisku:
#   DELL_SSH=user@10.x.x.x          # host Dell w VPN
#   DELL_FLEET_PATH=/opt/tiz/data/fleet   # opcjonalnie
#   LOCAL_FLEET_PATH=data/fleet           # opcjonalnie
#
# Użycie:
#   bash scripts/fleet_rsync_vpn.sh pull    # pobierz outbox z Della (knowledge pack)
#   bash scripts/fleet_rsync_vpn.sh push    # wyślij lokalny inbox na Della
#   bash scripts/fleet_rsync_vpn.sh status  # pokaż ścieżki / dry connectivity
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Load .env if present (same keys as market_agents)
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

DELL_SSH="${DELL_SSH:-}"
DELL_FLEET_PATH="${DELL_FLEET_PATH:-/opt/tiz/data/fleet}"
LOCAL_FLEET_PATH="${LOCAL_FLEET_PATH:-data/fleet}"
ACTION="${1:-status}"

if [[ -z "$DELL_SSH" && "$ACTION" != "status" ]]; then
  echo "Ustaw DELL_SSH=user@<dell-vpn-ip> w .env (Cybertech VPN)."
  exit 1
fi

mkdir -p "$LOCAL_FLEET_PATH/outbox" "$LOCAL_FLEET_PATH/inbox"

case "$ACTION" in
  pull)
    echo "==> pull Dell outbox → $LOCAL_FLEET_PATH/outbox"
    rsync -az --delete \
      "${DELL_SSH}:${DELL_FLEET_PATH}/outbox/" \
      "${LOCAL_FLEET_PATH}/outbox/"
    echo "==> OK pull"
    ;;
  push)
    echo "==> push $LOCAL_FLEET_PATH/inbox → Dell inbox"
    rsync -az \
      "${LOCAL_FLEET_PATH}/inbox/" \
      "${DELL_SSH}:${DELL_FLEET_PATH}/inbox/"
    echo "==> OK push (na Dellu: python -m market_agents fleet-absorb --republish)"
    ;;
  status)
    echo "DELL_SSH=${DELL_SSH:-<unset>}"
    echo "DELL_FLEET_PATH=$DELL_FLEET_PATH"
    echo "LOCAL_FLEET_PATH=$LOCAL_FLEET_PATH"
    if [[ -n "$DELL_SSH" ]]; then
      if ssh -o ConnectTimeout=5 -o BatchMode=yes "$DELL_SSH" "test -d '$DELL_FLEET_PATH' && echo fleet_dir_ok" 2>/dev/null; then
        echo "==> SSH/fleet OK"
      else
        echo "==> SSH niedostępny (VPN? klucz? ścieżka?)"
        exit 1
      fi
    else
      echo "==> Brak DELL_SSH — tylko lokalny status"
      ls -la "$LOCAL_FLEET_PATH" 2>/dev/null || echo "(brak lokalnego fleet)"
    fi
    ;;
  *)
    echo "Użycie: $0 {pull|push|status}"
    exit 2
    ;;
esac
