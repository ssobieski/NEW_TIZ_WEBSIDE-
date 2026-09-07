"""Architecture gap fixes: worker enrich gate, fleet auto-publish, absorb signals, resolve rewrites."""

from __future__ import annotations

import json
from pathlib import Path

from market_agents.agents.orchestrator import Orchestrator
from market_agents.config import (
    AgentsConfig,
    AppConfig,
    FleetConfig,
    IndustryConfig,
    SecurityConfig,
    SourcesConfig,
)
from market_agents.crm_tasks import CrmTaskStore
from market_agents.entity_resolution import resolve_golden_records
from market_agents.firm_profiles import FirmProfile, FirmProfileRegistry
from market_agents.firm_relations import FirmRelationsGraph
from market_agents.fleet import FleetConfig as SyncFleetConfig
from market_agents.fleet import FleetSync
from market_agents.security import SAFE_TOOLS, WRITE_TOOLS, SENSITIVE_CLOUD_TOOLS, SecurityPolicy


def _cfg(tmp: Path, *, role: str = "central", **agent_kw) -> AppConfig:
    agents = AgentsConfig(
        data_dir=str(tmp),
        report_dir=str(tmp / "reports"),
        post_enrich_profiles=True,
        post_enrich_crm_tasks=True,
        post_enrich_suppliers=True,
        post_enrich_digest=True,
        fleet=FleetConfig(role=role, sync_dir=str(tmp / "fleet"), auto_push_after_run=True),
        security=SecurityConfig(allow_notion_tools=False, allow_write_tools=True),
    )
    for k, v in agent_kw.items():
        if hasattr(agents, k):
            setattr(agents, k, v)
    return AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cnc"], competitors=[]),
        sources=SourcesConfig(),
        agents=agents,
    )


def test_worker_skips_post_enrich(tmp_path: Path):
    cfg = _cfg(tmp_path, role="worker")
    # seed a marker file that enrich would overwrite if it ran build
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir(parents=True)
    marker = {"profiles": [{"id": "only", "company": "OnlyFirm", "roles": ["competitor"]}], "count": 1}
    (knowledge / "firm_profiles.json").write_text(json.dumps(marker), encoding="utf-8")
    before = (knowledge / "firm_profiles.json").read_text(encoding="utf-8")

    orch = Orchestrator(cfg)
    meta = orch._post_enrich()
    assert meta.get("skipped") is True
    assert (knowledge / "firm_profiles.json").read_text(encoding="utf-8") == before

    orch.run(skip_llm=True)
    assert (knowledge / "firm_profiles.json").read_text(encoding="utf-8") == before
    # metrics still written
    assert (tmp_path / "metrics" / "latest.json").is_file()


def test_central_auto_fleet_publish(tmp_path: Path):
    cfg = _cfg(tmp_path, role="central")
    orch = Orchestrator(cfg)
    orch.run(skip_llm=True)
    fleet_root = tmp_path / "fleet"
    assert (fleet_root / "latest.json").is_file()
    latest = json.loads((fleet_root / "latest.json").read_text(encoding="utf-8"))
    assert latest.get("pack_id")


def test_digest_is_write_tool_and_notion_push_gated():
    assert "refresh_change_digest" in WRITE_TOOLS
    assert "refresh_change_digest" not in SAFE_TOOLS
    assert "push_crm_to_notion" in SENSITIVE_CLOUD_TOOLS
    pol = SecurityPolicy(allow_notion_tools=False, allow_write_tools=True)
    ok, reason = pol.tool_allowed("push_crm_to_notion")
    assert ok is False
    assert "Notion" in reason


