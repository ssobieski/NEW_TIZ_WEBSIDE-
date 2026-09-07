from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from market_agents.config import (
    AgentsConfig,
    AppConfig,
    FleetConfig,
    GovernanceConfig,
    IndustryConfig,
    SourcesConfig,
    load_config,
)
from market_agents.governance import (
    GovernanceEngine,
    PolicyPack,
    PolicyRequest,
    builtin_default_pack,
    evaluate_pack,
    load_policy_pack,
    rule_matches,
    tool_tier,
    validate_policy_pack,
)
from market_agents.memory import MarketMemory
from market_agents.tools import ToolRegistry


def test_load_tiz_policy_pack():
    pack = load_policy_pack("config/governance/tiz.policy.yaml")
    assert pack.id == "tiz-market-agents"
    assert len(pack.rules) >= 5
    warns = validate_policy_pack(pack)
    assert isinstance(warns, list)


def test_deny_overrides_worker_write():
    pack = load_policy_pack("config/governance/tiz.policy.yaml")
    decision = evaluate_pack(
        pack,
        PolicyRequest(action="tool_call", tool="remember", role="worker"),
        mode="enforce",
    )
    assert decision.allowed is False
    assert decision.effect == "deny"
    assert "worker-deny-write-tools" in decision.matched_rules


def test_require_approval_promote():
    pack = load_policy_pack("config/governance/tiz.policy.yaml")
    decision = evaluate_pack(
        pack,
        PolicyRequest(action="tool_call", tool="promote_host_skill", role="central"),
        mode="enforce",
    )
    assert decision.effect == "require_approval"
    assert decision.allowed is False


def test_monitor_mode_allows_denied():
    pack = load_policy_pack("config/governance/tiz.policy.yaml")
    decision = evaluate_pack(
        pack,
        PolicyRequest(action="tool_call", tool="remember", role="worker"),
        mode="monitor",
    )
    assert decision.effect == "deny"
    assert decision.allowed is True  # monitor does not block


def test_cutdata_content_deny():
    pack = load_policy_pack("config/governance/tiz.policy.yaml")
    decision = evaluate_pack(
        pack,
        PolicyRequest(
            action="tool_call",
            tool="register_product_tech",
            role="central",
            args={"notes": "insert vc=220 fz=0.12 for steel"},
        ),
        mode="enforce",
    )
    assert decision.allowed is False
    assert "deny-cutdata-param-injection" in decision.matched_rules


def test_safe_tool_allowed():
    pack = load_policy_pack("config/governance/tiz.policy.yaml")
    decision = evaluate_pack(
        pack,
        PolicyRequest(action="tool_call", tool="list_candidates", role="central"),
        mode="enforce",
    )
    assert decision.allowed is True


def test_file_scheme_denied():
    pack = builtin_default_pack()
    decision = evaluate_pack(
        pack,
        PolicyRequest(action="fetch", url="file:///etc/passwd"),
        mode="enforce",
    )
    assert decision.allowed is False


def test_engine_audit_and_rate_limit(tmp_path: Path):
    pack = PolicyPack.from_dict(
        {
            "version": 1,
            "id": "rate-test",
            "defaults": {"effect": "allow", "audit": True},
            "rules": [
                {
                    "id": "limit-fetch",
                    "priority": 100,
                    "effect": "allow",
                    "match": {
                        "action": "tool_call",
                        "tool": "fetch_and_parse",
                        "max_per_run": 2,
                    },
                }
            ],
        }
    )
    engine = GovernanceEngine(
        pack=pack,
        enabled=True,
        mode="enforce",
        audit_dir=tmp_path / "gov",
        role="central",
    )
    assert engine.authorize_tool("fetch_and_parse", {"url": "https://example.com"}).allowed
    assert engine.authorize_tool("fetch_and_parse", {"url": "https://example.com"}).allowed
    third = engine.authorize_tool("fetch_and_parse", {"url": "https://example.com"})
    assert third.allowed is False
    assert "rate limit" in third.reason
    rows = engine.recent_audit(limit=10)
    assert len(rows) >= 3


def test_tool_registry_governance_blocks_promote(tmp_path: Path):
    cfg = AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cutting"]),
        sources=SourcesConfig(),
        agents=AgentsConfig(
            data_dir=str(tmp_path),
            fleet=FleetConfig(role="central"),
            governance=GovernanceConfig(
                enabled=True,
                mode="enforce",
                policy_pack="config/governance/tiz.policy.yaml",
                audit_dir=str(tmp_path / "gov"),
            ),
        ),
    )
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    out = tools.call("promote_host_skill", {"host_or_url": "example.com"})
    assert out["ok"] is False
    assert out.get("governance") is True
    assert out.get("effect") == "require_approval"
    st = tools.call("governance_status", {})
    assert st["ok"] is True
    assert st["enabled"] is True
    assert st["policy_id"] == "tiz-market-agents"


def test_tool_tier_helpers():
    assert tool_tier("list_candidates") == "safe"
    assert tool_tier("fetch_and_parse") == "fetch"
    assert tool_tier("remember") == "write"
    assert tool_tier("search_notion") == "cloud"


def test_tiz_config_has_governance():
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    assert cfg.agents.governance.enabled is True
    assert cfg.agents.governance.policy_pack.endswith("tiz.policy.yaml")
    engine = GovernanceEngine.from_config(cfg)
    assert engine.enabled is True
    assert engine.pack is not None
