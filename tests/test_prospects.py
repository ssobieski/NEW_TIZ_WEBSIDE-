from __future__ import annotations

from pathlib import Path

from market_agents.config import AgentsConfig, AppConfig, IndustryConfig, SourcesConfig
from market_agents.memory import MarketMemory
from market_agents.prospects import (
    analyze_prospect_text,
    estimate_tooling_budget,
    detect_vertical,
    detect_equipment,
    detect_tool_suppliers,
    extract_stakeholders,
    ProspectRegistry,
)
from market_agents.tools import ToolRegistry


SAMPLE = """
Acme Aerospace Sp. z o.o. — produkujemy wały turbinowe i obudowy z tytanu (titanium).
Park maszynowy: DMG MORI NLX 2500, Mazak Integrex, Hermle C42.
Obróbka: 5-axis milling, turning, deep hole drilling. Materiały: Inconel, titanium HRC.
Używamy narzędzi Sandvik Coromant oraz Kennametal; częściowo Hoffmann Group.
Revenue of EUR 48 million in 2024. Employees: 220.
Jan Kowalski, Purchasing Manager
Anna Nowak, Production Director
Piotr Wiśniewski — Chief Process Engineer
"""


def test_detect_vertical_and_equipment():
    vert, conf = detect_vertical(SAMPLE)
    assert vert == "aerospace"
    assert conf > 0
    eq = detect_equipment(SAMPLE)
    brands = {e["brand"].lower() for e in eq}
    assert any("dmg" in b for b in brands)
    assert any(e["quality_tier"] == "premium" for e in eq)
    suppliers = detect_tool_suppliers(SAMPLE)
    assert any("sandvik" in s["brand"].lower() for s in suppliers)


def test_budget_4_to_10_pct():
    b = estimate_tooling_budget(vertical="automotive", revenue_eur=10_000_000)
    assert b["annual_tooling_budget_eur"] is not None
    mid = b["annual_tooling_budget_eur"]["mid"]
    # 10M * 0.65 * ~0.06 ≈ 390k
    assert 200_000 <= mid <= 700_000
    assert b["tooling_pct_of_production_cost"]["min"] == 0.04
    assert b["tooling_pct_of_production_cost"]["max"] == 0.08


def test_analyze_prospect_text_full():
    analysis = analyze_prospect_text(SAMPLE, company="Acme Aerospace", website="https://acme.example")
    assert analysis["vertical"] == "aerospace"
    assert analysis["quality_tier"] in {"premium", "mixed"}
    assert analysis["financial"]["budget"]["annual_tooling_budget_eur"]["mid"] > 0
    assert analysis["buys_from"]
    assert "milling" in analysis["product_map"]["likely_processes"] or "turning" in analysis[
        "product_map"
    ]["likely_processes"]
    assert "iso-s" in analysis["product_map"]["likely_materials"]
    assert analysis["stakeholders"]
    assert analysis["opportunity"]["overall"] > 40
    people_roles = {s["role"] for s in analysis["stakeholders"]}
    assert "purchasing" in people_roles or "production" in people_roles


def test_prospect_registry_persist(tmp_path: Path):
    pr = ProspectRegistry.load(tmp_path)
    analysis = analyze_prospect_text(SAMPLE, company="Acme Aerospace", website="https://acme.example")
    profile = pr.upsert_from_analysis("Acme Aerospace", analysis, tmp_path)
    assert "prospect" in profile.roles
    assert profile.prospect.get("opportunity")
    board = pr.scoreboard(limit=5)
    assert board and board[0]["company"] == "Acme Aerospace"
    assert (tmp_path / "knowledge" / "firm_profiles.json").is_file()


def test_tools_analyze_prospect_from_text(tmp_path: Path):
    cfg = AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cutting"]),
        sources=SourcesConfig(),
        agents=AgentsConfig(data_dir=str(tmp_path)),
    )
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    out = tools.call(
        "analyze_prospect",
        {"company": "Acme Aerospace", "text": SAMPLE, "persist": True},
    )
    assert out["ok"] is True
    assert out["prospect"]["vertical"] == "aerospace"
    listed = tools.call("list_prospects", {})
    assert listed["count"] >= 1
    budget = tools.call(
        "estimate_tooling_budget",
        {"vertical": "mold_die", "revenue_eur": 5_000_000},
    )
    assert budget["ok"] is True
    assert budget["budget"]["annual_tooling_budget_eur"]["min"] > 0


def test_extract_stakeholders_influence():
    people = extract_stakeholders("Maria Zielińska, Purchasing Manager leads all tooling buys.")
    assert people
    assert people[0]["influence_on_purchase"] == "high"
