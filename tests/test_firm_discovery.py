from __future__ import annotations

from pathlib import Path
import tempfile

from market_agents.config import AppConfig, IndustryConfig, load_config
from market_agents.firms import (
    KnownFirmsIndex,
    extract_candidate_firm_names,
    filter_new_firms,
    is_plausible_firm_name,
    is_tooling_relevant,
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


def test_extract_rejects_headline_verbs_and_off_domain():
    assert "Secures" not in extract_candidate_firm_names(
        "Diamond-Tech Startup AKHAN Secures $20M"
    )
    assert "Promote" not in extract_candidate_firm_names(
        "Haas F1 Team to Promote HaasTooling.com European Debut"
    )
    assert not is_tooling_relevant(
        "Groupe Chaumont launches with acquisition of two Swiss watchmaking startups"
    )
    assert not is_tooling_relevant("Bret Taylor Predicts A.I. Agents Will Redefine")
    assert not is_plausible_firm_name("Secures")
    assert not is_plausible_firm_name("Kanematsu Invests")
    assert is_plausible_firm_name("BladeForge Inc")


def test_tooling_relevant_gate():
    assert is_tooling_relevant("MSC introduces new brand of cutting tools")
    assert is_tooling_relevant("OrbitalEdge GmbH solid carbide mills catalog")
    assert is_tooling_relevant("DIAEDGE, A New Brand of Cemented Carbide Products")
    assert not is_tooling_relevant("Luxury watch platform Groupe Chaumont")
    assert not is_tooling_relevant("Haas F1 Team European Debut")
    assert not is_tooling_relevant(
        "PHOTOS: Mississauga introduces brand new grass cutting tools"
    )
    assert not is_tooling_relevant("Berry Introduces Global Tooling Services")
    assert not is_tooling_relevant("Tooling manufacturer celebrates 50 years")
    assert not is_plausible_firm_name("Secures")
    assert not is_plausible_firm_name("Kanematsu Invests")
    assert not is_plausible_firm_name("Dry-cut Hobbing Machine")
    assert not is_plausible_firm_name("Key Acquisitions")
    assert not is_plausible_firm_name("Hypepotamus")
    assert not is_plausible_firm_name("PHOTOS")
    assert not is_plausible_firm_name("FMT")
    assert not is_plausible_firm_name("Leverages")
    assert not is_plausible_firm_name("How")
    assert not is_plausible_firm_name("Enterprise Manufacturers")
    assert is_plausible_firm_name("DIAEDGE")
    assert is_plausible_firm_name("BladeForge Inc")
    assert is_plausible_firm_name("Manar Tools")


def test_extract_real_tiz_headlines():
    names = extract_candidate_firm_names(
        "DIAEDGE, A New Brand of Cemented Carbide Products"
    )
    assert any(n.upper() == "DIAEDGE" for n in names)
    names2 = extract_candidate_firm_names(
        "Walter FMT: a new brand for lightweight machining"
    )
    assert any("Walter" in n for n in names2)
    grass = "PHOTOS: Mississauga introduces brand new grass cutting tools"
    assert not is_tooling_relevant(grass)
    bad = extract_candidate_firm_names(
        "Startup Leverages Machine Tool Builder Expertise - Modern Machine Shop"
    )
    assert "Leverages" not in bad
    how = extract_candidate_firm_names(
        "How elastic bonded diamond tools optimise precision machining - PES Media"
    )
    assert "How" not in how
    ent = extract_candidate_firm_names(
        "Enterprise Manufacturers Cut CNC Programming Time by Up to 50% with Limitless CAM Agent"
    )
    assert "Enterprise Manufacturers" not in ent
    assert "Increasing Production Capacity" not in extract_candidate_firm_names(
        "Nidec Machine Tool to Launch New Cutting Tool Factory in India to Meet "
        "Growing Demand for Automotive and Related Components by Increasing Production Capacity by 1.5 Times"
    )
    assert "Four" not in extract_candidate_firm_names(
        "Four of the Nidec Group's Machine Tool Companies to Exhibit Products at IMTS 2024"
    )
    assert "UNLOCKED" not in extract_candidate_firm_names(
        "PRECISION UNLOCKED | TaeguTec India Launch Cutting Tool Knowledge Series"
    )
    assert any(
        "TaeguTec" in n
        for n in extract_candidate_firm_names(
            "PRECISION UNLOCKED | TaeguTec India Launch Cutting Tool Knowledge Series"
        )
    )
    assert not is_plausible_firm_name("DLC")
    assert not is_plausible_firm_name("Baucor Expands")
    assert is_plausible_firm_name("Baucor")


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


def test_filter_require_domain_drops_off_topic():
    idx = KnownFirmsIndex([])
    rows = filter_new_firms(
        ["Groupe Chaumont"],
        idx,
        evidence="Groupe Chaumont launches Swiss watchmaking startups",
        url="https://example.com/x",
        require_domain=True,
    )
    assert rows == []


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
