"""Security spec: untrusted feeds must not expand XML entities.

These tests fail while a compact DOCTYPE still expands into a large title
during literature collection, discover_literature, or the other feed parsers
that share that behavior. A fix passes them by refusing the document before
the expanded text is built. Expat's default amplification guard is not
enough: the payload below stays under that guard and under the polite
fetcher's raw response cap, and it still expands.

CollectorAgent.run is not called here. It does not parse feeds itself; the
scheduled literature path is LiteratureCollector.collect, which is tested.
"""

from __future__ import annotations

import dataclasses
import threading
import tracemalloc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from market_agents.collectors.firm_discovery import FirmDiscoveryCollector
from market_agents.collectors.literature import LiteratureCollector
from market_agents.collectors.rss import RssCollector
from market_agents.collectors.social import SocialMediaCollector
from market_agents.config import (
    AgenticConfig,
    AgentsConfig,
    AppConfig,
    CrawlConfig,
    FirmDiscoveryConfig,
    GovernanceConfig,
    IndustryConfig,
    LiteratureSource,
    RssSource,
    SocialMediaSource,
    SourcesConfig,
)
from market_agents.firms import KnownFirmsIndex
from market_agents.literature import LiteratureRegistry
from market_agents.memory import MarketMemory
from market_agents.polite_http import AdaptivePoliteFetcher
from market_agents.tools import ToolRegistry

# 1000 * 10**3 characters. Raw document stays a few KB, under both the
# 15_000_000 byte fetch cap and expat's 8 MiB activation threshold.
_LEAF = "A" * 1000
_LEVELS = 3
_REPS = 10
EXPANDED_MARK = "A" * 50_000
TEXT_LIMIT = 64 * 1024
PEAK_LIMIT = 900_000
FETCH_CAP = 15_000_000

ROOT = Path(__file__).resolve().parents[1]


def compact_entity_bomb() -> str:
    parts = [f'<!ENTITY a0 "{_LEAF}">']
    for level in range(1, _LEVELS + 1):
        parts.append(f'<!ENTITY a{level} "{"&a%d;" % (level - 1) * _REPS}">')
    return (
        '<?xml version="1.0"?>'
        "<!DOCTYPE bomb [" + " ".join(parts) + "]>"
        "<rss version=\"2.0\"><channel><item>"
        "<title>&a3;</title>"
        "<link>https://example.com/machining-handbook</link>"
        "<description>cutting tool</description>"
        "</item></channel></rss>"
    )


BOMB = compact_entity_bomb()
BOMB_BYTES = BOMB.encode("utf-8")


def _iter_text(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, BaseModel):
        yield from _iter_text(value.model_dump())
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        yield from _iter_text(dataclasses.asdict(value))
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_text(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_text(item)


def _assert_bounded(value: object) -> None:
    for text in _iter_text(value):
        if len(text) > TEXT_LIMIT or len(EXPANDED_MARK) <= len(text) and EXPANDED_MARK in text:
            raise AssertionError(
                f"feed text length {len(text)} exceeds the {TEXT_LIMIT} byte expanded-size limit"
            )


def assert_feed_refused(call):
    """Run call(). Expansion, or a MemoryError from expansion, fails the test."""
    tracemalloc.start()
    try:
        try:
            result = call()
        except MemoryError:
            raise
        except Exception as exc:
            assert EXPANDED_MARK not in f"{type(exc).__name__}: {exc}"
            result = exc
        else:
            _assert_bounded(result)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < PEAK_LIMIT, f"feed parse allocated {peak} bytes from a {len(BOMB_BYTES)} byte document"
    return result


@pytest.fixture
def bomb_responses(monkeypatch):
    """Serve the compact bomb through the real polite-fetcher HTTP client."""
    served: list[int] = []

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

        def get(self, url, headers=None):
            return self.request("GET", url, headers=headers)

        def request(self, method, url, headers=None):
            url_s = str(url)
            if urlparse_path_is_robots(url_s):
                body = b"User-agent: *\nAllow: /\n"
            else:
                body = BOMB_BYTES
                served.append(len(body))
            request = httpx.Request(method, url_s)
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "application/rss+xml; charset=utf-8"},
                request=request,
            )

    monkeypatch.setattr("market_agents.polite_http.httpx.Client", Client)
    AdaptivePoliteFetcher.reset_shared()
    yield served
    AdaptivePoliteFetcher.reset_shared()


def urlparse_path_is_robots(url: str) -> bool:
    return url.split("?", 1)[0].rstrip("/").endswith("/robots.txt")


def _app_config(tmp_path: Path, **sources_kwargs) -> AppConfig:
    return AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cutting", "machining", "tool"]),
        sources=SourcesConfig(**sources_kwargs),
        agents=AgentsConfig(
            data_dir=str(tmp_path / "data"),
            report_dir=str(tmp_path / "reports"),
            crawl=CrawlConfig(
                min_delay_seconds=0,
                jitter_seconds=0,
                max_retries=0,
                respect_robots_txt=True,
                block_private_networks=True,
                resolve_dns_for_ssrf=True,
                max_response_bytes=FETCH_CAP,
            ),
            governance=GovernanceConfig(
                audit_dir=str(tmp_path / "audit"),
                policy_pack=str(ROOT / "config" / "governance" / "tiz.policy.yaml"),
            ),
            agentic=AgenticConfig(
                learn_parse_rules=False,
                learn_parsing_skills=False,
            ),
        ),
    )


