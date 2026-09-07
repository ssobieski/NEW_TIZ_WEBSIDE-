from __future__ import annotations

from pathlib import Path

from market_agents.collectors.catalog import _classify_asset
from market_agents.config import (
    AgentsConfig,
    AppConfig,
    CatalogSource,
    IndustryConfig,
    SourcesConfig,
)
from market_agents.memory import MarketMemory
from market_agents.pricelists import PricelistRegistry
from market_agents.tools import ToolRegistry


def test_classify_pricelist_before_pdf():
    assert _classify_asset("https://x.com/preisliste-2025.pdf", "Preisliste 2025") == "pricelist"
    assert _classify_asset("https://x.com/cennik", "Cennik narzędzi") == "pricelist"
    assert _classify_asset("https://x.com/downloads/price-list.pdf", "Price list") == "pricelist"
    assert _classify_asset("https://x.com/file.pdf", "Main catalog") == "pdf"


def test_pricelist_registry_roundtrip(tmp_path: Path):
    reg = PricelistRegistry()
    reg.upsert(
        url="https://oem.example/cennik.pdf",
        title="Cennik OEM",
        brand="OEM",
        access="public",
        year="2026",
    )
    reg.ingest_discovered(
        [
            {
                "url": "https://walter.example/Preisliste.pdf",
                "text": "Preisliste Walter",
                "asset_type": "pricelist",
            }
        ],
        brand="Walter",
        source="hub",
    )
    path = reg.save(tmp_path)
    assert path.exists()
    loaded = PricelistRegistry.load(tmp_path)
    assert len(loaded.items) == 2
    assert loaded.list(brand="OEM")[0].year == "2026"
    assert loaded.list(q="preis")[0].brand == "Walter"


def test_tiz_config_tracks_pricelists():
    from market_agents.config import load_config

    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    assert "cennik" in cfg.industry.keywords
    assert any("CENNIKI" in q or "cennik" in q.lower() for q in cfg.industry.focus_questions)


def test_tools_register_and_list_pricelists(tmp_path: Path):
    cfg = AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cennik"]),
        sources=SourcesConfig(
            catalogs=[
                CatalogSource(
                    name="Demo",
                    brand="DemoCo",
                    kind="pricelist",
                    url="https://example.com/downloads",
                )
            ]
        ),
        agents=AgentsConfig(data_dir=str(tmp_path)),
    )
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    names = {t["function"]["name"] for t in tools.openai_tools_schema()}
    assert "discover_pricelists" in names
    assert "list_available_pricelists" in names
    assert "register_pricelist" in names
    registered = tools.register_pricelist(
        {
            "url": "https://example.com/cennik-2026.pdf",
            "title": "Cennik 2026",
            "brand": "DemoCo",
            "access": "public",
        }
    )
    assert registered["ok"] is True
    listed = tools.list_available_pricelists({"brand": "DemoCo"})
    assert listed["count"] == 1
