#!/usr/bin/env bash
# Start vLLM on Dell A100s — OpenAI-compatible API for market agents.
# Requires: CUDA, vllm, model on disk / HF cache.
# Note: attention heads must be divisible by tensor parallel size
# (Qwen2.5-72B = 64 heads → TP ∈ {1,2,4,8,…}; with 3 GPUs use TP=2).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if command -v python >/dev/null 2>&1; then
    PYTHON=python
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
  else
    echo "ERROR: brak python/python3. Zainstaluj: sudo apt install -y python3 python3-venv"
    exit 1
  fi
fi

if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi >/dev/null 2>&1; then
  echo "ERROR: NVIDIA driver nie działa (nvidia-smi)."
  echo "Na VM (agentic-vm-os) zwykle trzeba:"
  echo "  1) GPU passthrough A100 z hosta Proxmox/ESXi do tej VM"
  echo "  2) sudo apt install -y nvidia-driver-550   # lub nowszy"
  echo "  3) reboot + nvidia-smi -L"
  echo "Diag: bash scripts/dell_gpu_diagnose.sh"
  exit 1
fi

MODEL="${MODEL:-Qwen/Qwen2.5-72B-Instruct-AWQ}"
PORT="${PORT:-8000}"
REQUESTED_TP="${TP:-4}"
# Qwen2.5-72B = 64 heads; override if you change MODEL to another arch.
ATTN_HEADS="${ATTN_HEADS:-64}"
GPU_UTIL="${GPU_UTIL:-0.90}"
MAX_LEN="${MAX_LEN:-32768}"
HOST="${HOST:-0.0.0.0}"

GPU_COUNT="$(nvidia-smi -L 2>/dev/null | wc -l | tr -d ' ')"
if [[ "${GPU_COUNT}" -lt 1 ]]; then
  echo "ERROR: 0 GPU widocznych dla nvidia-smi"
  exit 1
fi

# TP must satisfy ATTN_HEADS % TP == 0 and TP <= GPU_COUNT.
pick_tp() {
  local gpus="$1" heads="$2" want="$3" t
  if [[ "${want}" -le "${gpus}" && $((heads % want)) -eq 0 ]]; then
    echo "${want}"
    return
  fi
  for (( t=gpus; t>=1; t-- )); do
    if (( heads % t == 0 )); then
      echo "${t}"
      return
    fi
  done
  echo 1
}

TP="$(pick_tp "${GPU_COUNT}" "${ATTN_HEADS}" "${REQUESTED_TP}")"
if [[ "${REQUESTED_TP}" != "${TP}" ]]; then
  echo "WARN: TP=${REQUESTED_TP} niepasujący do GPU_COUNT=${GPU_COUNT} / heads=${ATTN_HEADS} — używam TP=${TP}"
fi

echo "==> vLLM model=$MODEL tp=$TP port=$PORT max_len=$MAX_LEN python=$PYTHON gpus=$GPU_COUNT heads=$ATTN_HEADS"

exec "$PYTHON" -m vllm.entrypoints.openai.api_server \
  --model "$MODEL" \
  --host "$HOST" \
  --port "$PORT" \
  --tensor-parallel-size "$TP" \
  --gpu-memory-utilization "$GPU_UTIL" \
  --max-model-len "$MAX_LEN" \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  --trust-remote-code
