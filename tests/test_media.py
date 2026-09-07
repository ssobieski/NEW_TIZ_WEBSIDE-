from __future__ import annotations

from pathlib import Path
import tempfile

from market_agents.collectors.media import IndustryMediaCollector, extract_exhibitors_from_html
from market_agents.config import AppConfig, IndustryConfig, IndustryMediaSource, load_config
from market_agents.memory import MarketMemory
from market_agents.tools import ToolRegistry


SAMPLE_FAIR_HTML = """
<html><body>
<main>
  <h1>Exhibitor list</h1>
  <ul class="exhibitors">
    <li><a href="/exhibitor/sandvik">Sandvik Coromant</a></li>
    <li><a href="/exhibitor/orbital">OrbitalEdge GmbH</a></li>
    <li><a href="/exhibitor/bladeforge">BladeForge Inc</a></li>
  </ul>
  <p>Visit our news section for machining articles.</p>
  <a href="/news/new-carbide-line">New carbide line debut</a>
  <a href="/exhibitors">All exhibitors</a>
</main>
</body></html>
"""


def test_extract_exhibitors_from_html():
    names = extract_exhibitors_from_html(SAMPLE_FAIR_HTML, limit=20)
    assert any("OrbitalEdge" in n for n in names)
    assert any("BladeForge" in n for n in names)


def test_media_collector_hub_item():
    src = IndustryMediaSource(
        name="Demo Fair",
        url="https://example.com/fair",
        kind="trade_fair",
        extract_exhibitors=True,
        max_links=10,
    )
    collector = IndustryMediaCollector(src)

    # stub fetch
    collector._fetch = lambda url: SAMPLE_FAIR_HTML  # type: ignore[method-assign]
    hub = collector.discover_links(max_links=10)
    assert hub["ok"] is True
    assert hub["count"] >= 1
    assert any("exhibitor" in (l["url"] + l["title"]).lower() for l in hub["links"])
    items = collector.collect(max_items=5)
    assert items
    assert items[0].tags and "trade_fair" in items[0].tags
    assert items[0].analysis.get("media_kind") == "trade_fair"


def test_tiz_config_has_media_sources():
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    assert len(cfg.sources.media) >= 5
    kinds = {s.kind for s in cfg.sources.media}
    assert "trade_fair" in kinds
    assert "magazine" in kinds
    assert "portal" in kinds
    assert any(s.extract_exhibitors for s in cfg.sources.media)


def test_media_tools_in_registry():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = AppConfig(
            industry=IndustryConfig(name="Test", keywords=["cutting"]),
        )
        cfg.agents.data_dir = tmp
        cfg.sources.media = [
            IndustryMediaSource(
                name="Demo Fair",
                url="https://example.com/fair",
                kind="trade_fair",
                extract_exhibitors=True,
            )
        ]
        tools = ToolRegistry(cfg, MarketMemory(Path(tmp)))
        names = {t["function"]["name"] for t in tools.openai_tools_schema()}
        assert "list_media_sources" in names
        assert "discover_media_links" in names
        assert "parse_media_page" in names
        assert "extract_fair_exhibitors" in names

        listed = tools.list_media_sources({"kind": "trade_fair"})
        assert listed["ok"] is True
        assert listed["count"] == 1

        tools._resolve_media({"source_name": "Demo Fair"})._fetch = (  # type: ignore[attr-defined]
            lambda url: SAMPLE_FAIR_HTML
        )
        # discover via stubbed collector path
        collector = IndustryMediaCollector(cfg.sources.media[0])
        collector._fetch = lambda url: SAMPLE_FAIR_HTML  # type: ignore[method-assign]
        # monkeypatch resolve
        tools._resolve_media = lambda args: collector  # type: ignore[method-assign]
        discovered = tools.discover_media_links({"source_name": "Demo Fair", "max_links": 10})
        assert discovered["ok"] is True
        assert discovered["count"] >= 1

        exhibitors = tools.extract_fair_exhibitors(
            {"url": "https://example.com/fair", "check_known": False, "limit": 20}
        )
        assert exhibitors["ok"] is True
        assert exhibitors["count"] >= 1
