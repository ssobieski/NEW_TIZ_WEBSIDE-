#!/usr/bin/env bash
# Uruchom na DELLU (już po SSH). pull → vLLM (auto-start) → ensure → doctor → agentic → raport.
set -euo pipefail
cd "${HOME}/NEW_TIZ_WEBSIDE-"
git fetch origin
git checkout cursor/agent-monitoring-improve-2b9f 2>/dev/null || true
git pull --ff-only origin cursor/agent-monitoring-improve-2b9f || git pull --ff-only
source .venv/bin/activate
export VLLM_BASE_URL="${VLLM_BASE_URL:-http://127.0.0.1:8000}"
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

bash scripts/ensure_tiz_sources.sh

vllm_ok() {
  curl -fsS --max-time 5 http://127.0.0.1:8000/v1/models >/dev/null 2>&1
}

echo "==> vLLM"
if ! vllm_ok; then
  echo "vLLM DOWN — startuję w tmux sesji 'vllm' …"
  if command -v tmux >/dev/null 2>&1; then
    tmux has-session -t vllm 2>/dev/null || \
      tmux new-session -d -s vllm "cd '$HOME/NEW_TIZ_WEBSIDE-' && source .venv/bin/activate && VLLM_USE_FLASHINFER_SAMPLER=0 bash scripts/run_vllm_a100.sh"
  else
    nohup bash -c 'source .venv/bin/activate && VLLM_USE_FLASHINFER_SAMPLER=0 bash scripts/run_vllm_a100.sh' \
      >"$HOME/vllm_a100.log" 2>&1 &
    echo "nohup PID $! → ~/vllm_a100.log"
  fi
  echo "Czekam na API (do ~10 min, model 72B)…"
  for i in $(seq 1 120); do
    if vllm_ok; then
      echo "vLLM OK po ~$((i * 5))s"
      break
    fi
    sleep 5
    if (( i % 12 == 0 )); then
      echo "  … nadal ładuje ($((i * 5))s)"
    fi
  done
  if ! vllm_ok; then
    echo "vLLM nie wstał. Log: tmux attach -t vllm   LUB   tail -100 ~/vllm_a100.log"
    exit 1
  fi
fi
curl -fsS --max-time 8 http://127.0.0.1:8000/v1/models | head -c 300
echo

python -m market_agents doctor
python -m market_agents run --agentic
echo
echo "========== LATEST REPORT =========="
ls -lt reports/*.md 2>/dev/null | head -5 || true
LATEST="$(ls -t reports/*.md 2>/dev/null | head -1 || true)"
if [[ -n "${LATEST}" ]]; then
  echo "FILE: $LATEST"
  echo "----- BEGIN REPORT -----"
  cat "$LATEST"
  echo "----- END REPORT -----"
fi
