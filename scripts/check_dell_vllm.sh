#!/usr/bin/env bash
# Sprawdź vLLM na Dellu przez Cybertech VPN (lub localhost).
# Użycie:
#   export VLLM_BASE_URL=http://<dell-vpn-ip>:8000
#   bash scripts/check_dell_vllm.sh
set -euo pipefail

BASE="${VLLM_BASE_URL:-${MARKET_AGENTS_LLM_BASE_URL:-http://127.0.0.1:8000}}"
BASE="${BASE%/}"
# OpenAI-compatible path
if [[ "$BASE" == */v1 ]]; then
  MODELS_URL="${BASE}/models"
else
  MODELS_URL="${BASE}/v1/models"
fi

echo "==> Checking vLLM at $MODELS_URL"
if curl -fsS --connect-timeout 5 --max-time 15 "$MODELS_URL" | head -c 400; then
  echo
  echo "==> OK — Dell/vLLM osiągalny"
  exit 0
else
  echo
  echo "==> FAIL — brak odpowiedzi. Sprawdź:"
  echo "  1) Cybertech VPN podłączony"
  echo "  2) na Dellu: bash scripts/run_vllm_a100.sh (HOST=0.0.0.0)"
  echo "  3) VLLM_BASE_URL=http://<dell-vpn-ip>:8000"
  exit 1
fi
