#!/usr/bin/env bash
# Run a command on Dell over SSH (Cybertech VPN required on this host).
#
# Env:
#   DELL_SSH=user@200.0.0.110          # required
#   DELL_SSH_PRIVATE_KEY=...           # optional; scripts/setup_dell_ssh_from_secrets.sh
#
# Usage:
#   bash scripts/dell_remote.sh status
#   bash scripts/dell_remote.sh 'nvidia-smi -L'
#   bash scripts/dell_remote.sh pull-repo
#   bash scripts/dell_remote.sh doctor
#   bash scripts/dell_remote.sh run-agentic
#   bash scripts/dell_remote.sh vllm-check
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

# Load key from secrets if present
if [[ -n "${DELL_SSH_PRIVATE_KEY:-}" && ! -f "${HOME}/.ssh/dell_agent" ]]; then
  bash "$ROOT/scripts/setup_dell_ssh_from_secrets.sh" || true
fi

DELL_SSH="${DELL_SSH:-}"
REPO_DIR="${DELL_REPO_DIR:-\$HOME/NEW_TIZ_WEBSIDE-}"
ACTION="${1:-status}"
shift || true

if [[ -z "$DELL_SSH" ]]; then
  echo "Ustaw DELL_SSH=user@<dell-vpn-ip> (sekret Cursor / .env)."
  exit 1
fi

SSH_OPTS=(-o ConnectTimeout=8 -o BatchMode=yes)
if [[ -f "${HOME}/.ssh/dell_agent" ]]; then
  SSH_OPTS+=(-i "${HOME}/.ssh/dell_agent" -o IdentitiesOnly=yes)
fi

remote() {
  # shellcheck disable=SC2029
  ssh "${SSH_OPTS[@]}" "$DELL_SSH" "$@"
}

# Expand repo dir on the remote shell (default ~/NEW_TIZ_WEBSIDE-)
remote_repo() {
  local inner="$1"
  remote "bash -lc 'cd ${REPO_DIR} && ${inner}'"
}

case "$ACTION" in
  status)
    echo "DELL_SSH=$DELL_SSH"
    echo "VLLM_BASE_URL=${VLLM_BASE_URL:-<unset>}"
    if remote "echo ok && hostname && nvidia-smi -L 2>/dev/null | head -5"; then
      echo "==> SSH OK"
    else
      echo "==> SSH FAIL — VPN? klucz w authorized_keys? self-hosted worker na Macu?"
      exit 1
    fi
    ;;
  pull-repo)
    remote_repo "git fetch --all --prune; git status -sb; git rev-parse --abbrev-ref HEAD"
    ;;
  checkout)
    BRANCH="${1:?branch name}"
    remote_repo "git fetch origin; git checkout '${BRANCH}'; git pull --ff-only origin '${BRANCH}' || git pull --ff-only"
    ;;
  doctor)
    remote_repo "source .venv/bin/activate; export VLLM_BASE_URL=\${VLLM_BASE_URL:-http://127.0.0.1:8000}; python -m market_agents doctor"
    ;;
  run-agentic)
    remote_repo "source .venv/bin/activate; export VLLM_BASE_URL=\${VLLM_BASE_URL:-http://127.0.0.1:8000}; python -m market_agents run --agentic"
    ;;
  vllm-check)
    remote "curl -fsS --max-time 10 http://127.0.0.1:8000/v1/models | head -c 400; echo"
    if [[ -n "${VLLM_BASE_URL:-}" ]]; then
      bash "$ROOT/scripts/check_dell_vllm.sh"
    fi
    ;;
  sh|shell|exec)
    CMD="${*:-}"
    if [[ -z "$CMD" ]]; then
      echo "Podaj komendę: bash scripts/dell_remote.sh exec 'uname -a'"
      exit 2
    fi
    remote "$CMD"
    ;;
  *)
    remote "$ACTION $*"
    ;;
esac
