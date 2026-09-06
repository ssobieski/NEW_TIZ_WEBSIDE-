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
from market_agents.parse_rules import SiteParseRulesStore, host_from_url
from market_agents.parsing import IntelligentParser
from market_agents.tools import ToolRegistry


SAMPLE_HTML = """
<html><head><title>NovaCut launches carbide mill</title></head>
<body>
<nav>Home About</nav>
<div class="noise">Cookie banner subscribe newsletter</div>
<article class="story-body">
<h1>NovaCut launches carbide mill</h1>
<p>NovaCut Tools GmbH today unveiled a new solid carbide end mill family
aimed at aerospace aluminium machining and high-feed roughing applications.</p>
<p>The company said distributors in DACH and Poland will stock the line
from Q4, with digital catalogue pages published alongside the launch.</p>
<p>Application engineers highlighted tool life gains of roughly fifteen percent
versus the previous generation in wet machining trials.</p>
</article>
<footer>Privacy cookies</footer>
</body></html>
"""


def test_host_from_url_strips_www():
    assert host_from_url("https://www.Example.com/a") == "example.com"


def test_css_rule_preferred_over_default(tmp_path: Path):
    store = SiteParseRulesStore()
    store.upsert(
        "https://news.example.com/x",
        preferred_method="css",
        css_selector="article.story-body",
        origin="manual",
    )
    parser = IntelligentParser(rules=store, learn=False)
    doc = parser.parse_html("https://news.example.com/x", SAMPLE_HTML)
    assert doc.ok
    assert doc.parse_method == "css"
    assert "NovaCut" in doc.text
    assert "Cookie banner" not in doc.text


def test_rate_and_persist_roundtrip(tmp_path: Path):
    store = SiteParseRulesStore()
    store.upsert("sandvik.coromant.com", preferred_method="trafilatura")
    store.rate(
        "https://www.sandvik.coromant.com/news/1",
        quality_0_to_1=0.9,
        parse_method="trafilatura",
    )
    path = store.save(tmp_path)
    assert path.exists()
    loaded = SiteParseRulesStore.load(tmp_path)
    rule = loaded.get("sandvik.coromant.com")
    assert rule is not None
    assert rule.success_count == 1
    assert rule.preferred_method == "trafilatura"


def test_agentic_config_has_learn_parse_rules():
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    assert cfg.agents.agentic.learn_parse_rules is True
    assert "site_parse_rules" in cfg.agents.agentic.parse_rules_path


def test_parse_rule_tools_in_registry(tmp_path: Path):
    cfg = AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cutting"]),
        agents=AgentsConfig(
            data_dir=str(tmp_path),
            agentic=AgenticConfig(learn_parse_rules=True),
        ),
    )
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    names = {t["function"]["name"] for t in tools.openai_tools_schema()}
    assert {"list_parse_rules", "upsert_parse_rule", "rate_parse"} <= names

    out = tools.upsert_parse_rule(
        {
            "host_or_url": "https://www.hoffmann-group.com/catalog",
            "preferred_method": "css",
            "css_selector": "div.product-description",
            "notes": "Hoffmann product pages",
        }
    )
    assert out["ok"] is True
    listed = tools.list_parse_rules({"query": "hoffmann"})
    assert listed["count"] >= 1
    rated = tools.rate_parse(
        {
            "url": "https://www.hoffmann-group.com/catalog/x",
            "quality_0_to_1": 0.2,
            "notes": "too short",
        }
    )
    assert rated["ok"] is True
    assert rated["rule"]["fail_count"] >= 1
