#!/usr/bin/env bash
# Sprawdź vLLM na Dellu przez Cybertech VPN (lub localhost).
# Użycie:
#   export VLLM_BASE_URL=http://<dell-vpn-ip>:8000
#   bash scripts/check_dell_vllm.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi
BASE="${VLLM_BASE_URL:-${MARKET_AGENTS_LLM_BASE_URL:-http://127.0.0.1:8000}}"
BASE="${BASE%/}"
# OpenAI-compatible path
if [[ "$BASE" == */v1 ]]; then
  MODELS_URL="${BASE}/models"
else
  MODELS_URL="${BASE}/v1/models"
fi

API_KEY="${MARKET_AGENTS_LLM_API_KEY:-${VLLM_API_KEY:-}}"
if [[ -z "$API_KEY" || "$API_KEY" == "EMPTY" ]]; then
  echo "WARN: brak MARKET_AGENTS_LLM_API_KEY / VLLM_API_KEY — vLLM odrzuci zapytanie (401)"
  API_KEY=""
fi

echo "==> Checking vLLM at $MODELS_URL"
# Header via stdin so the key does not appear in the process list.
if printf 'Authorization: Bearer %s\n' "$API_KEY" \
  | curl -fsS --connect-timeout 5 --max-time 15 -H @- "$MODELS_URL" | head -c 400; then
  echo
  echo "==> OK — Dell/vLLM osiągalny"
  exit 0
else
  echo
  echo "==> FAIL — brak odpowiedzi. Sprawdź:"
  echo "  1) Cybertech VPN podłączony"
  echo "  2) na Dellu: HOST=<dell-vpn-ip> VLLM_API_KEY=... bash scripts/run_vllm_a100.sh"
  echo "  3) VLLM_BASE_URL=http://<dell-vpn-ip>:8000"
  echo "  4) MARKET_AGENTS_LLM_API_KEY = ten sam klucz co VLLM_API_KEY na Dellu"
  exit 1
fi
