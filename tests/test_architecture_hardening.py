"""Architecture hardening: parity, approvals, fleet publish, post-enrich smoke."""

from __future__ import annotations

import json
from pathlib import Path

from market_agents.agents.orchestrator import Orchestrator
from market_agents.approvals import ApprovalStore
from market_agents.config import (
    AgentsConfig,
    AppConfig,
    FleetConfig,
    GovernanceConfig,
    IndustryConfig,
    SourcesConfig,
)
from market_agents.crm_tasks import CrmTaskStore, create_tasks_from_prospect_scoreboard
from market_agents.fleet import FleetSync, fleet_config_from_app
from market_agents.governance import GovernanceEngine, PolicyRequest, load_policy_pack
from market_agents.literature import LiteratureRegistry
from market_agents.memory import MarketMemory
from market_agents.pricelists import PricelistRegistry
from market_agents.product_tech import ProductTechRegistry
from market_agents.prospects import analyze_prospect_text, ProspectRegistry
from market_agents.tools import ToolRegistry


SAMPLE_PROSPECT = """
Beta Precision CNC manufactures steel housings and aluminum brackets.
Park: Haas VF-2, Doosan DNM 5700. Processes: milling, turning, drilling.
Uses Sandvik Coromant and Kennametal tooling. Revenue of EUR 12 million.
Employees: 85. Jan Nowak, Purchasing Manager.
"""


def _cfg(tmp: Path, **agent_kw) -> AppConfig:
    agents = AgentsConfig(
        data_dir=str(tmp),
        report_dir=str(tmp / "reports"),
        post_enrich_profiles=True,
        post_enrich_crm_tasks=True,
        crm_min_opportunity=50.0,
        fleet=FleetConfig(role="central", sync_dir=str(tmp / "fleet")),
        governance=GovernanceConfig(
            enabled=True,
            mode="enforce",
            policy_pack="config/governance/tiz.policy.yaml",
            audit_dir=str(tmp / "governance"),
        ),
        **{k: v for k, v in agent_kw.items() if k not in {
            "data_dir", "report_dir", "fleet", "governance",
            "post_enrich_profiles", "post_enrich_crm_tasks", "crm_min_opportunity",
        }},
    )
    # re-apply known kwargs that are AgentsConfig fields
    for k, v in agent_kw.items():
        if hasattr(agents, k):
            setattr(agents, k, v)
    return AppConfig(
        industry=IndustryConfig(
            name="Test",
            keywords=["cutting", "cnc"],
            competitors=["Sandvik Coromant"],
        ),
        sources=SourcesConfig(),
        agents=agents,
    )


def test_tool_schema_handler_parity():
    cfg = _cfg(Path("/tmp"))  # noqa: S108 — only for ToolRegistry init paths
    # use real tmp via MarketMemory path that may not exist — ToolRegistry still builds handlers
    tools = ToolRegistry(cfg, MarketMemory(Path("/tmp/ma-parity")))  # noqa: S108
    schema_names = {
        (t.get("function") or {}).get("name")
        for t in tools.openai_tools_schema()
        if (t.get("function") or {}).get("name")
    }
    handler_names = set(tools._handlers.keys())
    missing_handlers = schema_names - handler_names
    missing_schema = handler_names - schema_names
    assert not missing_handlers, f"schema without handler: {sorted(missing_handlers)}"
    assert not missing_schema, f"handler without schema: {sorted(missing_schema)}"
    assert "create_crm_task" in handler_names
    assert "analyze_prospect" in handler_names
    assert "build_firm_profiles" in handler_names


def test_governance_approve_unlocks_promote(tmp_path: Path):
    audit = tmp_path / "governance"
    pack = load_policy_pack("config/governance/tiz.policy.yaml")
    engine = GovernanceEngine(
        pack=pack,
        enabled=True,
        mode="enforce",
        audit_dir=audit,
        role="central",
        deny_require_approval=True,
    )
    blocked = engine.authorize_tool("promote_host_skill", {"host_or_url": "example.com"})
    assert blocked.allowed is False
    assert blocked.effect == "require_approval"

    store = ApprovalStore.load(audit)
    grant = store.approve("promote_host_skill", approved_by="tester", note="ok for CI")
    store.save(audit)

    allowed = engine.authorize_tool("promote_host_skill", {"host_or_url": "example.com"})
    assert allowed.allowed is True
    assert grant.id in " ".join(allowed.matched_rules) or "approval" in allowed.reason


