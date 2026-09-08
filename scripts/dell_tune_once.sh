#!/usr/bin/env bash
# Uruchom na DELLU (już po SSH). Jedna komenda: pull → ensure → doctor → agentic → wypisz raport.
set -euo pipefail
cd "${HOME}/NEW_TIZ_WEBSIDE-"
git fetch origin
git checkout cursor/agent-monitoring-improve-2b9f 2>/dev/null || true
git pull --ff-only origin cursor/agent-monitoring-improve-2b9f || git pull --ff-only
source .venv/bin/activate
export VLLM_BASE_URL="${VLLM_BASE_URL:-http://127.0.0.1:8000}"
bash scripts/ensure_tiz_sources.sh
echo "==> vLLM"
curl -fsS --max-time 8 http://127.0.0.1:8000/v1/models | head -c 300 || {
  echo "vLLM DOWN — start: VLLM_USE_FLASHINFER_SAMPLER=0 bash scripts/run_vllm_a100.sh"
  exit 1
}
echo
python -m market_agents doctor
python -m market_agents run --agentic
echo
echo "========== LATEST REPORT =========="
ls -lt reports/*.md 2>/dev/null | head -5
LATEST="$(ls -t reports/*.md 2>/dev/null | head -1 || true)"
if [[ -n "${LATEST}" ]]; then
  echo "FILE: $LATEST"
  echo "----- BEGIN REPORT -----"
  cat "$LATEST"
  echo "----- END REPORT -----"
fi