def test_entity_resolve_rewrites_crm_and_relations(tmp_path: Path):
    reg = FirmProfileRegistry()
    a = FirmProfile(id="sandvik-a", company="Sandvik Coromant", roles=["competitor"], sources=["seed"])
    b = FirmProfile(
        id="sandvik-b",
        company="Sandvik Coromant AB",
        roles=["competitor"],
        aliases=["Sandvik Coromant"],
        sources=["web"],
        websites=[{"url": "https://www.sandvik.coromant.com", "primary": True}],
    )
    reg.profiles[a.id] = a
    reg.profiles[b.id] = b
    reg.save(tmp_path)

    store = CrmTaskStore.load(tmp_path)
    # Loser name (fewer websites) — must be rewritten to golden record
    store.create(company="Sandvik Coromant", title="Check", opportunity_score=70)
    store.save(tmp_path)

    g = FirmRelationsGraph()
    g.add_relation("Sandvik Coromant", "Hoffmann Group", "distributor_of", confidence=0.8)
    g.save(tmp_path)

    result = resolve_golden_records(tmp_path, dry_run=False)
    assert result["duplicate_groups"] >= 1
    assert result["crm_rewritten"] >= 1
    assert result["relations_rewritten"] >= 1

    again = CrmTaskStore.load(tmp_path)
    companies = {t.company for t in again.tasks}
    assert "Sandvik Coromant" not in companies
    assert any("Sandvik Coromant AB" == c for c in companies)

    rel = FirmRelationsGraph.load(tmp_path)
    sources = {e.get("source") for e in rel.edges}
    targets = {e.get("target") for e in rel.edges}
    assert "Sandvik Coromant AB" in sources or "Sandvik Coromant AB" in targets
    # Seed must not reintroduce the pre-merge alias after resolve+reload.
    assert "Sandvik Coromant" not in sources
    assert "Sandvik Coromant" not in targets
    sandvik_distributor = [
        e
        for e in rel.edges
        if e.get("relation_type") == "distributor_of"
        and "sandvik" in str(e.get("source") or "").lower()
        and "hoffmann" in str(e.get("target") or "").lower()
    ]
    assert len(sandvik_distributor) == 1
    assert sandvik_distributor[0]["source"] == "Sandvik Coromant AB"


def test_flatten_alias_map_long_chain_rewrites_stores(tmp_path: Path):
    """Chains longer than 32 links must still resolve to the terminal canonical."""
    from market_agents.entity_resolution import (
        flatten_alias_map,
        rewrite_crm_companies,
        rewrite_relation_endpoints,
        save_alias_map,
    )
    from market_agents.firms import normalize_firm_name

    names = [f"Alias{i} Co" for i in range(40)] + ["Canonical Co"]
    chain: dict[str, str] = {}
    for a, b in zip(names, names[1:]):
        chain[normalize_firm_name(a)] = b
    flat = flatten_alias_map(chain)
    assert flat[normalize_firm_name("Alias0 Co")] == "Canonical Co"
    assert flat[normalize_firm_name("Alias39 Co")] == "Canonical Co"

    store = CrmTaskStore.load(tmp_path)
    store.create(company="Alias0 Co", title="Long chain", opportunity_score=70)
    store.save(tmp_path)
    g = FirmRelationsGraph()
    g.add_relation("Alias0 Co", "Partner GmbH", "partner_of", confidence=0.8)
    g.save(tmp_path)

    save_alias_map(tmp_path, flat)
    assert rewrite_crm_companies(tmp_path, flat) >= 1
    assert rewrite_relation_endpoints(tmp_path, flat) >= 1
    assert {t.company for t in CrmTaskStore.load(tmp_path).tasks} == {"Canonical Co"}
    rel = FirmRelationsGraph.load(tmp_path, include_seed=False)
    assert any(e.get("source") == "Canonical Co" for e in rel.edges)
    assert not any(e.get("source") == "Alias0 Co" for e in rel.edges)


