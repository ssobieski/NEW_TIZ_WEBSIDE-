from __future__ import annotations

from pathlib import Path
import tempfile

from market_agents.collectors.notion import _to_notion_id
from market_agents.config import AppConfig, IndustryConfig, load_config
from market_agents.memory import MarketMemory
from market_agents.tools import ToolRegistry


def test_tiz_config_loads_notion_and_r2():
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    assert "skraw" in cfg.industry.name.lower() or "TIZ" in cfg.industry.name
    assert cfg.sources.notion.enabled is True
    assert len(cfg.sources.notion.root_pages) >= 4
    assert "Sandvik" in " ".join(cfg.industry.competitors)
    assert cfg.sources.cloudflare_r2.bucket
    assert cfg.llm.tensor_parallel_size == 4


def test_notion_id_from_url():
    url = "https://www.notion.so/3c2bdd41cccd8176bf12f6efd37baacc"
    assert _to_notion_id(url) == "3c2bdd41-cccd-8176-bf12-f6efd37baacc"
    assert (
        _to_notion_id("3c2bdd41-cccd-8176-bf12-f6efd37baacc")
        == "3c2bdd41-cccd-8176-bf12-f6efd37baacc"
    )


def test_tool_registry_has_notion_r2_tools():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = AppConfig(industry=IndustryConfig(name="Test", keywords=["tooling"]))
        tools = ToolRegistry(cfg, MarketMemory(Path(tmp)))
        names = {t["function"]["name"] for t in tools.openai_tools_schema()}
        assert "search_notion" in names
        assert "fetch_notion_page" in names
        assert "search_r2" in names
        assert "fetch_r2_object" in names
        assert "list_catalog_sources" in names
        assert "discover_catalog_assets" in names
        assert "fetch_pdf_text" in names
