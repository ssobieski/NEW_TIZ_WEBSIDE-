"""Contacts + deals pipeline: buying center, strategy, sync from prospects."""

from __future__ import annotations

import json
from pathlib import Path

from market_agents.contacts import (
    ContactRegistry,
    contact_from_stakeholder,
    sync_contacts_from_profiles,
    upsert_manual_contact,
)
from market_agents.deals import (
    DealRegistry,
    build_deal_strategy,
    enrich_deal_strategy_with_llm,
    get_deal_strategy,
    record_deal_outcome,
    sync_deals_from_prospects,
    win_loss_report,
)
from market_agents.firm_profiles import FirmProfile, FirmProfileRegistry
from market_agents.security import SAFE_TOOLS, WRITE_TOOLS


def _seed_prospect_profile(tmp: Path) -> FirmProfile:
    profile = FirmProfile(
        id="acme-cnc",
        company="Acme CNC",
        roles=["prospect"],
        country="PL",
        prospect={
            "vertical": "automotive",
            "quality_tier": "mid",
            "budget": {"mid_eur": 120000, "tooling_budget_mid_eur": 120000},
            "buys_from": [{"brand": "Sandvik Coromant"}, {"brand": "Iscar"}],
            "product_map": {
                "product_families": ["shafts", "housings"],
                "likely_processes": ["turning", "milling"],
                "practical_tools": ["carbide inserts", "end mills"],
            },
            "stakeholders": [
                {
                    "name": "Anna Kowalska",
                    "title": "Purchasing Manager",
                    "role": "purchasing",
                    "influence_on_purchase": "high",
                    "source": "website_heuristic",
                },
                {
                    "name": "Piotr Nowak",
                    "title": "Production Director",
                    "role": "production",
                    "influence_on_purchase": "high",
                    "source": "website_heuristic",
                },
                {
                    "name": None,
                    "title": "Quality Manager",
                    "role": "quality",
                    "influence_on_purchase": "medium",
                    "source": "function_mention",
                },
            ],
            "opportunity": {"overall": 72},
        },
    )
    reg = FirmProfileRegistry([profile])
    reg.save(tmp)
    return profile


def test_contact_from_stakeholder_and_registry(tmp_path: Path):
    sh = {
        "name": "Anna Kowalska",
        "title": "Purchasing Manager",
        "role": "purchasing",
        "influence_on_purchase": "high",
    }
    c = contact_from_stakeholder(sh, company="Acme CNC", competitors=["Sandvik"])
    assert c is not None
    assert c.role == "purchasing"
    assert c.firm_links[0]["company"] == "Acme CNC"
    assert "Sandvik" in c.competitors_mentioned

    reg = ContactRegistry()
    saved, created = reg.upsert(c)
    assert created
    saved2, created2 = reg.upsert(c)
    assert not created2
    assert saved2.id == saved.id
    path = reg.save(tmp_path)
    assert path.is_file()
    loaded = ContactRegistry.load(tmp_path)
    assert len(loaded.contacts) == 1
    assert loaded.for_firm("Acme CNC")[0].name == "Anna Kowalska"


def test_sync_contacts_skips_nameless(tmp_path: Path):
    _seed_prospect_profile(tmp_path)
    result = sync_contacts_from_profiles(tmp_path)
    assert result["ok"]
    assert result["created"] == 2  # only named stakeholders
    reg = ContactRegistry.load(tmp_path)
    assert len(reg.contacts) == 2
    board = reg.scoreboard()
    assert board[0]["influence_on_purchase"] == "high"


def test_manual_contact_upsert(tmp_path: Path):
    out = upsert_manual_contact(
        tmp_path,
        name="Jan Dealer",
        company="Sandvik Coromant",
        role="sales",
        relation="competitor_rep",
        email="jan@example.com",
        influence="medium",
    )
    assert out["ok"]
    assert out["created"]
    reg = ContactRegistry.load(tmp_path)
    c = reg.contacts[0]
    assert c.emails[0]["value"] == "jan@example.com"
    assert c.firm_links[0]["relation"] == "competitor_rep"


