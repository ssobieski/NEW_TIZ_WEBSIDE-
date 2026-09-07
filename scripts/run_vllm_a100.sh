#!/usr/bin/env bash
# Start vLLM na Dellu z 4x NVIDIA A100 — OpenAI-compatible API dla agentów.
# Wymaga: CUDA, vllm, model na dysku/HF cache.
set -euo pipefail

MODEL="${MODEL:-Qwen/Qwen2.5-72B-Instruct-AWQ}"
PORT="${PORT:-8000}"
TP="${TP:-4}"                 # 4x A100
GPU_UTIL="${GPU_UTIL:-0.90}"
MAX_LEN="${MAX_LEN:-32768}"
HOST="${HOST:-0.0.0.0}"

echo "==> vLLM model=$MODEL tp=$TP port=$PORT max_len=$MAX_LEN"

exec python -m vllm.entrypoints.openai.api_server \
  --model "$MODEL" \
  --host "$HOST" \
  --port "$PORT" \
  --tensor-parallel-size "$TP" \
  --gpu-memory-utilization "$GPU_UTIL" \
  --max-model-len "$MAX_LEN" \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  --trust-remote-code
