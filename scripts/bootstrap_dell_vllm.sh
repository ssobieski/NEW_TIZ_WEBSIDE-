#!/usr/bin/env bash
# Bootstrap Della / agentic-vm-os (Cybertech VPN: 200.0.0.110)
# Uruchom NA Dellu po SSH:
#   cd ~/NEW_TIZ_WEBSIDE- && bash scripts/bootstrap_dell_vllm.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/ssobieski/NEW_TIZ_WEBSIDE-.git}"
REPO_DIR="${REPO_DIR:-$HOME/NEW_TIZ_WEBSIDE-}"
BRANCH="${BRANCH:-main}"
MODEL="${MODEL:-Qwen/Qwen2.5-72B-Instruct-AWQ}"
PORT="${PORT:-8000}"
TP="${TP:-4}"
INSTALL_VLLM="${INSTALL_VLLM:-1}"

echo "==> Host: $(hostname)  User: $(whoami)"
echo "==> Checking GPU"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  nvidia-smi -L || true
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true
  GPU_OK=1
else
  echo "WARN: NVIDIA driver NIE działa (nvidia-smi)."
  echo "      vLLM nie wystartuje, dopóki GPU nie będzie widoczne w tej VM."
  echo "      Uruchom: bash scripts/dell_gpu_diagnose.sh"
  GPU_OK=0
fi

echo "==> Ensuring python3-venv"
if ! python3 -c "import ensurepip,venv" 2>/dev/null; then
  echo "    sudo apt install -y python3-venv python3-pip"
  sudo apt-get update -y
  sudo apt-get install -y python3-venv python3-pip python3-dev build-essential
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

# Remove broken half-created venv
if [[ -d .venv && ! -x .venv/bin/python3 ]]; then
  echo "==> Usuwam uszkodzony .venv"
  rm -rf .venv
fi

if [[ ! -d .venv ]]; then
  echo "==> python3 -m venv .venv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -U pip wheel
pip install -r requirements.txt

if [[ "$INSTALL_VLLM" == "1" ]]; then
  if [[ "$GPU_OK" != "1" ]]; then
    echo "==> POMIJAM instalację vLLM (brak GPU). Po naprawie drivera:"
    echo "    source .venv/bin/activate && pip install vllm"
  elif ! python -c "import vllm" 2>/dev/null; then
    echo "==> Instalacja vLLM (może potrwać)"
    pip install vllm
  else
    echo "==> vLLM już zainstalowany"
  fi
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

echo
if [[ "$GPU_OK" == "1" ]]; then
  echo "==> Gotowe. Start vLLM:"
  echo "    source $HOME/.tiz_dell_env"
  echo "    bash scripts/run_vllm_a100.sh"
else
  echo "==> Bootstrap częściowy (bez GPU)."
  echo "    Najpierw napraw NVIDIA / passthrough, potem:"
  echo "    bash scripts/dell_gpu_diagnose.sh"
  echo "    source .venv/bin/activate && pip install vllm"
  echo "    bash scripts/run_vllm_a100.sh"
fi
echo
echo "Health (po starcie vLLM):"
echo "    curl -s http://127.0.0.1:${PORT}/v1/models"
echo "Z laptopa (VPN):"
echo "    curl -s http://200.0.0.110:${PORT}/v1/models"
