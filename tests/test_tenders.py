"""Tender / purchase-signal parsing for CNC and tooling investments."""

from __future__ import annotations

from pathlib import Path

from market_agents.config import AgentsConfig, AppConfig, IndustryConfig, SourcesConfig
from market_agents.crm_tasks import CrmTaskStore
from market_agents.firm_profiles import FirmProfileRegistry
from market_agents.memory import MarketMemory
from market_agents.tenders import (
    detect_intent,
    extract_equipment,
    ingest_tender_analysis,
    parse_tender_text,
    TenderRegistry,
)
from market_agents.tools import ToolRegistry


SAMPLE_ANNOUNCED = """
Firma Beta Precision CNC Sp. z o.o. ogłosiła przetarg na dostawę centrum obróbczego CNC
5-osiowego oraz tokarki CNC. Szacunkowa wartość: 2,5 mln PLN. Termin składania ofert:
15.10.2026. CPV: 42600000-1. Nr postępowania: BZP/2026/CNC-17.
"""

SAMPLE_PURCHASED = """
Acme Aerospace purchased a new 5-axis machining center and commissioned a CNC lathe
at its Polish plant. The investment strengthens titanium machining capacity.
"""

SAMPLE_PLANNED = """
Zakład Mechaniczny Gamma planuje ogłosić przetarg na frezarkę CNC
w ramach inwestycji w park maszynowy.
"""


def test_detect_intents_and_equipment():
    intent, conf, _ = detect_intent(SAMPLE_ANNOUNCED)
    assert intent == "announced_tender"
    assert conf >= 0.8
    eq = extract_equipment(SAMPLE_ANNOUNCED)
    kinds = {e["kind"] for e in eq}
    assert "cnc_mill" in kinds or "cnc_generic" in kinds or "5_axis" in kinds
    assert "cnc_lathe" in kinds

    intent2, _, _ = detect_intent(SAMPLE_PURCHASED)
    assert intent2 == "purchased"

    intent3, _, _ = detect_intent(SAMPLE_PLANNED)
    assert intent3 == "planned_tender"


def test_parse_tender_text_full():
    out = parse_tender_text(SAMPLE_ANNOUNCED, title="Przetarg CNC")
    assert out["ok"] is True
    assert out["intent"] == "announced_tender"
    assert out["tender_relevant"] is True
    assert "Beta" in (out.get("company") or "") or out.get("company")
    assert out.get("value_eur") and out["value_eur"] > 100_000
    assert out.get("deadline")
    assert out.get("cpv")
    assert out.get("equipment")


def test_ingest_updates_profile_and_crm(tmp_path: Path):
    analysis = parse_tender_text(
        SAMPLE_ANNOUNCED, company="Beta Precision CNC", title="Przetarg CNC"
    )
    result = ingest_tender_analysis(analysis, tmp_path)
    assert result["ok"] is True
    assert (tmp_path / "knowledge" / "tenders.json").is_file()

    reg = TenderRegistry.load(tmp_path)
    assert reg.list()
    assert reg.scoreboard()

    profiles = FirmProfileRegistry.load(tmp_path)
    p = profiles.get("Beta Precision CNC")
    assert p is not None
    assert "prospect" in (p.roles or [])
    assert (p.prospect or {}).get("tenders")

    crm = CrmTaskStore.load(tmp_path)
    assert any(t.source == "tender" for t in crm.tasks)


def test_następnie_not_announced_tender():
    """`na` inside `następnie` must not flip purchase news to announced_tender."""
    text = (
        "Acme Aerospace kupiła tokarkę CNC, a następnie zainstalowała centrum obróbcze."
    )
    intent, _, _ = detect_intent(text)
    assert intent != "announced_tender"
    assert intent == "purchased"


def test_upsert_preserves_nonempty_fields(tmp_path: Path):
    from market_agents.tenders import TenderSignal

    reg = TenderRegistry.load(tmp_path)
    full = TenderSignal(
        id="abc123def456",
        company="Beta Precision CNC",
        intent="announced_tender",
        equipment=[{"kind": "cnc_mill", "label": "centrum"}],
        title="Przetarg CNC",
        value_eur=1000.0,
        deadline="15.10.2026",
        evidence="ogłoszono przetarg",
        url="https://example.com/tender",
        confidence=0.9,
    )
    reg.upsert(full)
    weak = TenderSignal(
        id="abc123def456",
        company="Beta Precision CNC",
        intent="announced_tender",
        equipment=[],
        title="Przetarg CNC",
        value_eur=None,
        deadline="",
        evidence="",
        url="https://example.com/tender",
        confidence=0.5,
    )
    merged = reg.upsert(weak)
    assert merged.value_eur == 1000.0
    assert merged.deadline == "15.10.2026"
    assert merged.evidence == "ogłoszono przetarg"
    assert merged.confidence == 0.9
    assert merged.equipment


def test_software_buy_not_ingested(tmp_path: Path):
    analysis = parse_tender_text(
        "Acme Corp installed new ERP software and bought cloud licenses.",
        company="Acme Corp",
    )
    assert analysis.get("tender_relevant") is False
    result = ingest_tender_analysis(analysis, tmp_path)
    assert result.get("ok") is False


def test_equipment_only_without_intent_not_relevant():
    analysis = parse_tender_text(
        "Firma Delta używa oprawek HAIMER i toolholders w warsztacie.",
        company="Delta",
    )
    assert analysis.get("intent") == "other"
    assert analysis.get("tender_relevant") is False


def test_explicit_company_wins():
    analysis = parse_tender_text(
        SAMPLE_ANNOUNCED,
        company="Operator Override Sp. z o.o.",
    )
    assert "Override" in (analysis.get("company") or "")


def test_tools_parse_and_list_tenders(tmp_path: Path):
    cfg = AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cnc", "przetarg"]),
        sources=SourcesConfig(),
        agents=AgentsConfig(data_dir=str(tmp_path)),
    )
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    names = set(tools._handlers)
    assert {"parse_tender", "ingest_tender", "list_tenders", "tender_scoreboard"} <= names

    parsed = tools.call(
        "parse_tender",
        {"text": SAMPLE_ANNOUNCED, "company": "Beta Precision CNC", "persist": False},
    )
    assert parsed.get("intent") == "announced_tender"

    ingested = tools.call(
        "ingest_tender",
        {"text": SAMPLE_ANNOUNCED, "company": "Beta Precision CNC", "title": "Tender"},
    )
    assert ingested.get("ok") is True
    listed = tools.call("list_tenders", {"limit": 10})
    assert listed.get("count", 0) >= 1
    board = tools.call("tender_scoreboard", {"limit": 5})
    assert board.get("scoreboard")
