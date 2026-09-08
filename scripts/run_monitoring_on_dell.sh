#!/usr/bin/env bash
# Uruchom monitoring z Maca (VPN ON) na Dellu przez SSH.
# Wymaga: ~/dell_agent, Cybertech VPN, vLLM na Dellu.
set -euo pipefail

DELL_SSH="${DELL_SSH:-ssobieski@200.0.0.110}"
KEY="${DELL_SSH_KEY:-$HOME/dell_agent}"
BRANCH="${BRANCH:-cursor/agent-monitoring-improve-2b9f}"
SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10)

echo "==> SSH $DELL_SSH"
"${SSH[@]}" "$DELL_SSH" 'hostname && curl -fsS --max-time 8 http://127.0.0.1:8000/v1/models | head -c 200; echo'

echo "==> Pull + ensure sources + doctor + run --agentic (branch=$BRANCH)"
"${SSH[@]}" "$DELL_SSH" bash -s <<EOF
set -euo pipefail
cd "\$HOME/NEW_TIZ_WEBSIDE-"
git fetch origin
git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" "origin/$BRANCH"
git pull --ff-only origin "$BRANCH" || git pull --ff-only
source .venv/bin/activate
export VLLM_BASE_URL=http://127.0.0.1:8000
chmod +x scripts/ensure_tiz_sources.sh
bash scripts/ensure_tiz_sources.sh
python -m market_agents doctor
python -m market_agents run --agentic
ls -lt reports | head -5
EOF

echo "==> DONE"
