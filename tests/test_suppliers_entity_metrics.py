"""Suppliers scorecard, entity resolution, run metrics."""

from __future__ import annotations

import json
from pathlib import Path

from market_agents.agents.orchestrator import Orchestrator
from market_agents.config import (
    AgentsConfig,
    AppConfig,
    IndustryConfig,
    SourcesConfig,
)
from market_agents.crm_tasks import CrmTaskStore, push_crm_tasks_to_notion
from market_agents.entity_resolution import (
    find_duplicate_groups,
    resolve_golden_records,
)
from market_agents.firm_profiles import FirmProfile, FirmProfileRegistry
from market_agents.metrics import recent_metrics, record_run_metrics
from market_agents.suppliers import SupplierRegistry, compute_supplier_scorecard
from market_agents.tools import ToolRegistry
from market_agents.memory import MarketMemory


def _cfg(tmp: Path) -> AppConfig:
    return AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cnc"], competitors=["Sandvik"]),
        sources=SourcesConfig(),
        agents=AgentsConfig(
            data_dir=str(tmp),
            report_dir=str(tmp / "reports"),
            post_enrich_profiles=True,
            post_enrich_crm_tasks=True,
            post_enrich_suppliers=True,
            post_enrich_resolve_duplicates=False,
        ),
    )


def test_supplier_scorecard_and_refresh(tmp_path: Path):
    reg = FirmProfileRegistry()
    p = FirmProfile(
        id="acme-tools",
        company="Acme Tools Dist",
        roles=["supplier", "distributor"],
        network={"customers": ["Beta CNC", "Gamma Mold"], "dealers": ["Local Tools"]},
        aliases=["Acme"],
        sources=["seed", "firm_relations"],
        websites=[{"url": "https://acme.example", "primary": True, "status": "heuristic"}],
        emails=[{"value": "sales@acme.example", "status": "heuristic"}],
    )
    reg.upsert(p)
    reg.save(tmp_path)

    sc = compute_supplier_scorecard(reg.profiles[p.id])
    assert sc["overall"] > 0
    assert any(r["dimension"] == "channel_reach" for r in sc["table"])

    sreg = SupplierRegistry.load(tmp_path)
    result = sreg.refresh_and_save(tmp_path)
    assert result["ok"] is True
    assert result["scored"] >= 1
    board = sreg.scoreboard(limit=5)
    assert board and board[0]["company"] == "Acme Tools Dist"
    assert board[0]["supplier_overall"] > 0


def test_entity_resolution_merges_duplicates(tmp_path: Path):
    reg = FirmProfileRegistry()
    a = FirmProfile(
        id="sandvik-a",
        company="Sandvik Coromant",
        roles=["competitor"],
        websites=[{"url": "https://www.sandvik.coromant.com", "primary": True, "status": "heuristic"}],
        sources=["seed"],
    )
    b = FirmProfile(
        id="sandvik-b",
        company="Sandvik Coromant AB",
        roles=["competitor"],
        aliases=["Sandvik Coromant"],
        emails=[{"value": "info@sandvik.example", "status": "heuristic"}],
        sources=["web"],
    )
    reg.upsert(a)
    # force second id retained
    reg.profiles[b.id] = b
    reg.save(tmp_path)

    groups = find_duplicate_groups(list(FirmProfileRegistry.load(tmp_path).profiles.values()))
    assert groups

    dry = resolve_golden_records(tmp_path, dry_run=True)
    assert dry["duplicate_groups"] >= 1
    assert dry["dry_run"] is True

    applied = resolve_golden_records(tmp_path, dry_run=False)
    assert applied["duplicate_groups"] >= 1
    after = FirmProfileRegistry.load(tmp_path)
    assert applied["profiles_after"] == len(after.profiles)
    assert len(after.profiles) == 1
    # one golden remains with alias
    companies = {p.company for p in after.profiles.values()}
    assert any("Sandvik" in c for c in companies)


def test_metrics_record_and_orchestrator(tmp_path: Path):
    row = record_run_metrics(tmp_path, mode="collect_only", new_items=0)
    assert (tmp_path / "metrics" / "runs.jsonl").is_file()
    assert (tmp_path / "metrics" / "latest.json").is_file()
    assert recent_metrics(tmp_path, limit=5)
    assert row["knowledge_counts"]["firm_profiles"] == 0

    cfg = _cfg(tmp_path)
    orch = Orchestrator(cfg)
    result = orch.run(skip_llm=True)
    assert result.mode == "collect_only"
    latest = json.loads((tmp_path / "metrics" / "latest.json").read_text(encoding="utf-8"))
    assert latest["mode"] == "collect_only"
    assert "post_enrich" in latest


def test_push_crm_to_notion_requires_config(tmp_path: Path):
    cfg = _cfg(tmp_path)
    store = CrmTaskStore.load(tmp_path)
    store.create(company="Hot Co", title="Outreach", opportunity_score=90)
    store.save(tmp_path)
    out = push_crm_tasks_to_notion(cfg, dry_run=True)
    assert out["ok"] is False
    assert "Notion" in out["error"] or "crm_parent" in out["error"] or "disabled" in out["error"].lower()


def test_new_tools_in_registry(tmp_path: Path):
    cfg = _cfg(tmp_path)
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    names = set(tools._handlers)
    assert {
        "list_suppliers",
        "supplier_scoreboard",
        "score_suppliers",
        "resolve_firm_duplicates",
        "list_run_metrics",
        "push_crm_to_notion",
    } <= names
    # parity
    schema = {
        (t.get("function") or {}).get("name")
        for t in tools.openai_tools_schema()
        if (t.get("function") or {}).get("name")
    }
    assert names == schema
