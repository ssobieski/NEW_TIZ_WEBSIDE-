from __future__ import annotations

from pathlib import Path
import tempfile

from market_agents.collectors.catalog import CatalogCollector, _classify_asset
from market_agents.config import (
    AppConfig,
    CatalogSource,
    IndustryConfig,
    SourcesConfig,
    load_config,
)
from market_agents.memory import MarketMemory
from market_agents.tools import ToolRegistry


def test_tiz_config_has_catalogs():
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    assert len(cfg.sources.catalogs) >= 10
    kinds = {c.kind for c in cfg.sources.catalogs}
    assert "ecatalog" in kinds
    assert any(c.kind in {"pdf", "eshop", "digital_catalogue"} for c in cfg.sources.catalogs)
    brands = " ".join(c.brand or "" for c in cfg.sources.catalogs)
    assert "Iscar" in brands
    assert "Sandvik" in brands


def test_classify_asset_kinds():
    assert _classify_asset("https://x.com/file.pdf", "Main catalog") == "pdf"
    assert _classify_asset("https://x.com/ecatalog/", "eCatalog") == "ecatalog"
    assert _classify_asset("https://x.com/webshop", "Buy online") == "eshop"
    assert _classify_asset("https://x.com/handbook", "Machining handbook") == "publication"
    assert _classify_asset("https://x.com/downloads/catalogues", "Catalogues") == "digital_catalogue"
    assert _classify_asset("https://x.com/preisliste.pdf", "Preisliste") == "pricelist"
    assert _classify_asset("https://x.com/cennik", "Cennik 2025") == "pricelist"


def test_catalog_collector_builds_pdf_item_on_error():
    src = CatalogSource(
        name="Test PDF",
        brand="TestBrand",
        kind="pdf",
        url="https://127.0.0.1:9/no-such-catalog.pdf",
        extract_text=False,
    )
    items = CatalogCollector(src).collect(max_items=5)
    assert len(items) == 1
    assert items[0].url.endswith(".pdf")
    assert "pdf" in items[0].tags or "error" in items[0].tags


def test_tool_registry_has_catalog_tools():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = AppConfig(
            industry=IndustryConfig(name="Test", keywords=["catalog"]),
            sources=SourcesConfig(
                catalogs=[
                    CatalogSource(
                        name="Demo",
                        brand="DemoCo",
                        kind="ecatalog",
                        url="https://example.com/ecatalog",
                    )
                ]
            ),
        )
        tools = ToolRegistry(cfg, MarketMemory(Path(tmp)))
        names = {t["function"]["name"] for t in tools.openai_tools_schema()}
        assert "list_catalog_sources" in names
        assert "discover_catalog_assets" in names
        assert "fetch_pdf_text" in names
        listed = tools.list_catalog_sources({})
        assert listed["ok"] is True
        assert listed["count"] == 1
