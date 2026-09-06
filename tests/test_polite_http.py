from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from market_agents.config import (
    AgenticConfig,
    AgentsConfig,
    AppConfig,
    CrawlConfig,
    IndustryConfig,
    load_config,
)
from market_agents.memory import MarketMemory
from market_agents.polite_http import (
    AdaptivePoliteFetcher,
    CrawlBlockedError,
    CrawlPolicy,
)
from market_agents.tools import ToolRegistry


def _mock_response(status: int = 200, text: str = "ok", headers: dict | None = None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.content = text.encode()
    resp.headers = headers or {}
    resp.url = "https://example.com/x"
    resp.request = MagicMock()
    resp.raise_for_status = MagicMock()
    if status >= 400 and status not in {403, 429, 503}:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status}")
    return resp


def test_polite_fetcher_spaces_requests_per_host(tmp_path: Path):
    AdaptivePoliteFetcher.reset_shared()
    policy = CrawlPolicy(
        min_delay_seconds=0.15,
        max_delay_seconds=1.0,
        jitter_seconds=0.0,
        respect_robots_txt=False,
        adaptive=True,
        global_concurrency=1,
        per_host_concurrency=1,
    )
    fetcher = AdaptivePoliteFetcher(policy=policy, data_dir=tmp_path)
    with patch("httpx.Client") as client_cls:
        client = MagicMock()
        client_cls.return_value.__enter__.return_value = client
        client.request.return_value = _mock_response(200, "hello")
        t0 = time.time()
        fetcher.get_text("https://example.com/a")
        fetcher.get_text("https://example.com/b")
        elapsed = time.time() - t0
    assert elapsed >= 0.14
    health = fetcher.status("example.com")["health"]
    assert health["success_count"] >= 2


def test_429_triggers_cooldown(tmp_path: Path):
    AdaptivePoliteFetcher.reset_shared()
    policy = CrawlPolicy(
        min_delay_seconds=0.01,
        jitter_seconds=0.0,
        respect_robots_txt=False,
        max_retries=0,
        cooldown_on_block_seconds=2.0,
    )
    fetcher = AdaptivePoliteFetcher(policy=policy, data_dir=tmp_path)
    with patch("httpx.Client") as client_cls:
        client = MagicMock()
        client_cls.return_value.__enter__.return_value = client
        client.request.return_value = _mock_response(
            429, "slow down", headers={"Retry-After": "30"}
        )
        try:
            fetcher.get("https://blocked.example/page")
        except Exception:
            pass
    st = fetcher.status("blocked.example")
    assert st["health"]["block_count"] >= 1
    assert st["health"]["cooldown_until"] > time.time() - 1
    assert st["in_cooldown"] is True or st["health"]["delay_seconds"] > policy.min_delay_seconds


def test_403_raises_crawl_blocked(tmp_path: Path):
    AdaptivePoliteFetcher.reset_shared()
    policy = CrawlPolicy(
        min_delay_seconds=0.01,
        jitter_seconds=0.0,
        respect_robots_txt=False,
        max_retries=0,
        cooldown_on_block_seconds=5.0,
    )
    fetcher = AdaptivePoliteFetcher(policy=policy, data_dir=tmp_path)
    with patch("httpx.Client") as client_cls:
        client = MagicMock()
        client_cls.return_value.__enter__.return_value = client
        client.request.return_value = _mock_response(403, "forbidden")
        try:
            fetcher.get("https://deny.example/x")
            raised = False
        except CrawlBlockedError:
            raised = True
    assert raised is True
    assert fetcher.status("deny.example")["in_cooldown"] is True


def test_batch_parse_same_host_is_serial(tmp_path: Path):
    AdaptivePoliteFetcher.reset_shared()
    cfg = AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cutting"]),
        agents=AgentsConfig(
            data_dir=str(tmp_path),
            agentic=AgenticConfig(parallel_fetches=4, max_deep_parses=4),
            crawl=CrawlConfig(
                min_delay_seconds=0.01,
                jitter_seconds=0.0,
                respect_robots_txt=False,
                global_concurrency=2,
            ),
        ),
    )
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    with patch.object(tools, "fetch_and_parse", return_value={"ok": True}) as mocked:
        out = tools.batch_parse(
            {
                "urls": [
                    "https://same.example/1",
                    "https://same.example/2",
                    "https://same.example/3",
                ]
            }
        )
    assert out["ok"] is True
    assert out["workers"] == 1
    assert out["polite"] is True
    assert mocked.call_count == 3


def test_crawl_status_tool_in_schema(tmp_path: Path):
    cfg = AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cutting"]),
        agents=AgentsConfig(data_dir=str(tmp_path)),
    )
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    names = {t["function"]["name"] for t in tools.openai_tools_schema()}
    assert "crawl_status" in names
    assert tools.crawl_status({})["ok"] is True


def test_tiz_config_has_polite_crawl_defaults():
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    assert cfg.agents.crawl.enabled is True
    assert cfg.agents.crawl.global_concurrency <= 2
    assert cfg.agents.agentic.parallel_fetches <= 2
    assert cfg.agents.crawl.respect_robots_txt is True
    assert cfg.agents.crawl.adaptive is True
