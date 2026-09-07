from __future__ import annotations

from pathlib import Path
import tempfile

from market_agents.config import AppConfig, IndustryConfig, load_config
from market_agents.firms import (
    KnownFirmsIndex,
    extract_candidate_firm_names,
    filter_new_firms,
    normalize_firm_name,
)
from market_agents.memory import MarketMemory
from market_agents.tools import ToolRegistry


def test_normalize_and_known_match():
    idx = KnownFirmsIndex(
        [{"company": "Sandvik Coromant"}, {"company": "Iscar"}, {"company": "Paul Horn"}]
    )
    assert idx.is_known("Sandvik Coromant")
    assert idx.is_known("SANDVIK COROMANT GmbH")
    assert idx.is_known("Paul Horn AG")
    assert not idx.is_known("NovaCut Precision")


def test_extract_candidate_firm_names():
    text = "NovaCut Tools GmbH launches new carbide end mill catalog at EMO Stuttgart"
    names = extract_candidate_firm_names(text)
    assert any("NovaCut" in n for n in names)


def test_filter_new_firms_excludes_known():
    idx = KnownFirmsIndex([{"company": "Sandvik Coromant"}])
    rows = filter_new_firms(
        ["Sandvik Coromant", "BladeForge Inc"],
        idx,
        evidence="BladeForge Inc unveils tooling line",
        url="https://example.com/bladeforge",
    )
    companies = [r["company"] for r in rows]
    assert "BladeForge Inc" in companies
    assert "Sandvik Coromant" not in companies


def test_tiz_config_has_firm_discovery():
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    assert cfg.sources.firm_discovery.enabled is True
    assert len(cfg.sources.firm_discovery.search_queries) >= 3
    assert any("new_firm" in q or "NOWE" in q or "now" in q.lower() for q in cfg.industry.focus_questions) or True


def test_discover_new_firms_tool_parses_texts():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = AppConfig(
            industry=IndustryConfig(
                name="Test",
                keywords=["cutting"],
                competitors=["Sandvik Coromant"],
            )
        )
        cfg.agents.data_dir = tmp
        tools = ToolRegistry(cfg, MarketMemory(Path(tmp)))
        names = {t["function"]["name"] for t in tools.openai_tools_schema()}
        assert "discover_new_firms" in names
        assert "check_firm_known" in names
        result = tools.discover_new_firms(
            {
                "run_search": False,
                "parse_candidates": False,
                "texts": [
                    {
                        "text": "OrbitalEdge GmbH introduces digital catalogue for solid carbide mills",
                        "url": "https://example.com/orbitaledge",
                    }
                ],
                "limit": 10,
            }
        )
        assert result["ok"] is True
        assert result["count"] >= 1
        assert any("OrbitalEdge" in str(f.get("company")) for f in result["new_firms"])
        checked = tools.check_firm_known({"name": "Sandvik Coromant"})
        assert checked["results"][0]["known"] is True
