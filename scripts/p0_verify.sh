#!/usr/bin/env bash
# P0 verification: prompt + fleet + approval (plan acceptance)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
CFG="${CFG:-config/tiz_cutting_tools.example.yaml}"
OUT="${OUT:-/tmp/p0-verify-$$}"
mkdir -p "$OUT"
export PYTHONPATH="${PYTHONPATH:-.}"

echo "=== P0 verify ==="
echo "cfg=$CFG out=$OUT"

echo ""
echo "--- 1) Prompt guardrail (OEM vs prospect) ---"
rg -n "GUARDRAIL RÓL|tooling OEM|PROSPECT" market_agents/agents/parsing_agent.py
test "$(rg -c "GUARDRAIL RÓL" market_agents/agents/parsing_agent.py || true)" -ge 1

echo ""
echo "--- 2) Unit/integration tests (P0) ---"
python -m pytest \
  tests/test_architecture_hardening.py \
  tests/test_governance.py \
  tests/test_prospects.py \
  -q --tb=line

echo ""
echo "--- 3) Fleet publish contents ---"
# Use isolated data/fleet under OUT via temp industry copy isn't needed —
# publish into default data/fleet but also run pytest fleet test which is authoritative.
python - <<PY
from pathlib import Path
from market_agents.config import AgentsConfig, AppConfig, FleetConfig, IndustryConfig, SourcesConfig
from market_agents.fleet import FleetSync, fleet_config_from_app

tmp = Path("$OUT") / "fleet-data"
knowledge = tmp / "knowledge"
knowledge.mkdir(parents=True)
for name in (
    "ontology.json", "firm_profiles.json", "available_pricelists.json",
    "literature.json", "product_tech.json", "known_firms.json",
    "firm_relations.json", "site_parse_rules.json", "parsing_skills.json",
    "crawl_health.json", "crm_tasks.json",
):
    (knowledge / name).write_text("{}", encoding="utf-8")

cfg = AppConfig(
    industry=IndustryConfig(name="P0", keywords=["cutting"]),
    sources=SourcesConfig(),
    agents=AgentsConfig(
        data_dir=str(tmp),
        fleet=FleetConfig(role="central", sync_dir=str(tmp / "fleet")),
    ),
)
sync = FleetSync(tmp, fleet_config_from_app(cfg))
result = sync.publish_knowledge(notes="p0-verify")
assert result.get("ok"), result
files = set(result.get("files") or [])
need = {"ontology.json", "firm_profiles.json", "available_pricelists.json",
        "literature.json", "product_tech.json"}
missing = need - files
assert not missing, f"fleet pack missing: {missing} got={sorted(files)}"
print("fleet pack OK:", sorted(files))

# worker pull smoke
worker_data = Path("$OUT") / "worker-data"
worker_data.mkdir(parents=True)
(worker_data / "knowledge").mkdir(parents=True, exist_ok=True)
wcfg = AppConfig(
    industry=IndustryConfig(name="P0", keywords=["cutting"]),
    sources=SourcesConfig(),
    agents=AgentsConfig(
        data_dir=str(worker_data),
        fleet=FleetConfig(
            role="worker",
            worker_id="vps-p0-test",
            sync_dir=str(tmp / "fleet"),
        ),
    ),
)
wsync = FleetSync(worker_data, fleet_config_from_app(wcfg))
pulled = wsync.pull_knowledge() if hasattr(wsync, "pull_knowledge") else wsync.pull()
print("worker pull:", pulled if isinstance(pulled, dict) else pulled)
# knowledge should contain ontology after pull
ont = worker_data / "knowledge" / "ontology.json"
assert ont.is_file(), f"worker missing ontology after pull: {ont}"
print("worker ontology OK:", ont)
PY

echo ""
echo "--- 4) Approval runbook (deny → approve → allow → revoke → deny) ---"
python - <<PY
from pathlib import Path
from market_agents.approvals import ApprovalStore
from market_agents.governance import GovernanceEngine, load_policy_pack

audit = Path("$OUT") / "gov"
pack = load_policy_pack("config/governance/tiz.policy.yaml")
engine = GovernanceEngine(
    pack=pack, enabled=True, mode="enforce", audit_dir=audit, role="central"
)

# unscoped deny
d1 = engine.authorize_tool("promote_host_skill", {"host_or_url": "https://www.example.com/x"})
assert d1.allowed is False and d1.effect == "require_approval", d1
print("deny OK:", d1.reason)

# host-scoped grant — wrong host still deny
store = ApprovalStore.load(audit)
g = store.approve("promote_host_skill", approved_by="p0-verify", host="allowed.example", note="scoped")
store.save(audit)
d_wrong = engine.authorize_tool("promote_host_skill", {"host_or_url": "https://www.example.com/x"})
assert d_wrong.allowed is False, d_wrong
print("wrong-host still deny OK")

# matching host (www + path)
d_ok = engine.authorize_tool(
    "promote_host_skill", {"host_or_url": "https://www.allowed.example/path"}
)
assert d_ok.allowed is True, d_ok
print("host-scope allow OK:", d_ok.reason)

# audit has approval marker
rows = engine.recent_audit(limit=20)
assert any("approval" in str(r.get("decision", {})).lower() or
           any(str(x).startswith("approval:") for x in (r.get("decision") or {}).get("matched_rules") or [])
           for r in rows), rows[-3:]
print("audit OK, last decisions:", [r.get("decision", {}).get("effect") for r in rows[-5:]])

# revoke
assert store.revoke(g.id)
store.save(audit)
d3 = engine.authorize_tool(
    "promote_host_skill", {"host_or_url": "https://www.allowed.example/path"}
)
assert d3.allowed is False, d3
print("revoke → deny OK")
PY

echo ""
echo "=== P0 verify PASSED ==="
echo "Artifacts under $OUT"
