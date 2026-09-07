"""Change digest, knowledge export, prompt/fleet wiring."""

from __future__ import annotations

import json
from pathlib import Path

from market_agents.agents.orchestrator import Orchestrator
from market_agents.change_digest import compute_digest, refresh_digest
from market_agents.config import AgentsConfig, AppConfig, FleetConfig, IndustryConfig, SourcesConfig
from market_agents.firm_profiles import FirmProfile, FirmProfileRegistry
from market_agents.fleet import FleetSync, fleet_config_from_app
from market_agents.knowledge_export import export_knowledge_pack
from market_agents.metrics import knowledge_counts, record_run_metrics
from market_agents.memory import MarketMemory
from market_agents.tools import ToolRegistry


def _cfg(tmp: Path, **agent_kw) -> AppConfig:
    agents = AgentsConfig(
        data_dir=str(tmp),
        report_dir=str(tmp / "reports"),
        post_enrich_profiles=True,
        post_enrich_crm_tasks=True,
        post_enrich_suppliers=True,
        post_enrich_digest=True,
        post_enrich_crm_notion=False,
        fleet=FleetConfig(role="central", sync_dir=str(tmp / "fleet")),
    )
    for k, v in agent_kw.items():
        if hasattr(agents, k):
            setattr(agents, k, v)
    return AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cnc"], competitors=["Sandvik"]),
        sources=SourcesConfig(),
        agents=agents,
    )


def test_digest_detects_score_delta(tmp_path: Path):
    reg = FirmProfileRegistry()
    p = FirmProfile(id="acme", company="Acme CNC", roles=["prospect"], scores={"overall": 40})
    p.prospect = {"opportunity": {"overall": 55}}
    reg.profiles[p.id] = p
    reg.save(tmp_path)

    first = refresh_digest(tmp_path, min_delta=5)
    assert first["ok"]
    assert first["digest"]["has_previous"] is False

    p.scores["overall"] = 70
    p.prospect["opportunity"]["overall"] = 80
    p.scores["supplier_overall"] = 10
    reg.profiles[p.id] = p
    reg.save(tmp_path)

    second = refresh_digest(tmp_path, min_delta=5)
    deltas = second["digest"]["score_deltas"]
    dims = {d["dimension"] for d in deltas}
    assert "overall" in dims or "opportunity" in dims
    md = Path(second["markdown_path"])
    assert md.is_file()
    assert "Acme" in md.read_text(encoding="utf-8")


def test_knowledge_export_and_metrics_counts(tmp_path: Path):
    reg = FirmProfileRegistry()
    reg.upsert(
        FirmProfile(
            id="supp1",
            company="Dist Co",
            roles=["supplier"],
            network={"customers": ["A", "B"]},
        )
    )
    reg.upsert(
        FirmProfile(
            id="pro1",
            company="Plant Co",
            roles=["prospect"],
            prospect={"opportunity": {"overall": 70}, "vertical": "automotive"},
        )
    )
    reg.save(tmp_path)

    counts = knowledge_counts(tmp_path)
    assert counts["suppliers"] >= 1
    assert counts["prospects"] >= 1

    record_run_metrics(tmp_path, mode="collect_only", new_items=0)
    assert (tmp_path / "knowledge" / "run_status.json").is_file()

    out = export_knowledge_pack(tmp_path, tmp_path / "export", limit=10)
    assert out["ok"]
    assert Path(out["markdown"]).is_file()
    assert "Dist Co" in Path(out["markdown"]).read_text(encoding="utf-8")


def test_orchestrator_writes_digest_and_run_status(tmp_path: Path):
    cfg = _cfg(tmp_path)
    orch = Orchestrator(cfg)
    orch.run(skip_llm=True)
    assert (tmp_path / "metrics" / "digest_latest.md").is_file()
    assert (tmp_path / "knowledge" / "run_status.json").is_file()
    status = json.loads((tmp_path / "knowledge" / "run_status.json").read_text(encoding="utf-8"))
    assert "knowledge_counts" in status


def test_fleet_publishes_run_status(tmp_path: Path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir(parents=True)
    for name in (
        "site_parse_rules.json",
        "parsing_skills.json",
        "crawl_health.json",
        "firm_profiles.json",
        "crm_tasks.json",
        "run_status.json",
        "ontology.json",
    ):
        (knowledge / name).write_text("{}", encoding="utf-8")
    cfg = _cfg(tmp_path)
    sync = FleetSync(tmp_path, fleet_config_from_app(cfg))
    result = sync.publish_knowledge()
    assert result["ok"]
    assert "run_status.json" in set(result.get("files") or [])


def test_prompt_mentions_suppliers_and_metrics():
    src = Path("market_agents/agents/parsing_agent.py").read_text(encoding="utf-8")
    assert "SUPPLIERS" in src
    assert "score_suppliers" in src
    assert "list_run_metrics" in src
    assert "resolve_firm_duplicates" in src


def test_digest_compute_unit():
    prev = {
        "ts": "t0",
        "crm_open": 1,
        "firms": {"X": {"overall": 10, "supplier_overall": 0, "opportunity": 0, "completeness": 0, "roles": ["competitor"]}},
    }
    cur = {
        "ts": "t1",
        "crm_open": 3,
        "firm_count": 2,
        "firms": {
            "X": {"overall": 40, "supplier_overall": 0, "opportunity": 0, "completeness": 0, "roles": ["competitor"]},
            "Y": {"overall": 5, "supplier_overall": 0, "opportunity": 0, "completeness": 0, "roles": ["prospect"]},
        },
    }
    d = compute_digest(prev, cur, min_delta=5)
    assert d["crm_open_delta"] == 2
    assert "Y" in d["new_firms"]
    assert any(x["company"] == "X" and x["dimension"] == "overall" for x in d["score_deltas"])


def test_new_tools_parity(tmp_path: Path):
    tools = ToolRegistry(_cfg(tmp_path), MarketMemory(tmp_path))
    names = set(tools._handlers)
    assert {"refresh_change_digest", "export_knowledge_pack"} <= names
    schema = {
        (t.get("function") or {}).get("name")
        for t in tools.openai_tools_schema()
        if (t.get("function") or {}).get("name")
    }
    assert names == schema
