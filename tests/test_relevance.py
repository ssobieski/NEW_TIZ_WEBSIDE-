from market_agents.agents.collector import CollectorAgent
from market_agents.config import AppConfig, IndustryConfig, SourcesConfig, RssSource, AgentsConfig
from market_agents.storage import Storage


def test_relevance_prefers_keywords(tmp_path):
    cfg = AppConfig(
        industry=IndustryConfig(
            name="Test",
            keywords=["meble", "ikea"],
            exclude_keywords=["oferta pracy"],
        ),
        sources=SourcesConfig(rss=[]),
        agents=AgentsConfig(data_dir=str(tmp_path / "data"), report_dir=str(tmp_path / "reports")),
    )
    agent = CollectorAgent(cfg, Storage(cfg.data_path, cfg.report_path))
    from market_agents.models import MarketItem

    good = MarketItem(title="IKEA otwiera nowy sklep meble", url="https://x/1", source="t", summary="meble")
    bad = MarketItem(title="Oferta pracy w banku", url="https://x/2", source="t", summary="rekrutacja")
    assert agent._relevance(good) >= 0.35
    assert agent._relevance(bad) == 0.0
