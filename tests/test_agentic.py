from __future__ import annotations

from market_agents.parsing import IntelligentParser


SAMPLE_HTML = """
<html><head>
<title>Konkurent X obniża ceny o 12% w Q3</title>
<meta property="og:title" content="Konkurent X obniża ceny o 12% w Q3" />
</head><body>
<nav>Menu Home Kontakt</nav>
<article>
<h1>Konkurent X obniża ceny o 12% w Q3</h1>
<p>Producent mebli Konkurent X ogłosił obniżkę cen katalogowych o 12 procent
w trzecim kwartale. Ruch dotyczy segmentu e-commerce i salonów stacjonarnych.</p>
<p>Analitycy oceniają, że to odpowiedź na rosnącą konkurencję i spadek popytu
w segmencie premium. Firma planuje też otwarcie trzech nowych magazynów.</p>
<p>Marża brutto może spaść o 1,5 pp w krótkim terminie, ale wolumen ma wzrosnąć.</p>
</article>
<footer>Cookies privacy</footer>
</body></html>
"""


def test_intelligent_parser_extracts_article():
    parser = IntelligentParser()
    doc = parser.parse_html("https://example.com/news/x", SAMPLE_HTML)
    assert doc.ok
    assert "12" in doc.text or "12" in doc.title
    assert "Konkurent X" in doc.title or "Konkurent X" in doc.text
    assert len(doc.text) > 100
    assert doc.parse_method in {"trafilatura", "bs4"}


def test_tool_registry_extract_intel(tmp_path):
    from market_agents.config import (
        AppConfig,
        IndustryConfig,
        AgentsConfig,
        AgenticConfig,
    )
    from market_agents.memory import MarketMemory
    from market_agents.models import MarketItem
    from market_agents.tools import ToolRegistry

    cfg = AppConfig(
        industry=IndustryConfig(name="Test", keywords=["meble"]),
        agents=AgentsConfig(
            data_dir=str(tmp_path),
            report_dir=str(tmp_path / "r"),
            agentic=AgenticConfig(enabled=True, max_deep_parses=3),
        ),
    )
    item = MarketItem(
        title="Test",
        url="https://example.com/a",
        source="t",
        summary="meble",
    )
    tools = ToolRegistry(cfg, MarketMemory(tmp_path), candidates=[item])
    out = tools.extract_market_intel(
        {
            "url": item.url,
            "signal_type": "pricing",
            "impact": "high",
            "summary": "Obniżka cen 12%",
            "relevance_0_to_1": 0.9,
            "entities": ["Konkurent X"],
            "numbers": ["12%"],
        }
    )
    assert out["ok"]
    assert item.analysis["signal_type"] == "pricing"
    assert tools.structured[0]["impact"] == "high"


def test_relevance_prefers_keywords(tmp_path):
    from market_agents.agents.collector import CollectorAgent
    from market_agents.config import AppConfig, IndustryConfig, SourcesConfig, AgentsConfig
    from market_agents.models import MarketItem
    from market_agents.storage import Storage

    cfg = AppConfig(
        industry=IndustryConfig(
            name="Test",
            keywords=["meble", "ikea"],
            exclude_keywords=["oferta pracy"],
        ),
        sources=SourcesConfig(rss=[]),
        agents=AgentsConfig(
            data_dir=str(tmp_path / "data"),
            report_dir=str(tmp_path / "reports"),
        ),
    )
    agent = CollectorAgent(cfg, Storage(cfg.data_path, cfg.report_path))
    good = MarketItem(
        title="IKEA otwiera nowy sklep meble",
        url="https://x/1",
        source="t",
        summary="meble",
    )
    bad = MarketItem(
        title="Oferta pracy w banku",
        url="https://x/2",
        source="t",
        summary="rekrutacja",
    )
    assert agent._relevance(good) >= 0.35
    assert agent._relevance(bad) == 0.0
