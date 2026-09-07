#!/usr/bin/env bash
# MVP smoke — offline end-to-end bez GPU / Notion / sieci
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${PYTHONPATH:-.}"
CFG="${CFG:-config/mvp.example.yaml}"
DATA="${DATA:-data/mvp-smoke}"
OUT="${OUT:-/tmp/mvp-smoke-$$}"
mkdir -p "$OUT"

echo "=== MVP smoke ==="
echo "cfg=$CFG data=$DATA out=$OUT"

echo ""
echo "--- 1) Unit tests (MVP critical) ---"
python -m pytest \
  tests/test_mvp.py \
  tests/test_architecture_gaps.py \
  tests/test_architecture_hardening.py \
  -q --tb=line

echo ""
echo "--- 2) CLI mvp smoke (bootstrap + acceptance) ---"
python -m market_agents mvp smoke --config "$CFG" --data-dir "$DATA" --reset | tee "$OUT/mvp-smoke.json"
python - <<PY
import json
from pathlib import Path
from market_agents.mvp import mvp_acceptance

data = Path("$DATA")
acc = mvp_acceptance(data)
assert acc["ok"], acc
# sample prospect analyzed
profiles = json.loads((data / "knowledge" / "firm_profiles.json").read_text())
companies = {p.get("company") for p in (profiles.get("profiles") or [])}
assert "Beta Precision CNC" in companies, companies
# export readable
md = (data / "export" / "knowledge_export.md").read_text(encoding="utf-8")
assert "Firm scoreboard" in md or "scoreboard" in md.lower() or "Counts" in md
print("acceptance OK:", acc["counts"])
PY

echo ""
echo "--- 3) Worker pull from MVP fleet pack ---"
python - <<PY
from pathlib import Path
from market_agents.config import AgentsConfig, AppConfig, FleetConfig, IndustryConfig, SourcesConfig
from market_agents.fleet import FleetSync, fleet_config_from_app

central = Path("$DATA")
worker = Path("$OUT") / "worker"
worker.mkdir(parents=True)
(worker / "knowledge").mkdir(parents=True)

cfg = AppConfig(
    industry=IndustryConfig(name="MVP", keywords=["cnc"]),
    sources=SourcesConfig(),
    agents=AgentsConfig(
        data_dir=str(worker),
        fleet=FleetConfig(
            role="worker",
            worker_id="vps-mvp-smoke",
            sync_dir=str(central / "fleet"),
            auto_push_after_run=False,
        ),
        post_enrich_profiles=False,
        post_enrich_crm_tasks=False,
        post_enrich_suppliers=False,
        post_enrich_digest=False,
    ),
)
sync = FleetSync(worker, fleet_config_from_app(cfg))
pulled = sync.pull_knowledge()
assert pulled.get("ok"), pulled
assert (worker / "knowledge" / "firm_profiles.json").is_file()
assert (worker / "knowledge" / "ontology.json").is_file()
print("worker pull OK:", pulled.get("files"))
PY

echo ""
echo "=== MVP smoke PASSED ==="
echo "Data: $DATA"
echo "Export: $DATA/export/knowledge_export.md"
echo "Fleet: $DATA/fleet/latest.json"
