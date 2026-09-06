from __future__ import annotations

from pathlib import Path
import tempfile

from market_agents.collectors.social import (
    SocialMediaCollector,
    detect_platform,
    youtube_feed_url,
    _signal_hints,
)
from market_agents.config import (
    AgentsConfig,
    AppConfig,
    IndustryConfig,
    SocialMediaSource,
    SourcesConfig,
    load_config,
)
from market_agents.memory import MarketMemory
from market_agents.tools import ToolRegistry


SAMPLE_HTML = """
<html><head>
<meta property="og:title" content="Demo Cutting Tools"/>
<meta property="og:description" content="Carbide tooling and catalogs"/>
</head><body><main>
<a href="/posts/new-catalog-launch">New catalog launch 2026</a>
<a href="/posts/emo-hannover-booth">Meet us at EMO Hannover</a>
<a href="/about">About</a>
</main></body></html>
"""


def test_detect_platform_and_feed():
    assert detect_platform("https://www.youtube.com/@Brand") == "youtube"
    assert detect_platform("https://www.linkedin.com/company/brand/") == "linkedin"
    assert detect_platform("https://x.com/Brand") == "x"
    assert "channel_id=UCabc" in youtube_feed_url(channel_id="UCabc")
    assert "catalog" in _signal_hints("New catalog launch")
    assert "trade_fair" in _signal_hints("See us at EMO")


def test_social_collector_html_posts():
    src = SocialMediaSource(
        name="Demo LI",
        url="https://www.linkedin.com/company/demo/",
        platform="linkedin",
        brand="Demo",
    )
    collector = SocialMediaCollector(src)

    class F:
        def get_text(self, url, timeout=35):
            return SAMPLE_HTML

    collector.fetcher = F()  # type: ignore[assignment]
    posts = collector.discover_posts(max_items=10)
    assert posts["ok"] is True
    assert posts["count"] >= 2
    assert any("catalog" in (p.get("signal_hints") or []) for p in posts["posts"])
    items = collector.collect(max_items=5)
    assert items[0].tags and "social_media" in items[0].tags
    assert any("post" in (i.tags or []) for i in items)


def test_tiz_config_has_social():
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    assert len(cfg.sources.social) >= 4
    plats = {s.platform for s in cfg.sources.social}
    assert "youtube" in plats
    assert "linkedin" in plats


def test_social_tools_in_registry():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = AppConfig(
            industry=IndustryConfig(name="Test", keywords=["cutting"]),
            sources=SourcesConfig(
                social=[
                    SocialMediaSource(
                        name="Demo",
                        url="https://www.linkedin.com/company/demo/",
                        platform="linkedin",
                        brand="Demo",
                    )
                ]
            ),
            agents=AgentsConfig(data_dir=tmp),
        )
        tools = ToolRegistry(cfg, MarketMemory(Path(tmp)))
        names = {t["function"]["name"] for t in tools.openai_tools_schema()}
        assert "list_social_sources" in names
        assert "discover_social_posts" in names
        assert "parse_social_post" in names
        listed = tools.list_social_sources({})
        assert listed["count"] == 1
