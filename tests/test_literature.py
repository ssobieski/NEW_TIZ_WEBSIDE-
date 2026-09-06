from __future__ import annotations

from pathlib import Path
import tempfile

from market_agents.collectors.literature import LiteratureCollector
from market_agents.config import (
    AgentsConfig,
    AppConfig,
    IndustryConfig,
    LiteratureSource,
    SourcesConfig,
    load_config,
)
from market_agents.literature import (
    LiteratureRegistry,
    classify_literature_kind,
    literature_topics,
)
from market_agents.memory import MarketMemory
from market_agents.tools import ToolRegistry


SAMPLE_HTML = """
<html><body>
<a href="https://link.springer.com/book/10.1007/978-3-030-machining">Metal Cutting Textbook ISBN</a>
<a href="https://doi.org/10.1016/j.cirp.2024.01.001">CIRP Annals paper on chatter</a>
<a href="https://www.youtube.com/watch?v=abc123">Lecture: machining dynamics webinar</a>
<a href="/about">About</a>
</body></html>
"""

SAMPLE_FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Demo Lit</title>
<item>
  <title>Cutting tool wear article</title>
  <link>https://example.com/articles/tool-wear</link>
  <description>Journal paper on carbide machining</description>
  <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
</item>
<item>
  <title>Random HR news</title>
  <link>https://example.com/jobs</link>
  <description>hiring</description>
</item>
</channel></rss>
"""


def test_classify_literature_kinds():
    assert classify_literature_kind("https://youtu.be/x", "webinar lecture") == "video"
    assert classify_literature_kind("https://link.springer.com/book/10", "textbook ISBN") == "book"
    assert classify_literature_kind("https://doi.org/10.1/x", "journal article") == "article"
    assert "cutting_dynamics" in literature_topics("chatter vibration stability")


def test_literature_registry_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        reg = LiteratureRegistry()
        item = reg.upsert(
            url="https://doi.org/10.1000/demo",
            title="Demo paper",
            kind="article",
            authors="A. Author",
            year="2024",
            topics=["parameters"],
        )
        path = reg.save(tmp)
        assert path.exists()
        loaded = LiteratureRegistry.load(tmp)
        assert loaded.items[item.id].title == "Demo paper"
        assert loaded.list(kind="article")[0].authors == "A. Author"


def test_literature_collector_html():
    src = LiteratureSource(
        name="Demo hub",
        url="https://example.com/lit",
        kind="mixed",
        link_keywords=["machining", "cutting", "isbn", "doi", "webinar", "lecture"],
    )
    collector = LiteratureCollector(src)

    class F:
        def get_text(self, url, timeout=35):
            return SAMPLE_HTML

    collector.fetcher = F()  # type: ignore[assignment]
    found = collector.discover_items(max_items=10)
    assert found["ok"] is True
    kinds = {i["kind"] for i in found["items"]}
    assert "book" in kinds
    assert "video" in kinds
    assert "article" in kinds or "proceedings" in kinds
    items = collector.collect(max_items=5)
    assert items[0].tags and "literature" in items[0].tags


def test_literature_collector_feed_filters():
    src = LiteratureSource(
        name="Feed",
        url="https://example.com/",
        feed_url="https://example.com/feed.xml",
        kind="article",
        link_keywords=["cutting", "machining", "tool", "article", "paper", "journal"],
    )
    collector = LiteratureCollector(src)

    class F:
        def get_text(self, url, timeout=35):
            return SAMPLE_FEED

    collector.fetcher = F()  # type: ignore[assignment]
    found = collector.discover_items(max_items=10)
    assert found["ok"] is True
    assert found["count"] >= 1
    assert all("job" not in i["url"] for i in found["items"])


def test_tiz_config_has_literature():
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    assert len(cfg.sources.literature) >= 4
    kinds = {s.kind for s in cfg.sources.literature}
    assert "book" in kinds
    assert "article" in kinds
    assert "video" in kinds
    assert cfg.sources.notion.literature_database


def test_literature_tools_in_registry():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = AppConfig(
            industry=IndustryConfig(name="Test", keywords=["cutting"]),
            sources=SourcesConfig(
                literature=[
                    LiteratureSource(
                        name="Demo",
                        url="https://example.com/lit",
                        kind="article",
                    )
                ]
            ),
            agents=AgentsConfig(data_dir=tmp),
        )
        tools = ToolRegistry(cfg, MarketMemory(Path(tmp)))
        names = {t["function"]["name"] for t in tools.openai_tools_schema()}
        assert "list_literature_sources" in names
        assert "discover_literature" in names
        assert "list_literature" in names
        assert "register_literature" in names
        assert "sync_notion_literature" in names
        listed = tools.list_literature_sources({})
        assert listed["count"] == 1
        reg = tools.register_literature(
            {
                "url": "https://www.youtube.com/watch?v=demo1",
                "title": "Machining lecture",
                "kind": "video",
            }
        )
        assert reg["ok"] is True
        listed2 = tools.list_literature({"kind": "video"})
        assert listed2["count"] >= 1