def test_alias_chain_flattens_across_resolve_runs(tmp_path: Path):
    """old → mid, then mid → canonical must rewrite CRM/relations to canonical."""
    # Round 1: Old Co → Mid Co
    reg = FirmProfileRegistry()
    old = FirmProfile(id="old", company="Old Co", roles=["competitor"], sources=["seed"])
    mid = FirmProfile(
        id="mid",
        company="Mid Co",
        roles=["competitor"],
        aliases=["Old Co"],
        sources=["web"],
        websites=[{"url": "https://mid.example", "primary": True}],
    )
    reg.profiles[old.id] = old
    reg.profiles[mid.id] = mid
    reg.save(tmp_path)

    store = CrmTaskStore.load(tmp_path)
    store.create(company="Old Co", title="Outreach", opportunity_score=80)
    store.save(tmp_path)
    g = FirmRelationsGraph()
    g.add_relation("Old Co", "Partner GmbH", "partner_of", confidence=0.9)
    g.save(tmp_path)

    r1 = resolve_golden_records(tmp_path, dry_run=False)
    assert r1["duplicate_groups"] >= 1
    companies = {t.company for t in CrmTaskStore.load(tmp_path).tasks}
    assert companies == {"Mid Co"}

    # Round 2: Mid Co → Canonical Co (new profile wins on websites)
    reg = FirmProfileRegistry.load(tmp_path)
    # Drop leftover Mid if already alone; add Canonical with Mid as alias duplicate
    canon = FirmProfile(
        id="canon",
        company="Canonical Co",
        roles=["competitor"],
        aliases=["Mid Co"],
        sources=["web", "crm"],
        websites=[
            {"url": "https://canonical.example", "primary": True},
            {"url": "https://mid.example"},
        ],
    )
    # Ensure Mid still present so they form a duplicate group
    if "mid" not in reg.profiles and not any(p.company == "Mid Co" for p in reg.profiles.values()):
        reg.profiles["mid"] = FirmProfile(id="mid", company="Mid Co", roles=["competitor"], sources=["seed"])
    reg.profiles[canon.id] = canon
    reg.save(tmp_path)

    r2 = resolve_golden_records(tmp_path, dry_run=False)
    assert r2["duplicate_groups"] >= 1

    companies = {t.company for t in CrmTaskStore.load(tmp_path).tasks}
    assert companies == {"Canonical Co"}

    rel = FirmRelationsGraph.load(tmp_path, include_seed=False)
    partner_edges = [
        e for e in rel.edges if e.get("relation_type") == "partner_of" and "partner" in str(e.get("target") or "").lower()
    ]
    assert partner_edges
    assert all(e["source"] == "Canonical Co" for e in partner_edges)
    assert not any(e["source"] in {"Old Co", "Mid Co"} for e in partner_edges)

    from market_agents.entity_resolution import load_alias_map
    from market_agents.firms import normalize_firm_name

    aliases = load_alias_map(tmp_path)
    assert aliases[normalize_firm_name("Old Co")] == "Canonical Co"
    assert aliases[normalize_firm_name("Mid Co")] == "Canonical Co"


def test_post_enrich_marks_step_ok_false(tmp_path: Path):
    """Enabled step returning {ok: False} must fail overall post_enrich."""
    cfg = _cfg(tmp_path)
    cfg.agents.post_enrich_profiles = False
    cfg.agents.post_enrich_crm_tasks = False
    cfg.agents.post_enrich_suppliers = False
    cfg.agents.post_enrich_digest = False
    cfg.agents.post_enrich_crm_notion = True
    orch = Orchestrator(cfg)
    meta = orch._post_enrich()
    assert meta.get("ok") is False
    assert "crm_notion" in (meta.get("errors") or {})
    assert (meta.get("crm_notion") or {}).get("ok") is False


def test_absorb_items_tail(tmp_path: Path):
    sync_dir = tmp_path / "fleet"
    central = tmp_path / "central"
    worker = tmp_path / "worker"
    (central / "knowledge").mkdir(parents=True)
    (worker / "knowledge").mkdir(parents=True)
    (worker / "items.jsonl").write_text(
        json.dumps({"title": "News", "url": "https://example.com/a", "source": "rss"}) + "\n",
        encoding="utf-8",
    )
    w = FleetSync(worker, SyncFleetConfig(role="worker", worker_id="vps-test", sync_dir=str(sync_dir)))
    pushed = w.push_feedback()
    assert "items_tail.jsonl" in pushed["files"]

    c = FleetSync(central, SyncFleetConfig(role="central", sync_dir=str(sync_dir)))
    absorbed = c.absorb_feedback()
    assert absorbed["signals_appended"] >= 1
    signals = sync_dir / "inbox_signals.jsonl"
    assert signals.is_file()
    assert "example.com/a" in signals.read_text(encoding="utf-8")


def test_absorb_items_tail_skips_non_dict(tmp_path: Path):
    sync_dir = tmp_path / "fleet"
    central = tmp_path / "central"
    (central / "knowledge").mkdir(parents=True)
    pack = sync_dir / "inbox" / "vps-x" / "pack1"
    pack.mkdir(parents=True)
    (pack / "items_tail.jsonl").write_text(
        "\n".join(
            [
                json.dumps(["not", "a", "dict"]),
                json.dumps("string-row"),
                json.dumps({"title": "Ok", "url": "https://example.com/ok"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # marker so absorb finds the pack
    (pack / "manifest.json").write_text(
        json.dumps({"pack_id": "pack1", "role": "worker", "worker_id": "vps-x", "files": ["items_tail.jsonl"]}),
        encoding="utf-8",
    )
    c = FleetSync(central, SyncFleetConfig(role="central", sync_dir=str(sync_dir)))
    n = c._absorb_items_tail(pack / "items_tail.jsonl", worker_id="vps-x")
    assert n == 1
    text = (sync_dir / "inbox_signals.jsonl").read_text(encoding="utf-8")
    assert "example.com/ok" in text
    assert "not" not in text or '"url"' in text
