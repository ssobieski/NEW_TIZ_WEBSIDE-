from __future__ import annotations

from pathlib import Path

from market_agents.config import (
    AgenticConfig,
    AgentsConfig,
    AppConfig,
    IndustryConfig,
    load_config,
)
from market_agents.memory import MarketMemory
from market_agents.parsing_skills import ParsingSkillsStore, default_seed_skills
from market_agents.tools import ToolRegistry


def test_seed_skills_cover_core_categories():
    seeds = default_seed_skills()
    cats = {s.category for s in seeds}
    assert "exhibitor_list" in cats
    assert "article" in cats
    assert "catalog_hub" in cats
    assert "firm_extract" in cats
    assert "relation_extract" in cats


def test_learn_improve_rate_and_persist(tmp_path: Path):
    store = ParsingSkillsStore.load(data_dir=tmp_path, with_seeds=True)
    learned = store.learn(
        name="CTE article body",
        category="article",
        preferred_method="css",
        css_selectors=["article.story"],
        applies_to_hosts=["ctemag.com"],
        applies_to_kinds=["magazine"],
        taught_by="agent-a",
    )
    assert learned["ok"] is True
    skill_id = learned["skill"]["id"]

    improved = store.improve(
        skill_id,
        css_selectors=[".article-body"],
        hints=["drop paywall"],
        taught_by="agent-b",
    )
    assert improved["ok"] is True
    assert improved["skill"]["version"] == 2
    assert "drop paywall" in improved["skill"]["hints"]
    assert improved["skill"]["taught_by"] == "agent-b"

    rated = store.rate(skill_id, quality_0_to_1=0.85, taught_by="agent-c")
    assert rated["ok"] is True
    assert rated["skill"]["success_count"] == 1

    path = store.save(tmp_path)
    assert path.exists()
    loaded = ParsingSkillsStore.load(data_dir=tmp_path, with_seeds=False)
    skill = loaded.get(skill_id)
    assert skill is not None
    assert skill.version == 2
    assert skill.success_count == 1
    assert "ctemag.com" in skill.applies_to_hosts


def test_match_skills_for_trade_fair_url():
    store = ParsingSkillsStore.load(data_dir="/tmp/no-such", with_seeds=True)
    matches = store.match(url="https://emo-hannover.com/exhibitors", kind="trade_fair")
    assert matches
    assert any(m["id"] == "fair-exhibitor-list" for m in matches)


def test_promote_host_rule_creates_shared_skill(tmp_path: Path):
    store = ParsingSkillsStore()
    out = store.promote_host_rule(
        "https://www.mmsonline.com/articles/x",
        preferred_method="trafilatura",
        css_selector="article",
        category="article",
        taught_by="collector",
    )
    assert out["ok"] is True
    assert out["skill"]["id"].startswith("host-")
    assert "mmsonline.com" in out["skill"]["applies_to_hosts"]
    store.save(tmp_path)


def test_tiz_config_enables_shared_skills():
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    assert cfg.agents.agentic.learn_parsing_skills is True
    assert cfg.agents.agentic.auto_promote_host_skills is True


def test_parsing_skill_tools_in_registry(tmp_path: Path):
    cfg = AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cutting"]),
        agents=AgentsConfig(
            data_dir=str(tmp_path),
            agentic=AgenticConfig(learn_parsing_skills=True, learn_parse_rules=True),
        ),
    )
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    names = {t["function"]["name"] for t in tools.openai_tools_schema()}
    assert {
        "list_parsing_skills",
        "match_parsing_skills",
        "learn_parsing_skill",
        "improve_parsing_skill",
        "rate_parsing_skill",
        "promote_host_skill",
    } <= names

    listed = tools.list_parsing_skills({"category": "article"})
    assert listed["ok"] is True
    assert listed["count"] >= 1

    learned = tools.learn_parsing_skill(
        {
            "name": "Shared portal news",
            "category": "article",
            "preferred_method": "css",
            "css_selectors": ["main article"],
            "applies_to_kinds": ["portal"],
            "taught_by": "agent-x",
        }
    )
    assert learned["ok"] is True
    skill_id = learned["skill"]["id"]
    improved = tools.improve_parsing_skill(
        {"skill_id": skill_id, "hints": ["prefer main article"], "taught_by": "agent-y"}
    )
    assert improved["ok"] is True
    rated = tools.rate_parsing_skill({"skill_id": skill_id, "quality_0_to_1": 0.9})
    assert rated["ok"] is True
    promoted = tools.promote_host_skill(
        {
            "host_or_url": "https://www.productionmachining.com/news/1",
            "preferred_method": "css",
            "css_selector": "article",
            "category": "article",
        }
    )
    assert promoted["ok"] is True