def test_fleet_publish_includes_ontology_and_profiles(tmp_path: Path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir(parents=True)
    for name in (
        "ontology.json",
        "firm_profiles.json",
        "available_pricelists.json",
        "literature.json",
        "product_tech.json",
        "known_firms.json",
        "firm_relations.json",
        "site_parse_rules.json",
        "parsing_skills.json",
        "crawl_health.json",
        "crm_tasks.json",
    ):
        (knowledge / name).write_text("{}", encoding="utf-8")

    cfg = _cfg(tmp_path)
    sync = FleetSync(tmp_path, fleet_config_from_app(cfg))
    result = sync.publish_knowledge(notes="arch-test")
    assert result.get("ok") is True
    files = set(result.get("files") or [])
    assert "ontology.json" in files
    assert "firm_profiles.json" in files
    assert "available_pricelists.json" in files
    assert "literature.json" in files
    assert "product_tech.json" in files


def test_smoke_e2e_registries_and_post_enrich(tmp_path: Path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "known_firms.json").write_text(
        json.dumps(
            [
                {
                    "company": "Sandvik Coromant",
                    "country": "SE",
                    "site_url": "https://www.sandvik.coromant.com/",
                    "product_focus": "inserts",
                }
            ]
        ),
        encoding="utf-8",
    )

    # populate registries (smoke fill)
    prices = PricelistRegistry.load(tmp_path)
    prices.upsert(
        url="https://example.com/price.pdf",
        title="Demo pricelist",
        brand="Sandvik Coromant",
        source="test",
        asset_type="pricelist",
        access="public",
    )
    prices.save(tmp_path)

    lit = LiteratureRegistry.load(tmp_path)
    lit.upsert(
        url="https://example.com/book",
        title="Demo handbook",
        kind="book",
        brand="Sandvik Coromant",
    )
    lit.save(tmp_path)

    tech = ProductTechRegistry.load(tmp_path)
    tech.upsert(
        url="https://example.com/tech",
        title="Cutting data guide",
        brand="Sandvik Coromant",
        kind="handbook",
    )
    tech.save(tmp_path)

    # prospect + crm
    pr = ProspectRegistry.load(tmp_path)
    analysis = analyze_prospect_text(
        SAMPLE_PROSPECT, company="Beta Precision CNC", website="https://beta.example"
    )
    pr.upsert_from_analysis("Beta Precision CNC", analysis, tmp_path)
    crm = create_tasks_from_prospect_scoreboard(tmp_path, min_opportunity=40)
    assert crm["created_or_updated"] >= 1

    cfg = _cfg(tmp_path)
    orch = Orchestrator(cfg)
    result = orch.run(skip_llm=True)
    assert result.mode == "collect_only"
    assert (tmp_path / "knowledge" / "firm_profiles.json").is_file()
    assert (tmp_path / "knowledge" / "crm_tasks.json").is_file()
    assert (tmp_path / "knowledge" / "available_pricelists.json").is_file()
    assert (tmp_path / "knowledge" / "literature.json").is_file()
    assert (tmp_path / "knowledge" / "product_tech.json").is_file()

    # parsing agent prompt mentions prospects/profiles
    from market_agents.agents.parsing_agent import ParsingAgent

    agent = ParsingAgent(cfg, orch.llm, orch.memory)
    # inspect by building system string via a dry attribute — call run with empty is heavy;
    # instead read source contract via tool presence after agent constructs tools in run
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    names = set(tools._handlers)
    assert {"build_firm_profiles", "analyze_prospect", "create_crm_task"} <= names


def test_parsing_agent_prompt_mentions_profiles_prospects():
    src = Path("market_agents/agents/parsing_agent.py").read_text(encoding="utf-8")
    assert "PROSPECTS" in src or "analyze_prospect" in src
    assert "build_firm_profiles" in src
    assert "create_crm_task" in src
    assert "GUARDRAIL" in src
    assert "tooling OEM" in src


def test_approval_host_scope_matches_url(tmp_path: Path):
    from market_agents.approvals import ApprovalStore, normalize_host, host_matches
    from market_agents.governance import GovernanceEngine, load_policy_pack

    assert normalize_host("https://www.Example.COM/a") == "example.com"
    assert host_matches("example.com", "https://www.example.com/x")

    audit = tmp_path / "gov"
    pack = load_policy_pack("config/governance/tiz.policy.yaml")
    engine = GovernanceEngine(
        pack=pack, enabled=True, mode="enforce", audit_dir=audit, role="central"
    )
    store = ApprovalStore.load(audit)
    store.approve("promote_host_skill", host="allowed.example", approved_by="test")
    store.save(audit)

    assert engine.authorize_tool(
        "promote_host_skill", {"host_or_url": "https://evil.example/"}
    ).allowed is False
    ok = engine.authorize_tool(
        "promote_host_skill", {"host_or_url": "https://www.allowed.example/skills"}
    )
    assert ok.allowed is True
    rows = engine.recent_audit(limit=5)
    assert any(
        any(str(x).startswith("approval:") for x in (r.get("decision") or {}).get("matched_rules") or [])
        for r in rows
    )


def test_crm_cli_store(tmp_path: Path):
    store = CrmTaskStore.load(tmp_path)
    t = store.create(company="Acme", opportunity_score=80, reason="hot lead")
    assert t.priority == "hot"
    store.save(tmp_path)
    again = CrmTaskStore.load(tmp_path)
    assert again.list(status="open")
