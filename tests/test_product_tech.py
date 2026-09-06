from __future__ import annotations

from pathlib import Path

from market_agents.collectors.catalog import _classify_asset
from market_agents.config import (
    AgentsConfig,
    AppConfig,
    CatalogSource,
    IndustryConfig,
    SourcesConfig,
    load_config,
)
from market_agents.memory import MarketMemory
from market_agents.product_tech import (
    ProductTechRegistry,
    classify_tech_kind,
    extract_tech_schema,
)
from market_agents.tools import ToolRegistry


def test_classify_tech_kinds():
    assert classify_tech_kind("https://x.com/cutting-data.pdf", "Cutting data") == "cutting_data"
    assert classify_tech_kind("https://x.com/handbook", "Metal cutting handbook") == "handbook"
    assert classify_tech_kind("https://x.com/iso-13399", "ISO 13399 CAD") == "iso13399"
    assert _classify_asset("https://x.com/cutting-data.pdf", "Cutting data P20") == "cutting_data"
    assert _classify_asset("https://x.com/preisliste.pdf", "Preisliste") == "pricelist"


def test_extract_tech_schema_fields():
    text = """
    1. Turning
    Recommended cutting speed vc and feed per tooth fz.
    Depth of cut ap for ISO material group P20 with coolant.
    """
    schema = extract_tech_schema(text)
    assert schema["ok"] is True
    assert "vc" in schema["fields_present"]
    assert "fz" in schema["fields_present"]
    assert schema["has_coolant_info"] is True
    assert "schema_only" in schema["purpose"]


def test_product_tech_registry_roundtrip(tmp_path: Path):
    reg = ProductTechRegistry()
    reg.upsert(
        url="https://seco.example/cutting-data.pdf",
        title="Seco cutting data",
        brand="Seco",
        kind="cutting_data",
        access="public",
        schema_summary={"fields_present": ["vc", "fz"]},
    )
    reg.ingest_discovered(
        [
            {
                "url": "https://sandvik.example/handbook.pdf",
                "text": "Metal Cutting Handbook",
                "asset_type": "handbook",
            }
        ],
        brand="Sandvik",
        source="hub",
    )
    path = reg.save(tmp_path)
    assert path.exists()
    loaded = ProductTechRegistry.load(tmp_path)
    assert len(loaded.items) == 2
    assert loaded.list(kind="cutting_data")[0].brand == "Seco"


def test_tiz_config_tracks_product_tech():
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    assert any("cutting data" in k.lower() or k.lower() == "cutting data" for k in cfg.industry.keywords) or any(
        "handbook" in k.lower() for k in cfg.industry.keywords
    )
    assert any("TECHNICZNE" in q or "cutting data" in q.lower() for q in cfg.industry.focus_questions)


def test_tools_product_tech(tmp_path: Path):
    cfg = AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cutting data", "vc"]),
        sources=SourcesConfig(
            catalogs=[
                CatalogSource(
                    name="Demo",
                    brand="DemoCo",
                    kind="handbook",
                    url="https://example.com/tech",
                )
            ]
        ),
        agents=AgentsConfig(data_dir=str(tmp_path)),
    )
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    names = {t["function"]["name"] for t in tools.openai_tools_schema()}
    assert "discover_product_tech" in names
    assert "extract_product_tech_schema" in names
    registered = tools.register_product_tech(
        {
            "url": "https://example.com/cutting-data-2026.pdf",
            "title": "Cutting data 2026",
            "brand": "DemoCo",
            "kind": "cutting_data",
            "access": "public",
        }
    )
    assert registered["ok"] is True
    schema = tools.extract_product_tech_schema(
        {
            "text": "Milling: vc and fz for P20 with emulsion coolant.",
            "url": "https://example.com/cutting-data-2026.pdf",
            "brand": "DemoCo",
        }
    )
    assert schema["ok"] is True
    assert "vc" in schema["schema"]["fields_present"]
    listed = tools.list_product_tech({"brand": "DemoCo"})
    assert listed["count"] >= 1
