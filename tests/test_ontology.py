from __future__ import annotations

from pathlib import Path
import tempfile

from market_agents.config import AgentsConfig, AppConfig, IndustryConfig, SourcesConfig
from market_agents.memory import MarketMemory
from market_agents.ontology import (
    MachiningOntology,
    extract_domain_context,
)
from market_agents.tools import ToolRegistry


SAMPLE = """
High-speed milling of Inconel (ISO S) on a 5-axis machining center.
Use MQL or through-tool coolant. Recommended parameters: vc, fz, ap, ae.
Turning of stainless steel (ISO M) on CNC lathe with emulsion flood coolant.
"""


def test_extract_domain_context_detects_groups():
    ctx = extract_domain_context(SAMPLE)
    assert ctx["ok"] is True
    mat_ids = {m["id"] for m in ctx["materials"]}
    assert "material:iso-s" in mat_ids
    assert "material:iso-m" in mat_ids
    proc_ids = {p["id"] for p in ctx["processes"]}
    assert "process:milling" in proc_ids
    assert "process:turning" in proc_ids
    cool_ids = {c["id"] for c in ctx["coolants"]}
    assert "coolant:mql" in cool_ids or "coolant:through-tool" in cool_ids
    assert "coolant:emulsion" in cool_ids
    mac_ids = {m["id"] for m in ctx["machines"]}
    assert "machine:machining-center" in mac_ids
    assert "machine:cnc-lathe" in mac_ids
    assert any(p["id"] == "parameter:vc" for p in ctx["parameters"])


def test_ontology_seed_and_neighborhood():
    with tempfile.TemporaryDirectory() as tmp:
        graph = MachiningOntology()
        added = graph.merge_seed("config/machining_ontology.seed.json")
        assert added > 0
        assert len(graph.nodes) >= 20
        path = graph.save(tmp)
        assert path.exists()
        loaded = MachiningOntology.load(tmp)
        nb = loaded.neighborhood("process:milling", depth=1)
        assert nb["ok"] is True
        assert nb["counts"]["edges"] >= 1
        relations = {e["relation"] for e in nb["edges"]}
        assert "used_on_machine" in relations or "suitable_for_material" in relations


def test_ingest_context_builds_edges():
    with tempfile.TemporaryDirectory() as tmp:
        graph = MachiningOntology()
        graph.merge_seed("config/machining_ontology.seed.json")
        ctx = extract_domain_context(SAMPLE)
        before = len(graph.edges)
        result = graph.ingest_context(ctx, source_url="https://example.com/hsm", origin="test")
        assert result["ok"] is True
        assert len(graph.edges) >= before
        graph.save(tmp)
        brief = graph.domain_brief()
        assert brief["node_count"] >= 20
        assert "materials" in brief


def test_ontology_tools():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = AppConfig(
            industry=IndustryConfig(name="Test", keywords=["cutting", "milling"]),
            sources=SourcesConfig(),
            agents=AgentsConfig(data_dir=tmp),
        )
        # seed into data dir
        g = MachiningOntology.load(tmp)
        g.merge_seed("config/machining_ontology.seed.json")
        g.save(tmp)
        tools = ToolRegistry(cfg, MarketMemory(Path(tmp)))
        names = {t["function"]["name"] for t in tools.openai_tools_schema()}
        assert "get_domain_context" in names
        assert "extract_domain_context" in names
        assert "list_ontology" in names
        assert "ontology_neighborhood" in names
        brief = tools.get_domain_context({})
        assert brief["ok"] is True
        assert brief["node_count"] >= 10
        extracted = tools.extract_domain_context_tool({"text": SAMPLE, "ingest": True})
        assert extracted["ok"] is True
        assert extracted.get("ingested")
        listed = tools.list_ontology({"type": "material"})
        assert listed["count"] >= 4
        nb = tools.ontology_neighborhood({"node": "process:milling"})
        assert nb["ok"] is True
