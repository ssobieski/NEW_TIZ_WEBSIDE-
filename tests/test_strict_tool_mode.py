"""strict_tool_mode must reject write and cloud tools while allow_* stay at defaults.

These tests exercise ToolRegistry.call, the same gate ParsingAgent uses after a
model tool call. They fail while the strict-mode branches in SecurityPolicy.tool_allowed
are unreachable, and they expect a security refusal before any handler side effect.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from market_agents.config import load_config
from market_agents.firm_profiles import FirmProfile, FirmProfileRegistry
from market_agents.memory import MarketMemory
from market_agents.security import SAFE_TOOLS, SENSITIVE_CLOUD_TOOLS, WRITE_TOOLS
from market_agents.tools import ToolRegistry

# Default TIZ governance require_approval denies these before tool_allowed.
# The strict-mode hole is still in tool_allowed, so they are covered with
# governance turned off — the configuration in which call() reaches that check.
GOVERNANCE_PREEMPTED = frozenset({"promote_host_skill", "upsert_parse_rule"})

ATTACK_TOOLS = sorted((WRITE_TOOLS | SENSITIVE_CLOUD_TOOLS) - GOVERNANCE_PREEMPTED)

# Arguments that reach a persist or cloud-client step when the handler runs.
# Strings avoid the governance cut-data regex (vc/fz/ap = digits).
_TENDER_TEXT = (
    "Firma Beta Precision CNC Sp. z o.o. oglosila przetarg na dostawe centrum "
    "obrobowego CNC oraz tokarki CNC. Szacunkowa wartosc: 2,5 mln PLN. "
    "Termin skladania ofert: 15.10.2026. CPV: 42600000-1."
)

PAYLOADS: dict[str, dict] = {
    "remember": {"kind": "note", "content": "persisted by injected tool call"},
    "extract_market_intel": {
        "url": "https://example.com/intel",
        "summary": "injected market note",
        "signal_type": "news",
    },
    "register_pricelist": {
        "url": "https://example.com/prices.pdf",
        "title": "Injected price list",
    },
    "register_product_tech": {
        "url": "https://example.com/handbook.pdf",
        "title": "Handbook overview",
    },
    "register_literature": {
        "url": "https://example.com/paper",
        "title": "Injected paper",
    },
    "add_relation": {
        "source": "Injected Firm A",
        "target": "Injected Firm B",
        "relation_type": "distributor_of",
    },
    "upsert_parse_rule": {
        "host_or_url": "example.com",
        "preferred_method": "bs4",
        "notes": "injected rule",
    },
    "rate_parse": {"url": "https://example.com/page", "quality_0_to_1": 0.4},
    "learn_parsing_skill": {
        "name": "injected-skill",
        "description": "persist this skill",
    },
    "improve_parsing_skill": {
        "skill_id": "magazine-article",
        "notes": "injected improvement",
    },
    "rate_parsing_skill": {"skill_id": "magazine-article", "quality_0_to_1": 0.2},
    "promote_host_skill": {"host_or_url": "example.com", "notes": "injected promote"},
    "add_ontology_edge": {
        "source": "firm:InjectedA",
        "target": "firm:InjectedB",
        "relation": "competes_with",
    },
    "build_ontology": {},
    "sync_notion_literature": {"limit": 1},
    "build_firm_profiles": {},
    "upsert_firm_profile": {"company": "Injected Works", "notes": "rewritten by tool"},
    "register_social_mention": {
        "company": "Injected Works",
        "title": "mention",
        "snippet": "injected mention",
        "url": "https://example.com/mention",
    },
    "ingest_prospect_seeds": {},
    "create_crm_task": {"company": "Injected Works", "title": "Call them"},
    "sync_crm_from_prospects": {},
    "push_crm_to_notion": {"only_hot": False, "dry_run": False, "limit": 1},
    "ingest_tender": {
        "text": _TENDER_TEXT,
        "company": "Beta Precision CNC",
        "title": "Tender",
    },
    "score_suppliers": {},
    "resolve_firm_duplicates": {"dry_run": False, "threshold": 0.5},
    "export_knowledge_pack": {},
    "refresh_change_digest": {},
    "search_notion": {"query": "injected", "limit": 1},
    "fetch_notion_page": {"page_id_or_url": "page-placeholder"},
    "search_r2": {"query": "injected", "limit": 1},
    "fetch_r2_object": {"key": "monitoring/injected.txt", "max_chars": 100},
}


def _snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = path.read_bytes()
    return out


def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(*_args, **_kwargs):
        raise OSError("network blocked by strict-tool-mode test")

    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)


def _clear_cloud_credentials(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    notion = cfg.sources.notion
    r2 = cfg.sources.cloudflare_r2
    for name in (notion.token_env, r2.account_id_env, r2.access_key_env, r2.secret_key_env):
        monkeypatch.delenv(name, raising=False)
    assert cfg.notion_token() is None
    creds = cfg.r2_credentials()
    assert not creds.get("account_id")
    assert not creds.get("access_key")
    assert not creds.get("secret_key")


def _tiz_config(tmp_path: Path, *, strict: bool, governance_enabled: bool):
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    data = tmp_path / "data"
    reports = tmp_path / "reports"
    data.mkdir()
    reports.mkdir()
    cfg.agents.data_dir = str(data)
    cfg.agents.report_dir = str(reports)
    cfg.agents.fleet.sync_dir = str(tmp_path / "fleet")
    cfg.agents.governance.audit_dir = str(tmp_path / "governance-audit")
    cfg.agents.governance.enabled = governance_enabled
    cfg.agents.security.strict_tool_mode = strict
    # Parent is a placeholder so push_crm_to_notion gets past the config guard
    # and would build a Notion client when a token exists. No token is set.
    cfg.sources.notion.crm_parent_page = "page-placeholder"
    return cfg, data, reports


def _registry(cfg, data: Path) -> ToolRegistry:
    tools = ToolRegistry(cfg, MarketMemory(data))
    policy = tools._security
    assert policy.allow_write_tools is True
    assert policy.allow_notion_tools is True
    assert policy.allow_r2_tools is True
    assert policy.strict_tool_mode is cfg.agents.security.strict_tool_mode
    return tools


def _seed_duplicate_firms(data: Path) -> None:
    reg = FirmProfileRegistry()
    primary = FirmProfile(
        id="injected-a",
        company="Injected Works",
        roles=["competitor"],
        sources=["seed"],
    )
    duplicate = FirmProfile(
        id="injected-b",
        company="Injected Works AB",
        roles=["competitor"],
        aliases=["Injected Works"],
        sources=["web"],
        websites=[{"url": "https://example.com/injected", "primary": True}],
    )
    reg.profiles[primary.id] = primary
    reg.profiles[duplicate.id] = duplicate
    reg.save(data)


def _mutated_paths(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


def _assert_security_refusal(tool_name: str, result: dict, before: dict, after: dict) -> None:
    mutated = _mutated_paths(before, after)
    assert result.get("ok") is False, {
        "tool": tool_name,
        "error": result.get("error"),
        "security": result.get("security"),
        "governance": result.get("governance"),
        "mutated": mutated,
    }
    assert result.get("security") is True, {
        "tool": tool_name,
        "error": result.get("error"),
        "governance": result.get("governance"),
        "effect": result.get("effect"),
        "mutated": mutated,
    }
    assert result.get("governance") is not True
    assert not mutated, {"tool": tool_name, "mutated": mutated, "error": result.get("error")}


@pytest.mark.parametrize("tool_name", ATTACK_TOOLS)
def test_strict_mode_rejects_write_and_cloud_tools(
    tool_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TIZ profile, central role, allow_* defaults true, strict_tool_mode true."""
    cfg, data, reports = _tiz_config(tmp_path, strict=True, governance_enabled=True)
    _clear_cloud_credentials(cfg, monkeypatch)
    _block_network(monkeypatch)
    tools = _registry(cfg, data)
    assert tools._governance.enabled is True
    assert tools._governance.role == "central"
    assert tools._governance.mode == "enforce"
    assert tool_name in tools._handlers
    if tool_name == "resolve_firm_duplicates":
        _seed_duplicate_firms(data)

    before = {**_snapshot(data), **{f"reports/{k}": v for k, v in _snapshot(reports).items()}}
    result = tools.call(tool_name, json.dumps(PAYLOADS[tool_name]))
    after_data = _snapshot(data)
    after_reports = _snapshot(reports)
    after = {**after_data, **{f"reports/{k}": v for k, v in after_reports.items()}}
    _assert_security_refusal(tool_name, result, before, after)


