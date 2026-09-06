from __future__ import annotations

from pathlib import Path

from market_agents.config import load_config
from market_agents.firm_relations import (
    RELATION_TYPES,
    FirmRelationsGraph,
    extract_relation_candidates,
)
from market_agents.memory import MarketMemory
from market_agents.tools import ToolRegistry


def test_seed_loads_distributor_and_brand_edges():
    graph = FirmRelationsGraph.load(
        data_dir=Path("data"),
        seed_path=Path("config/firm_relations.seed.json"),
    )
    assert len(graph.edges) >= 10
    rows = graph.list_relations(company="Hoffmann Group")
    types = {r["relation_type"] for r in rows}
    assert "brand_of" in types
    assert "distributor_of" in types
    brands = {
        r["source"]
        for r in rows
        if r["relation_type"] == "brand_of" and r["target"].startswith("Hoffmann")
    }
    assert "GARANT" in brands
    assert "HOLEX" in brands


def test_neighborhood_includes_imc_family():
    graph = FirmRelationsGraph.load(seed_path=Path("config/firm_relations.seed.json"))
    nb = graph.neighborhood("Iscar", depth=1)
    assert nb["count_edges"] >= 1
    assert any("IMC" in (e["source"] + e["target"]) for e in nb["edges"]) or any(
        e["relation_type"] in {"subsidiary_of", "oem_group"} for e in nb["edges"]
    )


def test_extract_relation_candidates_basic():
    text = (
        "Perschmann is a subsidiary of Hoffmann Group. "
        "GARANT is a brand of Hoffmann Group. "
        "HAHN+KOLB is an authorized distributor of Sandvik Coromant."
    )
    cands = extract_relation_candidates(text)
    by_type = {(c["source"], c["relation_type"], c["target"]) for c in cands}
    assert ("Perschmann", "subsidiary_of", "Hoffmann Group") in by_type
    assert ("GARANT", "brand_of", "Hoffmann Group") in by_type
    assert any(
        s == "HAHN+KOLB" and t.startswith("Sandvik") and rt == "distributor_of"
        for s, rt, t in by_type
    )


def test_add_relation_dedupes():
    g = FirmRelationsGraph()
    r1 = g.add_relation(
        "Alpha Tools", "Beta OEM", "distributor_of", evidence="test", confidence=0.9
    )
    r2 = g.add_relation(
        "Alpha Tools", "Beta OEM", "distributor_of", evidence="dup", confidence=0.5
    )
    assert r1["added"] is True
    assert r2["added"] is False
    assert len(g.edges) == 1


def test_relation_tools_in_registry(tmp_path: Path):
    cfg = load_config("config/industry.yaml")
    cfg.agents.data_dir = str(tmp_path)
    # ensure seed still found from CWD
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    names = {t["function"]["name"] for t in tools.openai_tools_schema()}
    assert "list_relations" in names
    assert "add_relation" in names
    assert "discover_relations" in names
    assert "firm_neighborhood" in names
    assert set(RELATION_TYPES).issuperset({"distributor_of", "brand_of", "oem_group"})

    listed = tools.list_relations({"company": "Hoffmann Group", "limit": 20})
    assert listed["ok"] is True
    assert listed["count"] >= 1

    discovered = tools.discover_relations(
        {
            "parse_candidates": False,
            "persist": False,
            "texts": [
                {
                    "text": "NovaCut is an authorized distributor of Kennametal.",
                    "url": "https://example.com/novacut",
                }
            ],
        }
    )
    assert discovered["ok"] is True
    assert discovered["count"] >= 1
    assert any(
        r["relation_type"] == "distributor_of" and "NovaCut" in r["source"]
        for r in discovered["relations"]
    )

    nb = tools.firm_neighborhood({"company": "Hoffmann Group", "depth": 1})
    assert nb["ok"] is True
    assert nb["count_edges"] >= 1