def test_build_deal_strategy_and_pipeline(tmp_path: Path):
    _seed_prospect_profile(tmp_path)
    sync_contacts_from_profiles(tmp_path)
    contacts = [c.to_dict() for c in ContactRegistry.load(tmp_path).for_firm("Acme CNC")]
    prospect = list(FirmProfileRegistry.load(tmp_path).profiles.values())[0].prospect
    strategy = build_deal_strategy(
        company="Acme CNC",
        prospect=prospect,
        contacts=contacts,
        competitors_industry=["Sandvik Coromant", "Iscar", "Kennametal"],
        our_brand="TIZ",
    )
    assert strategy["buying_center"]
    assert strategy["next_actions"]
    assert strategy["outreach_sequence"]
    assert "Sandvik" in str(strategy["win_themes"])
    assert strategy["value_eur_mid"] == 120000

    sync = sync_deals_from_prospects(
        tmp_path,
        min_opportunity=50,
        create_crm=True,
        competitors_industry=["Sandvik Coromant"],
    )
    assert sync["ok"]
    assert sync["created"] >= 1
    deals = DealRegistry.load(tmp_path)
    assert deals.pipeline()
    d = deals.find_open_for_company("Acme CNC")
    assert d is not None
    assert d.strategy.get("value_proposition")
    assert d.contact_ids
    assert d.competitive_context.get("incumbents")

    got = get_deal_strategy(tmp_path, "Acme CNC")
    assert got["ok"]
    assert got["strategy"]["buying_center"]

    d2 = deals.set_stage(d.id, "propose")
    assert d2 is not None
    assert d2.stage == "propose"
    assert d2.probability == 0.55


def test_contact_deal_tools_in_security_sets():
    for name in (
        "list_contacts",
        "get_contact",
        "contact_scoreboard",
        "list_deals",
        "get_deal",
        "deal_pipeline",
        "get_deal_strategy",
    ):
        assert name in SAFE_TOOLS
    for name in (
        "upsert_contact",
        "sync_contacts_from_prospects",
        "sync_deals_from_prospects",
        "upsert_deal",
        "set_deal_stage",
    ):
        assert name in WRITE_TOOLS


def test_tool_handlers_contacts_deals(tmp_path: Path):
    from market_agents.config import AgentsConfig, AppConfig, IndustryConfig, SourcesConfig
    from market_agents.memory import MarketMemory
    from market_agents.tooling.registry import ToolRegistry

    _seed_prospect_profile(tmp_path)
    cfg = AppConfig(
        industry=IndustryConfig(
            name="Test", keywords=["cnc"], competitors=["Sandvik Coromant"]
        ),
        sources=SourcesConfig(),
        agents=AgentsConfig(data_dir=str(tmp_path), report_dir=str(tmp_path / "reports")),
    )
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    sync = tools.call("sync_deals_from_prospects", {"min_opportunity": 50, "create_crm": True})
    assert sync.get("ok")
    contacts = tools.call("list_contacts", {"company": "Acme"})
    assert contacts["count"] >= 2
    pipeline = tools.call("deal_pipeline", {"limit": 10})
    assert pipeline["pipeline"]
    strategy = tools.call("get_deal_strategy", {"company": "Acme CNC"})
    assert strategy["ok"]
    board = tools.call("contact_scoreboard", {})
    assert board["scoreboard"]


def test_ingest_contact_seeds(tmp_path: Path, monkeypatch):
    seed = tmp_path / "contacts.seed.json"
    seed.write_text(
        json.dumps(
            [
                {
                    "name": "Seed Buyer",
                    "company": "Seed Corp",
                    "role": "purchasing",
                    "influence": "high",
                    "relation": "buys_for",
                }
            ]
        ),
        encoding="utf-8",
    )
    from market_agents.contacts import ContactRegistry, ingest_contact_seeds

    out = ingest_contact_seeds(tmp_path, seed_path=seed)
    assert out["ok"]
    assert out["created"] == 1
    assert ContactRegistry.load(tmp_path).contacts[0].name == "Seed Buyer"


