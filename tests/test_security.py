from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from market_agents.config import (
    AgentsConfig,
    AppConfig,
    IndustryConfig,
    SecurityConfig,
    SourcesConfig,
    load_config,
)
from market_agents.memory import MarketMemory
from market_agents.polite_http import AdaptivePoliteFetcher, CrawlBlockedError, CrawlPolicy
from market_agents.security import (
    SecurityError,
    SecurityPolicy,
    enforce_r2_key_prefix,
    iter_safe_pack_files,
    redact_secrets,
    resolve_fleet_pack_path,
    validate_fetch_url,
)
from market_agents.tools import ToolRegistry


def test_ssrf_blocks_localhost_and_metadata():
    with pytest.raises(SecurityError):
        validate_fetch_url("http://127.0.0.1/admin")
    with pytest.raises(SecurityError):
        validate_fetch_url("http://localhost:8000/v1")
    with pytest.raises(SecurityError):
        validate_fetch_url("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(SecurityError):
        validate_fetch_url("http://10.0.0.5/internal")
    with pytest.raises(SecurityError):
        validate_fetch_url("file:///etc/passwd")
    # public ok
    assert validate_fetch_url("https://example.com/path").startswith("https://")


def test_ssrf_allowlist():
    with pytest.raises(SecurityError):
        validate_fetch_url("https://evil.example", allowed_hosts=["sandvik.com"])
    ok = validate_fetch_url(
        "https://www.sandvik.com/x", allowed_hosts=["sandvik.com"]
    )
    assert "sandvik.com" in ok


def test_fetcher_blocks_private(monkeypatch):
    fetcher = AdaptivePoliteFetcher(
        policy=CrawlPolicy(enabled=True, block_private_networks=True, resolve_dns_for_ssrf=False)
    )
    with pytest.raises(CrawlBlockedError):
        fetcher.get("http://192.168.1.1/")


def test_redact_secrets():
    raw = "Authorization: Bearer secret_abc123XYZ0000000000 and AKIAIOSFODNN7EXAMPLE"
    out = redact_secrets(raw)
    assert "secret_abc" not in out
    assert "AKIA" not in out or "REDACTED" in out
    assert "Bearer" in out or "REDACTED" in out


def test_r2_prefix_enforcement():
    enforce_r2_key_prefix("monitoring/a.json", "monitoring/")
    with pytest.raises(SecurityError):
        enforce_r2_key_prefix("../../etc/passwd", "monitoring/")
    with pytest.raises(SecurityError):
        enforce_r2_key_prefix("other/a.json", "monitoring/")


def test_fleet_pack_path_and_allowlist(tmp_path: Path):
    sync = tmp_path / "fleet"
    outbox = sync / "outbox" / "knowledge" / "pack1"
    outbox.mkdir(parents=True)
    (outbox / "ontology.json").write_text("{}", encoding="utf-8")
    (outbox / "evil.exe").write_text("x", encoding="utf-8")
    (outbox / "not_allowed.json").write_text("{}", encoding="utf-8")
    # absolute path outside outbox must be rejected
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ontology.json").write_text("{}", encoding="utf-8")
    assert resolve_fleet_pack_path(str(outside), sync_root=sync) is None
    safe = resolve_fleet_pack_path(None, sync_root=sync, pack_id="pack1")
    assert safe == outbox.resolve()
    files = iter_safe_pack_files(outbox)
    names = {p.name for p in files}
    assert "ontology.json" in names
    assert "evil.exe" not in names
    assert "not_allowed.json" not in names


def test_tool_policy_blocks_notion_when_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = AppConfig(
            industry=IndustryConfig(name="Test", keywords=["cutting"]),
            sources=SourcesConfig(),
            agents=AgentsConfig(
                data_dir=tmp,
                security=SecurityConfig(
                    allow_notion_tools=False,
                    allow_r2_tools=False,
                    allow_write_tools=False,
                ),
            ),
        )
        tools = ToolRegistry(cfg, MarketMemory(Path(tmp)))
        blocked = tools.call("search_notion", {"query": "x"})
        assert blocked["ok"] is False
        assert blocked.get("security") is True
        blocked2 = tools.call("upsert_parse_rule", {"host_or_url": "example.com"})
        assert blocked2["ok"] is False
        st = tools.call("security_status", {})
        assert st["ok"] is True
        assert st["allow_notion_tools"] is False


def test_tiz_config_has_security():
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    assert cfg.agents.security.block_private_networks is True
    assert cfg.agents.crawl.block_private_networks is True
    assert cfg.agents.agentic.auto_promote_host_skills is False
