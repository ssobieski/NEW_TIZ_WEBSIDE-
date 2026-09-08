#!/usr/bin/env bash
# Install Dell SSH key from Cursor Cloud / worker secrets (never commit the key).
#
# Secrets (environment dashboard):
#   DELL_SSH=ssobieski@200.0.0.110
#   DELL_SSH_PRIVATE_KEY=<PEM private key>
#   VLLM_BASE_URL=http://200.0.0.110:8000
#
# Optional:
#   DELL_SSH_PUBLIC_KEY=...   # written next to the private key for reference
set -euo pipefail

KEY_DIR="${DELL_SSH_KEY_DIR:-$HOME/.ssh}"
KEY_FILE="${DELL_SSH_KEY_FILE:-$KEY_DIR/dell_agent}"
mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"

if [[ -z "${DELL_SSH_PRIVATE_KEY:-}" ]]; then
  echo "WARN: DELL_SSH_PRIVATE_KEY unset — SSH BatchMode do Della nie zadziała."
  echo "      Dodaj sekret w Cursor Environment albo uruchom self-hosted worker na Macu z VPN."
  exit 0
fi

# Normalize escaped newlines from secret stores
printf '%s\n' "${DELL_SSH_PRIVATE_KEY}" | sed 's/\r$//' | sed 's/\\n/\n/g' >"$KEY_FILE"
chmod 600 "$KEY_FILE"

if [[ -n "${DELL_SSH_PUBLIC_KEY:-}" ]]; then
  printf '%s\n' "${DELL_SSH_PUBLIC_KEY}" >"${KEY_FILE}.pub"
  chmod 644 "${KEY_FILE}.pub"
fi

# ssh config snippet (idempotent)
CFG="$KEY_DIR/config"
touch "$CFG"
chmod 600 "$CFG"
HOST_ALIAS="${DELL_SSH_HOST_ALIAS:-dell}"
if ! grep -q "Host ${HOST_ALIAS}\$" "$CFG" 2>/dev/null; then
  {
    echo ""
    echo "Host ${HOST_ALIAS}"
    echo "  HostName ${DELL_SSH_HOST:-200.0.0.110}"
    echo "  User ${DELL_SSH_USER:-ssobieski}"
    echo "  IdentityFile ${KEY_FILE}"
    echo "  IdentitiesOnly yes"
    echo "  StrictHostKeyChecking accept-new"
  } >>"$CFG"
fi

echo "==> Dell SSH key ready: $KEY_FILE (alias: $HOST_ALIAS)"
if [[ -n "${DELL_SSH:-}" ]]; then
  echo "==> DELL_SSH=$DELL_SSH"
fi