def test_win_loss_and_llm_enrich(tmp_path: Path):
    _seed_prospect_profile(tmp_path)
    sync_deals_from_prospects(tmp_path, min_opportunity=50, create_crm=False)
    deals = DealRegistry.load(tmp_path)
    d = deals.find_open_for_company("Acme CNC")
    assert d is not None
    out = record_deal_outcome(
        tmp_path,
        deal_id=d.id,
        result="lost",
        competitor="Sandvik Coromant",
        reason="price + incumbent lock-in",
        lessons="Need trial kit offer",
    )
    assert out["ok"]
    assert out["deal"]["stage"] == "lost"
    report = win_loss_report(tmp_path)
    assert report["lost"] == 1
    assert report["by_competitor"][0]["competitor"] == "Sandvik Coromant"

    # reopen-ish: create another open deal via sync after resetting
    # LLM enrich without LLM → skipped
    # first create fresh open deal
    from market_agents.deals import Deal, deal_id_from_company, enrich_deal_strategy_with_llm

    open_deal = Deal(
        id=deal_id_from_company("Acme CNC"),
        company="Acme CNC",
        firm_id="acme-cnc",
        title="Retry",
        stage="qualify",
        sources=["test"],
    )
    deals2 = DealRegistry.load(tmp_path)
    deals2.upsert(open_deal)
    deals2.save(tmp_path)

    skipped = enrich_deal_strategy_with_llm(tmp_path, "Acme CNC", llm=None)
    assert skipped["ok"]
    assert skipped["strategy"]["llm_enrichment"]["status"] == "skipped"

    class FakeLLM:
        def chat(self, system: str, user: str) -> str:
            return json.dumps(
                {
                    "pitch": "TIZ second source na wałach automotive",
                    "objection_handling": ["Porównaj TCO vs Sandvik"],
                    "discovery_questions": ["Jaki % scrap na insercie?"],
                    "email_opener": "Dzień dobry — krótka propozycja trial tooling",
                }
            )

    enriched = enrich_deal_strategy_with_llm(tmp_path, "Acme CNC", llm=FakeLLM())
    assert enriched["llm_enriched"] is True
    assert "pitch" in enriched["strategy"]["llm_enrichment"]


def test_notion_push_dry_run(tmp_path: Path):
    from market_agents.config import (
        AgentsConfig,
        AppConfig,
        IndustryConfig,
        NotionConfig,
        SourcesConfig,
    )
    from market_agents.contacts import push_contacts_to_notion, upsert_manual_contact
    from market_agents.deals import Deal, DealRegistry, deal_id_from_company, push_deals_to_notion

    upsert_manual_contact(
        tmp_path,
        name="Notion Contact",
        company="Acme CNC",
        influence="high",
        role="purchasing",
    )
    reg = DealRegistry(
        [
            Deal(
                id=deal_id_from_company("Acme CNC"),
                company="Acme CNC",
                stage="qualify",
                title="Deal Acme",
            )
        ]
    )
    reg.save(tmp_path)

    cfg = AppConfig(
        industry=IndustryConfig(name="T", keywords=["x"], competitors=[]),
        sources=SourcesConfig(
            notion=NotionConfig(
                enabled=True,
                contacts_parent_page="https://www.notion.so/parent",
                deals_parent_page="https://www.notion.so/deals",
            )
        ),
        agents=AgentsConfig(data_dir=str(tmp_path), report_dir=str(tmp_path / "r")),
    )
    # no token → error path
    assert push_contacts_to_notion(cfg, dry_run=False)["ok"] is False
    c_dry = push_contacts_to_notion(cfg, dry_run=True)
    assert c_dry["ok"] and c_dry["dry_run"] and c_dry["would_push"] >= 1
    d_dry = push_deals_to_notion(cfg, dry_run=True)
    assert d_dry["ok"] and d_dry["would_push"] >= 1


def test_new_tools_in_security_sets():
    for name in ("win_loss_report",):
        assert name in SAFE_TOOLS
    for name in (
        "ingest_contact_seeds",
        "record_deal_outcome",
        "enrich_deal_strategy",
        "push_contacts_to_notion",
        "push_deals_to_notion",
    ):
        assert name in WRITE_TOOLS
    from market_agents.security import SENSITIVE_CLOUD_TOOLS

    assert "push_contacts_to_notion" in SENSITIVE_CLOUD_TOOLS
    assert "push_deals_to_notion" in SENSITIVE_CLOUD_TOOLS