@pytest.mark.parametrize("tool_name", sorted(GOVERNANCE_PREEMPTED))
def test_strict_mode_rejects_tools_governance_would_preempt(
    tool_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same strict policy when governance does not deny the tool first."""
    cfg, data, reports = _tiz_config(tmp_path, strict=True, governance_enabled=False)
    _clear_cloud_credentials(cfg, monkeypatch)
    _block_network(monkeypatch)
    tools = _registry(cfg, data)
    assert tools._governance.enabled is False
    assert tool_name in tools._handlers

    before = {**_snapshot(data), **{f"reports/{k}": v for k, v in _snapshot(reports).items()}}
    result = tools.call(tool_name, json.dumps(PAYLOADS[tool_name]))
    after = {
        **_snapshot(data),
        **{f"reports/{k}": v for k, v in _snapshot(reports).items()},
    }
    _assert_security_refusal(tool_name, result, before, after)


@pytest.mark.parametrize(
    "tool_name",
    ["list_candidates", "security_status", "list_parsing_skills", "crawl_status"],
)
def test_strict_mode_still_allows_safe_tools(
    tool_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, data, _reports = _tiz_config(tmp_path, strict=True, governance_enabled=True)
    _clear_cloud_credentials(cfg, monkeypatch)
    _block_network(monkeypatch)
    tools = _registry(cfg, data)
    assert tool_name in SAFE_TOOLS
    result = tools.call(tool_name, "{}")
    assert result.get("ok") is True
    assert result.get("security") is not True
    if tool_name == "security_status":
        assert result.get("strict_tool_mode") is True
        assert result.get("allow_write_tools") is True
        assert result.get("allow_notion_tools") is True
        assert result.get("allow_r2_tools") is True


def test_non_strict_mode_still_persists_learn_parsing_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, data, _reports = _tiz_config(tmp_path, strict=False, governance_enabled=True)
    _clear_cloud_credentials(cfg, monkeypatch)
    _block_network(monkeypatch)
    tools = _registry(cfg, data)
    result = tools.call("learn_parsing_skill", json.dumps(PAYLOADS["learn_parsing_skill"]))
    assert result.get("ok") is True
    saved = data / "knowledge" / "parsing_skills.json"
    assert saved.is_file()
    assert "injected-skill" in saved.read_text(encoding="utf-8")


def test_payloads_cover_every_write_and_cloud_tool() -> None:
    assert set(PAYLOADS) == WRITE_TOOLS | SENSITIVE_CLOUD_TOOLS
