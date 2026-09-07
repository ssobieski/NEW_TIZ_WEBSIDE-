#!/usr/bin/env bash
# Start vLLM na Dellu z 4x NVIDIA A100 — OpenAI-compatible API dla agentów.
# Wymaga: CUDA, vllm, model na dysku/HF cache.
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
TP="${TP:-4}"                 # 4x A100
GPU_UTIL="${GPU_UTIL:-0.90}"
MAX_LEN="${MAX_LEN:-32768}"
HOST="${HOST:-0.0.0.0}"

GPU_COUNT="$(nvidia-smi -L 2>/dev/null | wc -l | tr -d ' ')"
if [[ "${GPU_COUNT}" -lt 1 ]]; then
  echo "ERROR: 0 GPU widocznych dla nvidia-smi"
  exit 1
fi
if [[ "${GPU_COUNT}" -lt "$TP" ]]; then
  echo "WARN: GPU_COUNT=$GPU_COUNT < TP=$TP — obniżam TP do $GPU_COUNT"
  TP="$GPU_COUNT"
fi

echo "==> vLLM model=$MODEL tp=$TP port=$PORT max_len=$MAX_LEN python=$PYTHON gpus=$GPU_COUNT"

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
