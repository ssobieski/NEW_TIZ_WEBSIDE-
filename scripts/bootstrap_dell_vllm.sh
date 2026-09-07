#!/usr/bin/env bash
# Bootstrap Della / agentic-vm-os (Cybertech VPN: 200.0.0.110)
# Uruchom NA Dellu po SSH:
#   curl -fsSL ... | bash
# albo skopiuj i: bash scripts/bootstrap_dell_vllm.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/ssobieski/NEW_TIZ_WEBSIDE-.git}"
REPO_DIR="${REPO_DIR:-$HOME/NEW_TIZ_WEBSIDE-}"
BRANCH="${BRANCH:-main}"
MODEL="${MODEL:-Qwen/Qwen2.5-72B-Instruct-AWQ}"
PORT="${PORT:-8000}"
TP="${TP:-4}"

echo "==> Host: $(hostname)  User: $(whoami)"
echo "==> Checking GPU"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi -L || true
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true
else
  echo "WARN: nvidia-smi brak — vLLM na A100 wymaga CUDA/driverów"
fi

if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "==> Clone $REPO_URL → $REPO_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
else
  echo "==> Repo istnieje — git pull"
  git -C "$REPO_DIR" fetch origin "$BRANCH"
  git -C "$REPO_DIR" checkout "$BRANCH"
  git -C "$REPO_DIR" pull --ff-only origin "$BRANCH" || true
fi

cd "$REPO_DIR"

if [[ ! -d .venv ]]; then
  echo "==> python venv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -U pip wheel
pip install -r requirements.txt

if ! python -c "import vllm" 2>/dev/null; then
  echo "==> Instalacja vLLM (może potrwać)"
  pip install vllm
fi

mkdir -p data/fleet/outbox data/fleet/inbox reports
if [[ ! -f config/industry.yaml ]]; then
  cp config/dell_vpn.example.yaml config/industry.yaml
fi

cat > "$HOME/.tiz_dell_env" <<EOF
export VLLM_BASE_URL=http://127.0.0.1:${PORT}
export HOST=0.0.0.0
export PORT=${PORT}
export TP=${TP}
export MODEL=${MODEL}
export PATH="$REPO_DIR/.venv/bin:\$PATH"
cd "$REPO_DIR"
EOF

echo "==> Gotowe. Start vLLM:"
echo "    source $HOME/.tiz_dell_env"
echo "    bash scripts/run_vllm_a100.sh"
echo
echo "W drugim terminalu (na Dellu):"
echo "    curl -s http://127.0.0.1:${PORT}/v1/models"
echo "Z laptopa (VPN):"
echo "    curl -s http://200.0.0.110:${PORT}/v1/models"