def _tools(tmp_path: Path, **sources_kwargs) -> ToolRegistry:
    config = _app_config(tmp_path, **sources_kwargs)
    return ToolRegistry(config, MarketMemory(config.data_path))


def _assert_cap_did_not_see_expansion(served: list[int], fetcher: AdaptivePoliteFetcher) -> None:
    assert len(BOMB_BYTES) < 8_000
    assert served, "polite fetcher did not download the feed body"
    assert max(served) == len(BOMB_BYTES)
    assert max(served) < fetcher.policy.max_response_bytes
    assert fetcher.policy.max_response_bytes >= FETCH_CAP


@pytest.mark.parametrize(
    "feed_url",
    [
        "https://example.com/feed",
        "https://example.com/rss",
        "https://example.com/atom",
        "https://example.com/catalog.xml",
    ],
)
def test_discover_literature_path_token_rejects_entity_expansion(tmp_path, bomb_responses, feed_url):
    tools = _tools(tmp_path)
    assert_feed_refused(
        lambda: tools.call(
            "discover_literature",
            {"url": feed_url, "max_items": 1},
        )
    )
    _assert_cap_did_not_see_expansion(bomb_responses, tools._fetcher)
    registry = LiteratureRegistry.path_for(tmp_path / "data")
    if registry.exists():
        assert EXPANDED_MARK not in registry.read_text(encoding="utf-8")


def test_discover_literature_configured_feed_url_rejects_entity_expansion(tmp_path, bomb_responses):
    tools = _tools(
        tmp_path,
        literature=[
            LiteratureSource(
                name="Configured feed",
                url="https://example.com/hub",
                feed_url="https://example.com/updates",
                kind="mixed",
            )
        ],
    )
    assert_feed_refused(
        lambda: tools.call(
            "discover_literature",
            {"source_name": "Configured feed", "max_items": 1},
        )
    )
    _assert_cap_did_not_see_expansion(bomb_responses, tools._fetcher)
    registry = LiteratureRegistry.path_for(tmp_path / "data")
    if registry.exists():
        assert EXPANDED_MARK not in registry.read_text(encoding="utf-8")


def test_scheduled_literature_collect_rejects_entity_expansion(tmp_path, bomb_responses):
    from market_agents.polite_http import build_fetcher_from_config

    config = _app_config(
        tmp_path,
        literature=[
            LiteratureSource(
                name="Nightly",
                url="https://example.com/hub",
                feed_url="https://example.com/updates",
                kind="mixed",
            )
        ],
    )
    # CollectorAgent.run installs this fetcher and calls LiteratureCollector.collect.
    AdaptivePoliteFetcher.reset_shared()
    fetcher = build_fetcher_from_config(config)
    collector = LiteratureCollector(config.sources.literature[0], fetcher=fetcher)
    assert_feed_refused(lambda: collector.collect(max_items=1))
    _assert_cap_did_not_see_expansion(bomb_responses, fetcher)


@pytest.fixture
def bomb_server():
    payload = BOMB_BYTES

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/feed.xml"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_rss_collector_rejects_entity_expansion(bomb_server):
    collector = RssCollector(RssSource(name="Remote", url=bomb_server))
    assert_feed_refused(lambda: collector.collect(max_items=1))


def test_social_feed_url_rejects_entity_expansion(bomb_server):
    collector = SocialMediaCollector(
        SocialMediaSource(
            name="Remote",
            url="https://example.com/brand",
            platform="other",
            feed_url=bomb_server,
        )
    )
    assert_feed_refused(lambda: collector.discover_posts(max_items=1))


def test_youtube_feed_url_rejects_entity_expansion(bomb_server):
    collector = SocialMediaCollector(
        SocialMediaSource(
            name="Channel",
            url="https://www.youtube.com/channel/UCexample",
            platform="youtube",
            feed_url=bomb_server,
        )
    )
    assert_feed_refused(lambda: collector.collect(max_items=1))


def test_firm_discovery_feed_url_rejects_entity_expansion(bomb_server):
    collector = FirmDiscoveryCollector(
        FirmDiscoveryConfig(
            enabled=True,
            search_queries=[],
            feed_urls=[bomb_server],
        ),
        KnownFirmsIndex(),
    )
    assert_feed_refused(lambda: collector.collect(max_items=1))


def test_discover_social_posts_configured_feed_rejects_entity_expansion(tmp_path, bomb_server):
    tools = _tools(
        tmp_path,
        social=[
            SocialMediaSource(
                name="Configured social",
                url="https://example.com/brand",
                platform="other",
                feed_url=bomb_server,
            )
        ],
    )
    assert_feed_refused(
        lambda: tools.call(
            "discover_social_posts",
            {"source_name": "Configured social", "max_items": 1},
        )
    )


def test_discover_new_firms_feed_url_rejects_entity_expansion(tmp_path, bomb_server):
    tools = _tools(
        tmp_path,
        firm_discovery=FirmDiscoveryConfig(
            enabled=True,
            search_queries=[],
            feed_urls=[bomb_server],
        ),
    )
    assert_feed_refused(
        lambda: tools.call(
            "discover_new_firms",
            {"limit": 1, "run_search": True, "parse_candidates": False},
        )
    )
