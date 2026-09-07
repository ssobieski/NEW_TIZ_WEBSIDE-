"""Customer tooling inference: product → process → tools; equipment → tools; peers."""

from __future__ import annotations

from pathlib import Path

from market_agents.prospects import analyze_prospect_text
from market_agents.tooling_inference import (
    apply_peer_rules,
    detect_equipment_categories,
    detect_product_families,
    infer_customer_tooling,
    load_tooling_rules,
)


MOLD_SAMPLE = """
ToolTech Forms Sp. z o.o. — produkujemy formy wtryskowe (injection molds) do automotive.
Park maszynowy: Hermle C42 5-axis, Sodick EDM, DMG MORI NLX.
Materiały: hartowane stale narzędziowe HRC. HSM finishing i deep hole drilling.
"""

AERO_SAMPLE = """
Acme Aerospace manufactures titanium turbine housings and Inconel blades.
We run 5-axis machining centers and CNC lathes. Cutting tools from Sandvik Coromant.
"""


def test_rules_file_loads():
    rules = load_tooling_rules()
    assert rules.get("product_families")
    assert "injection_molds" in rules["product_families"]
    assert rules.get("peer_rules")


def test_mold_family_infers_process_and_tools():
    fams = detect_product_families(MOLD_SAMPLE)
    assert any(f["id"] == "injection_molds" for f in fams)
    out = infer_customer_tooling(MOLD_SAMPLE, vertical="mold_die")
    assert "hsm_finishing" in out["theoretical_process"] or any(
        "hsm" in p for p in out["theoretical_process"]
    )
    tools = set(out["practical_tools"])
    assert "solid_carbide_end_mills" in tools or "ball_nose_end_mills" in tools
    assert out["workholding_fixturing"]
    assert any(r.get("rule_id") == "mold_peers_share_tooling" for r in out["peer_rules_applied"])
    cats = {c["category"] for c in out["equipment_categories"]}
    assert "5_axis" in cats or "cnc_mill" in cats or "edm" in cats


def test_equipment_categories_from_text():
    cats = detect_equipment_categories("Centrum obróbcze CNC i tokarka CNC, wire EDM.")
    ids = {c["category"] for c in cats}
    assert "cnc_mill" in ids
    assert "cnc_lathe" in ids
    assert "edm" in ids


def test_peer_rule_vertical_only():
    peers = apply_peer_rules(product_families=[], vertical="aerospace")
    assert any(p.get("rule_id") == "aerospace_iso_s_tooling" for p in peers)


def test_analyze_prospect_includes_tooling_inference():
    analysis = analyze_prospect_text(MOLD_SAMPLE, company="ToolTech Forms")
    assert analysis["vertical"] in {"mold_die", "automotive", "general_machining"}
    pm = analysis["product_map"]
    assert pm.get("product_families")
    assert pm.get("practical_tools")
    assert pm.get("theoretical_process")
    assert "tooling_inference" in (analysis.get("opportunity") or {}) or any(
        d.get("dimension") == "tooling_inference"
        for d in (analysis.get("opportunity") or {}).get("table") or []
    )
    ti = analysis.get("tooling_inference") or {}
    assert ti.get("practical_tools")
    assert ti.get("inference_confidence", 0) > 0.4


def test_aerospace_peer_tooling():
    analysis = analyze_prospect_text(AERO_SAMPLE, company="Acme Aerospace")
    tools = set((analysis.get("product_map") or {}).get("practical_tools") or [])
    assert tools
    # ISO-S oriented tooling from aerospace family / peers
    assert any("iso_s" in t or "end_mills" in t or "hsm" in t for t in tools)


def test_new_product_families_castings_hydraulics():
    cast = infer_customer_tooling(
        "Obrabiamy odlewy żeliwne — face milling i boring korpusów.",
        vertical="general_machining",
    )
    assert any(f["id"] == "castings_machining" for f in cast["product_families"])
    hyd = infer_customer_tooling(
        "Produkcja bloków hydraulicznych (manifold) z deep hole drilling.",
        vertical="general_machining",
    )
    assert any(f["id"] == "hydraulics_pneumatics" for f in hyd["product_families"])
    assert "deep_hole_drills" in hyd["practical_tools"] or "gun_drills" in hyd["practical_tools"]


def test_crm_dedupe_tender_and_prospect(tmp_path: Path):
    from market_agents.crm_tasks import CrmTaskStore

    store = CrmTaskStore.load(tmp_path)
    t1, c1 = store.create(
        company="Beta Precision CNC",
        title="Przetarg ogłoszony: tokarka",
        reason="announced_tender",
        opportunity_score=85,
        source="tender",
        dedupe_by_company=True,
        meta={"tender": {"tender_id": "abc"}},
    )
    assert c1 is True
    t2, c2 = store.create(
        company="Beta Precision CNC",
        title="Prospect outreach: Beta Precision CNC",
        reason="vertical=mold_die",
        opportunity_score=70,
        source="prospect",
        dedupe_by_company=True,
        meta={"processes": ["milling"]},
    )
    assert c2 is False
    assert t2.id == t1.id
    store.save(tmp_path)
    again = CrmTaskStore.load(tmp_path)
    assert len(again.list(status="open")) == 1
    sources = (again.tasks[0].meta or {}).get("sources") or []
    assert "tender" in sources and "prospect" in sources


def test_crm_dedupe_normalizes_legal_form(tmp_path: Path):
    from market_agents.crm_tasks import CrmTaskStore

    store = CrmTaskStore.load(tmp_path)
    t1, _ = store.create(
        company="Acme",
        title="Prospect: Acme",
        source="prospect",
        dedupe_by_company=True,
        opportunity_score=70,
        priority="medium",
    )
    t2, created = store.create(
        company="Acme Sp. z o.o.",
        title="Przetarg: tokarka",
        source="tender",
        dedupe_by_company=True,
        opportunity_score=85,
        priority="hot",
    )
    assert created is False
    assert t2.id == t1.id
    assert t2.priority == "hot"


def test_rules_load_from_other_cwd(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rules = load_tooling_rules()
    assert rules.get("product_families")
    assert "injection_molds" in rules["product_families"]


def test_citizen_brand_maps_to_lathe():
    cats = detect_equipment_categories(
        "Citizen L20 Swiss-type and Index G200.",
        brand_equipment=[
            {"brand": "Citizen", "quality_tier": "premium"},
            {"brand": "Index", "quality_tier": "premium"},
        ],
    )
    ids = {c["category"] for c in cats}
    assert "cnc_lathe" in ids


def test_dmg_nlx_maps_to_lathe():
    cats = detect_equipment_categories(
        "Park: DMG MORI NLX 2500.",
        brand_equipment=[{"brand": "DMG MORI", "quality_tier": "premium"}],
    )
    ids = {c["category"] for c in cats}
    assert "cnc_lathe" in ids


def test_peer_same_family_no_gap_fill():
    peers = apply_peer_rules(
        product_families=[{"id": "injection_molds"}],
        vertical="mold_die",
    )
    mold = next(p for p in peers if p.get("rule_id") == "mold_peers_share_tooling")
    assert mold.get("gap_fill") is False
    assert mold.get("practical_tools") == []


def test_short_keyword_false_positives():
    # bare "gear oil" / "detale usług" alone should not force gears / precision_parts
    gear_oil = detect_product_families("We stock gear oil and lubricants only.")
    assert not any(f["id"] == "gears" for f in gear_oil)
    weak = detect_product_families("detale usług biurowych bez CNC")
    assert not any(f["id"] == "precision_parts" for f in weak)
