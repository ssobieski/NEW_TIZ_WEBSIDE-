from __future__ import annotations

import json
from pathlib import Path

from market_agents.collectors.notion import _firm_from_page
from market_agents.config import (
    AgenticConfig,
    AgentsConfig,
    AppConfig,
    IndustryConfig,
)
from market_agents.memory import MarketMemory
from market_agents.tools import ToolRegistry


def test_firm_from_page_reads_presentation_property():
    page = {
        "id": "abc",
        "url": "https://www.notion.so/abc",
        "properties": {
            "Firma": {
                "type": "title",
                "title": [{"plain_text": "Sandvik Coromant"}],
            },
            "Kraj": {"type": "select", "select": {"name": "SE"}},
            "Przedstawienie": {
                "type": "rich_text",
                "rich_text": [
                    {
                        "plain_text": "OEM narzędzi skrawających, grupa Sandvik."
                    }
                ],
            },
            "Fokus produktowy": {
                "type": "rich_text",
                "rich_text": [{"plain_text": "inserts / turning"}],
            },
        },
    }
    firm = _firm_from_page(page)
    assert firm["company"] == "Sandvik Coromant"
    assert firm["country"] == "SE"
    assert "OEM" in firm["presentation"]
    assert firm["presentation_source"] == "notion_property"


def test_get_firm_presentation_tool_uses_notion_catalog(tmp_path: Path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "known_firms.json").write_text(
        json.dumps(
            [
                {
                    "company": "Iscar",
                    "country": "IL",
                    "product_focus": "carbide tooling",
                    "presentation": "Iscar należy do IMC Group — sister brands Ingersoll, TaeguTec.",
                    "presentation_source": "notion_property",
                    "notion_url": "https://www.notion.so/iscar",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg = AppConfig(
        industry=IndustryConfig(name="TIZ", keywords=["cutting"]),
        agents=AgentsConfig(
            data_dir=str(tmp_path),
            agentic=AgenticConfig(),
        ),
    )
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    names = {t["function"]["name"] for t in tools.openai_tools_schema()}
    assert "get_firm_presentation" in names
    listed = tools.list_known_firms({"query": "iscar"})
    assert listed["ok"] is True
    assert listed["with_presentation"] == 1
    assert listed["catalog"] == "notion"
    profile = tools.get_firm_presentation({"company": "Iscar"})
    assert profile["ok"] is True
    assert "IMC" in profile["presentation"]
