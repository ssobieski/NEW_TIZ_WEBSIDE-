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
# Loopback by default. For Cybertech VPN set HOST to the Dell VPN interface IP
# and restrict TCP $PORT in the firewall to the VPN network.
HOST="${HOST:-127.0.0.1}"
if [[ -z "${HOST//[[:space:]]/}" ]]; then
  HOST=127.0.0.1
fi

# vLLM rejects /v1/* requests without "Authorization: Bearer $VLLM_API_KEY".
# Clients use the same value via MARKET_AGENTS_LLM_API_KEY (or VLLM_API_KEY).
VLLM_API_KEY="${VLLM_API_KEY:-}"
_key_trimmed="${VLLM_API_KEY//[[:space:]]/}"
if [[ -z "$_key_trimmed" || "${_key_trimmed^^}" == "EMPTY" ]]; then
  echo "ERROR: VLLM_API_KEY nie jest ustawiony (lub to placeholder EMPTY)."
  echo "Ustaw losowy klucz przed startem, np.:"
  echo "  export VLLM_API_KEY=\"\$(openssl rand -hex 32)\""
  echo "Na kliencie ten sam klucz: MARKET_AGENTS_LLM_API_KEY / VLLM_API_KEY"
  exit 1
fi
unset _key_trimmed

# --trust-remote-code runs Python shipped with the model repo inside the server.
TRUST_ARGS=()
if [[ "${VLLM_TRUST_REMOTE_CODE:-0}" == "1" ]]; then
  echo "WARN: VLLM_TRUST_REMOTE_CODE=1 — vLLM wykona kod z repozytorium modelu $MODEL"
  TRUST_ARGS=(--trust-remote-code)
fi

GPU_COUNT="$(nvidia-smi -L 2>/dev/null | wc -l | tr -d ' ')"
if [[ "${GPU_COUNT}" -lt 1 ]]; then
  echo "ERROR: 0 GPU widocznych dla nvidia-smi"
  exit 1
fi
if [[ "${GPU_COUNT}" -lt "$TP" ]]; then
  echo "WARN: GPU_COUNT=$GPU_COUNT < TP=$TP — obniżam TP do $GPU_COUNT"
  TP="$GPU_COUNT"
fi

echo "==> vLLM model=$MODEL host=$HOST tp=$TP port=$PORT max_len=$MAX_LEN python=$PYTHON gpus=$GPU_COUNT"

exec "$PYTHON" -m vllm.entrypoints.openai.api_server \
  --model "$MODEL" \
  --host "$HOST" \
  --port "$PORT" \
  --api-key "$VLLM_API_KEY" \
  --tensor-parallel-size "$TP" \
  --gpu-memory-utilization "$GPU_UTIL" \
  --max-model-len "$MAX_LEN" \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  ${TRUST_ARGS[@]+"${TRUST_ARGS[@]}"}
