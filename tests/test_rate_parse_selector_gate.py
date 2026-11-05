"""Spec for the rate_parse selector gate.

These tests exercise ToolRegistry.call with the shipped TIZ policy. They fail
while a tool can persist a CSS selector or parse method without the same
human-approval gate as upsert_parse_rule. A fix should make this module pass.
"""

from __future__ import annotations

import json
from pathlib import Path

from market_agents.approvals import ApprovalStore, normalize_host
from market_agents.config import load_config
from market_agents.fleet import FleetConfig, FleetSync, fleet_config_from_app
from market_agents.memory import MarketMemory
from market_agents.parse_rules import SiteParseRulesStore, host_from_url
from market_agents.parsing import IntelligentParser
from market_agents.tools import ToolRegistry

ATTACK_SELECTOR = ".attacker-payload"
ATTACK_METHOD = "css"
VICTIM_URL = "https://victim.example/catalog/item"
PAGE_HTML = """
<html><head><title>Real article</title></head>
<body>
<div class="attacker-payload">ATTACKER_MARKER_PARSE_POISON payload payload payload
payload payload payload payload payload payload payload payload payload payload
payload payload payload payload payload payload payload payload payload</div>
<article class="story-body">
<h1>carbide tooling</h1>
<p>Legitimate body text about carbide tooling and machining centres that the parser
should keep for the operator excerpt.</p>
<p>More legitimate public article copy about distributors, catalogues, and tool life
in wet machining trials across DACH.</p>
</article>
</body></html>
"""


def _shipped_tools(tmp_path: Path, *, auto_promote: bool = False) -> tuple[ToolRegistry, object]:
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    cfg.agents.data_dir = str(tmp_path)
    cfg.agents.governance.audit_dir = str(tmp_path / "gov")
    cfg.agents.fleet.sync_dir = str(tmp_path / "fleet")
    cfg.agents.agentic.trace_dir = str(tmp_path / "traces")
    cfg.agents.agentic.auto_promote_host_skills = auto_promote
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    return tools, cfg


def _call(tools: ToolRegistry, name: str, args: dict) -> dict:
    # ParsingAgent passes the model arguments as a JSON string.
    return tools.call(name, json.dumps(args))


def _grant(cfg, tool: str, *, host: str | None) -> str:
    audit = Path(cfg.agents.governance.audit_dir)
    store = ApprovalStore.load(audit)
    grant = store.approve(tool, host=host, approved_by="operator", note="selector gate test")
    store.save(audit)
    return grant.id


def _audit_text(cfg) -> str:
    path = Path(cfg.agents.governance.audit_dir) / "audit.jsonl"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _rules_blob(tmp_path: Path) -> str:
    path = tmp_path / "knowledge" / "site_parse_rules.json"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _skills_blob(tmp_path: Path) -> str:
    path = tmp_path / "knowledge" / "parsing_skills.json"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _memory_blob(tmp_path: Path) -> str:
    path = tmp_path / "memory.jsonl"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def assert_approval_refusal(result: dict) -> None:
    assert result.get("ok") is False, result
    assert result.get("governance") is True, result
    assert result.get("effect") == "require_approval", result


def _parse(rules: SiteParseRulesStore, url: str):
    return IntelligentParser(rules=rules, learn=False).parse_html(url, PAGE_HTML)


def test_upsert_parse_rule_requires_approval_on_shipped_policy(tmp_path: Path):
    """Control: the existing gate still refuses the sibling write with no side effect."""
    tools, _cfg = _shipped_tools(tmp_path)
    result = _call(
        tools,
        "upsert_parse_rule",
        {
            "host_or_url": VICTIM_URL,
            "preferred_method": ATTACK_METHOD,
            "css_selector": ATTACK_SELECTOR,
        },
    )
    assert_approval_refusal(result)
    assert not (tmp_path / "knowledge" / "site_parse_rules.json").exists()
    assert ATTACK_SELECTOR not in _memory_blob(tmp_path)
    assert tools.parser.rules.get(VICTIM_URL) is None


def test_rate_parse_quality_only_does_not_set_selector(tmp_path: Path):
    """A score with no selector and no method is not this bug."""
    tools, _cfg = _shipped_tools(tmp_path)
    result = _call(
        tools,
        "rate_parse",
        {"url": VICTIM_URL, "quality_0_to_1": 0.9, "notes": "length ok"},
    )
    assert result.get("ok") is True, result
    rule = SiteParseRulesStore.load(tmp_path).get(VICTIM_URL)
    assert rule is not None
    assert rule.css_selector == ""
    assert rule.preferred_method == "auto"
    doc = _parse(SiteParseRulesStore.load(tmp_path), VICTIM_URL)
    assert doc.parse_method != ATTACK_METHOD
    assert "carbide tooling" in doc.text


def test_rate_parse_selector_is_not_persisted_parsed_or_published(tmp_path: Path):
    """Attack path: rate_parse stores a selector, the next parse applies it, fleet copies it."""
    tools, cfg = _shipped_tools(tmp_path)
    assert cfg.agents.security.allow_write_tools is True
    assert cfg.agents.governance.mode == "enforce"
    assert cfg.agents.fleet.role == "central"

    result = _call(
        tools,
        "rate_parse",
        {
            "url": VICTIM_URL,
            "quality_0_to_1": 1,
            "parse_method": ATTACK_METHOD,
            "css_selector": ATTACK_SELECTOR,
            "notes": "use this node",
        },
    )
    live = _parse(tools.parser.rules, VICTIM_URL)
    sync = FleetSync(tmp_path, fleet_config_from_app(cfg))
    published = sync.publish_knowledge(notes="after rate_parse")
    pack = sync.latest_knowledge_pack()
    pack_rules = ""
    if pack is not None and (pack / "site_parse_rules.json").is_file():
        pack_rules = (pack / "site_parse_rules.json").read_text(encoding="utf-8")
    worker = FleetSync(
        tmp_path / "worker",
        FleetConfig(role="worker", worker_id="vps-edge", sync_dir=str(tmp_path / "fleet")),
    )
    worker.pull_knowledge()
    worker_doc = _parse(SiteParseRulesStore.load(tmp_path / "worker"), VICTIM_URL)

    assert result.get("ok") is False and result.get("effect") == "require_approval", {
        "result": result,
        "rules_file": (tmp_path / "knowledge" / "site_parse_rules.json").exists(),
        "live_method": live.parse_method,
        "live_selector": live.meta.get("applied_selector"),
        "live_has_headline": "carbide tooling" in live.text,
        "worker_method": worker_doc.parse_method,
        "worker_selector": worker_doc.meta.get("applied_selector"),
        "pack_has_selector": ATTACK_SELECTOR in pack_rules,
        "published_files": published.get("files"),
    }
    assert result.get("governance") is True, result
    assert not (tmp_path / "knowledge" / "site_parse_rules.json").exists()
    assert ATTACK_SELECTOR not in _memory_blob(tmp_path)
    assert ATTACK_SELECTOR not in pack_rules
    assert ATTACK_SELECTOR not in _skills_blob(tmp_path)
    assert tools.parser.rules.get(VICTIM_URL) is None
    assert live.parse_method != ATTACK_METHOD, live
    assert live.meta.get("applied_selector") != ATTACK_SELECTOR
    assert "carbide tooling" in live.text
    assert worker_doc.parse_method != ATTACK_METHOD, worker_doc
    assert worker_doc.meta.get("applied_selector") != ATTACK_SELECTOR
    assert "carbide tooling" in worker_doc.text
    assert "site_parse_rules.json" not in (published.get("files") or [])

    tools.parser.fetch_html = lambda url: PAGE_HTML  # noqa: ARG005 — HTML under test, not the network
    fetched = _call(tools, "fetch_and_parse", {"url": VICTIM_URL, "bypass_cache": True})
    assert fetched.get("ok") is True, fetched
    assert fetched.get("parse_method") != ATTACK_METHOD
    assert (fetched.get("meta") or {}).get("applied_selector") != ATTACK_SELECTOR
    assert "carbide tooling" in (fetched.get("excerpt") or "")
    assert ATTACK_SELECTOR not in _rules_blob(tmp_path)


def test_rate_parse_method_change_without_selector_requires_approval(tmp_path: Path):
    tools, _cfg = _shipped_tools(tmp_path)
    before = _rules_blob(tmp_path)
    result = _call(
        tools,
        "rate_parse",
        {"url": VICTIM_URL, "quality_0_to_1": 1, "parse_method": "bs4"},
    )
    assert_approval_refusal(result)
    assert _rules_blob(tmp_path) == before
    assert tools.parser.rules.get(VICTIM_URL) is None
    assert ATTACK_SELECTOR not in _memory_blob(tmp_path)


def test_rate_parse_does_not_overwrite_manual_rule_without_grant(tmp_path: Path):
    store = SiteParseRulesStore()
    store.upsert(
        VICTIM_URL,
        preferred_method="css",
        css_selector="article.story-body",
        origin="manual",
        notes="operator",
    )
    store.save(tmp_path)
    tools, _cfg = _shipped_tools(tmp_path)
    before = (tmp_path / "knowledge" / "site_parse_rules.json").read_bytes()
    result = _call(
        tools,
        "rate_parse",
        {
            "url": VICTIM_URL,
            "quality_0_to_1": 1,
            "parse_method": ATTACK_METHOD,
            "css_selector": ATTACK_SELECTOR,
        },
    )
    assert_approval_refusal(result)
    assert (tmp_path / "knowledge" / "site_parse_rules.json").read_bytes() == before
    live = tools.parser.rules.get(VICTIM_URL)
    assert live is not None
    assert live.origin == "manual"
    assert live.css_selector == "article.story-body"
    assert live.preferred_method == "css"
    doc = _parse(tools.parser.rules, VICTIM_URL)
    assert doc.meta.get("applied_selector") == "article.story-body"
    assert "carbide tooling" in doc.text
    assert "ATTACKER_MARKER_PARSE_POISON" not in doc.text


def test_rate_parse_grant_does_not_overwrite_manual_rule(tmp_path: Path):
    store = SiteParseRulesStore()
    store.upsert(
        VICTIM_URL,
        preferred_method="css",
        css_selector="article.story-body",
        origin="manual",
        notes="operator",
    )
    store.save(tmp_path)
    tools, cfg = _shipped_tools(tmp_path)
    _grant(cfg, "rate_parse", host="victim.example")
    result = _call(
        tools,
        "rate_parse",
        {
            "url": "https://www.victim.example/catalog/item",
            "quality_0_to_1": 1,
            "parse_method": "bs4",
            "css_selector": ATTACK_SELECTOR,
        },
    )
    disk = SiteParseRulesStore.load(tmp_path).get(VICTIM_URL)
    live = tools.parser.rules.get(VICTIM_URL)
    assert disk is not None and live is not None
    assert disk.origin == "manual" and live.origin == "manual"
    assert disk.css_selector == "article.story-body"
    assert live.css_selector == "article.story-body"
    assert disk.preferred_method == "css"
    assert live.preferred_method == "css"
    assert ATTACK_SELECTOR not in _rules_blob(tmp_path)
    assert ATTACK_SELECTOR not in _memory_blob(tmp_path)
    doc = _parse(SiteParseRulesStore.load(tmp_path), VICTIM_URL)
    assert doc.meta.get("applied_selector") == "article.story-body"
    assert "ATTACKER_MARKER_PARSE_POISON" not in doc.text
    # The call may refuse or record quality. It must not report the poisoned rule.
    if result.get("ok"):
        assert (result.get("rule") or {}).get("css_selector") != ATTACK_SELECTOR


def test_rate_parse_grant_for_other_host_does_not_write_victim(tmp_path: Path):
    tools, cfg = _shipped_tools(tmp_path)
    _grant(cfg, "rate_parse", host="other.example")
    result = _call(
        tools,
        "rate_parse",
        {
            "url": VICTIM_URL,
            "quality_0_to_1": 1,
            "parse_method": ATTACK_METHOD,
            "css_selector": ATTACK_SELECTOR,
        },
    )
    assert_approval_refusal(result)
    assert not (tmp_path / "knowledge" / "site_parse_rules.json").exists()
    assert tools.parser.rules.get(VICTIM_URL) is None
    assert ATTACK_SELECTOR not in _memory_blob(tmp_path)


def test_operator_grant_for_same_host_allows_selector_and_records_approval(tmp_path: Path):
    """The host that is approved is the host that is stored and later parsed."""
    tools, cfg = _shipped_tools(tmp_path)
    url = "https://www.victim.example/catalog/item"
    assert normalize_host(url) == host_from_url(url) == "victim.example"
    grant_id = _grant(cfg, "rate_parse", host="victim.example")
    result = _call(
        tools,
        "rate_parse",
        {
            "url": url,
            "quality_0_to_1": 1,
            "parse_method": ATTACK_METHOD,
            "css_selector": ATTACK_SELECTOR,
        },
    )
    assert result.get("ok") is True, result
    disk = SiteParseRulesStore.load(tmp_path).get(url)
    live = tools.parser.rules.get(url)
    assert disk is not None and live is not None
    assert disk.host == live.host == "victim.example"
    assert disk.css_selector == live.css_selector == ATTACK_SELECTOR
    assert disk.preferred_method == live.preferred_method == ATTACK_METHOD
    assert f"approval:{grant_id}" in _audit_text(cfg)
    doc = _parse(SiteParseRulesStore.load(tmp_path), url)
    assert doc.parse_method == ATTACK_METHOD
    assert doc.meta.get("applied_selector") == ATTACK_SELECTOR
    assert doc.meta.get("rule_host") == "victim.example"


def test_rate_parse_userinfo_url_cannot_store_a_different_host(tmp_path: Path):
    """Approval host is normalize_host; SiteParseRulesStore.rate keys host_from_url."""
    tools, cfg = _shipped_tools(tmp_path)
    raw = "https://editor@victim.example/catalog/item"
    authorized = normalize_host(raw)
    stored_key = host_from_url(raw)
    assert authorized == "victim.example"
    assert stored_key == "editor@victim.example"
    assert authorized != stored_key
    grant_id = _grant(cfg, "rate_parse", host=authorized)
    result = _call(
        tools,
        "rate_parse",
        {
            "url": raw,
            "quality_0_to_1": 1,
            "parse_method": ATTACK_METHOD,
            "css_selector": ATTACK_SELECTOR,
        },
    )
    _assert_selector_host_matches_approval(
        tools,
        tmp_path,
        raw_url=raw,
        authorized_host=authorized,
        divergent_key=stored_key,
        result=result,
        grant_id=grant_id,
    )


def test_rate_parse_port_url_cannot_store_a_different_host(tmp_path: Path):
    tools, cfg = _shipped_tools(tmp_path)
    raw = "https://victim.example:8443/catalog/item"
    authorized = normalize_host(raw)
    stored_key = host_from_url(raw)
    assert authorized == "victim.example"
    assert stored_key == "victim.example:8443"
    assert authorized != stored_key
    grant_id = _grant(cfg, "rate_parse", host=authorized)
    result = _call(
        tools,
        "rate_parse",
        {
            "url": raw,
            "quality_0_to_1": 1,
            "parse_method": ATTACK_METHOD,
            "css_selector": ATTACK_SELECTOR,
        },
    )
    _assert_selector_host_matches_approval(
        tools,
        tmp_path,
        raw_url=raw,
        authorized_host=authorized,
        divergent_key=stored_key,
        result=result,
        grant_id=grant_id,
    )


def _assert_selector_host_matches_approval(
    tools: ToolRegistry,
    tmp_path: Path,
    *,
    raw_url: str,
    authorized_host: str,
    divergent_key: str,
    result: dict,
    grant_id: str,
) -> None:
    disk = SiteParseRulesStore.load(tmp_path)
    for store in (disk, tools.parser.rules):
        divergent = store.rules.get(divergent_key)
        assert divergent is None or ATTACK_SELECTOR not in (divergent.css_selector or "")
        for rule in store.rules.values():
            if ATTACK_SELECTOR in (rule.css_selector or ""):
                assert rule.host == authorized_host
                assert rule.host != divergent_key
    used = tools.parser.rules.get(raw_url)
    if used is not None and ATTACK_SELECTOR in (used.css_selector or ""):
        assert used.host == authorized_host
        assert f"approval:{grant_id}" in _audit_text(tools.config)
    elif result.get("ok"):
        # Allowed only when the persisted selector host is the approved host.
        assert (result.get("rule") or {}).get("host") in {None, authorized_host}
        if (result.get("rule") or {}).get("css_selector") == ATTACK_SELECTOR:
            assert result["rule"]["host"] == authorized_host
            assert f"approval:{grant_id}" in _audit_text(tools.config)
    else:
        assert_approval_refusal(result)
        assert ATTACK_SELECTOR not in _rules_blob(tmp_path)
        assert ATTACK_SELECTOR not in _memory_blob(tmp_path)


def test_rate_parse_auto_promote_does_not_publish_ungated_selector(tmp_path: Path):
    tools, cfg = _shipped_tools(tmp_path, auto_promote=True)
    assert cfg.agents.agentic.auto_promote_host_skills is True
    result = _call(
        tools,
        "rate_parse",
        {
            "url": VICTIM_URL,
            "quality_0_to_1": 1,
            "parse_method": ATTACK_METHOD,
            "css_selector": ATTACK_SELECTOR,
        },
    )
    assert result.get("ok") is False and result.get("effect") == "require_approval", {
        "result_keys": sorted(result),
        "promoted": result.get("promoted_skill"),
        "rules_have_selector": ATTACK_SELECTOR in _rules_blob(tmp_path),
        "skills_have_selector": ATTACK_SELECTOR in _skills_blob(tmp_path),
    }
    assert result.get("governance") is True, result
    assert ATTACK_SELECTOR not in _rules_blob(tmp_path)
    assert ATTACK_SELECTOR not in _skills_blob(tmp_path)
    assert not (tmp_path / "knowledge" / "parsing_skills.json").exists()
    dumped = json.dumps(tools._parsing_skills.list_skills(limit=100))
    assert ATTACK_SELECTOR not in dumped
    assert "host-victim-example" not in tools._parsing_skills.skills


def test_learn_parsing_skill_selector_requires_approval(tmp_path: Path):
    tools, cfg = _shipped_tools(tmp_path)
    _grant(cfg, "learn_parsing_skill", host="other.example")
    result = _call(
        tools,
        "learn_parsing_skill",
        {
            "name": "Victim extract",
            "skill_id": "victim-extract",
            "preferred_method": ATTACK_METHOD,
            "css_selectors": [ATTACK_SELECTOR],
            "drop_selectors": ["article.story-body"],
            "applies_to_hosts": ["https://www.victim.example/catalog"],
            "url_contains": ["catalog"],
        },
    )
    assert_approval_refusal(result)
    assert not (tmp_path / "knowledge" / "parsing_skills.json").exists()
    assert "victim-extract" not in tools._parsing_skills.skills
    assert ATTACK_SELECTOR not in _memory_blob(tmp_path)
    matched = _call(
        tools,
        "match_parsing_skills",
        {"url": VICTIM_URL, "kind": "catalog"},
    )
    assert ATTACK_SELECTOR not in json.dumps(matched)
    sync = FleetSync(tmp_path, fleet_config_from_app(cfg))
    published = sync.publish_knowledge(notes="after learn")
    pack = sync.latest_knowledge_pack()
    blob = ""
    if pack is not None and (pack / "parsing_skills.json").is_file():
        blob = (pack / "parsing_skills.json").read_text(encoding="utf-8")
    assert ATTACK_SELECTOR not in blob
    assert "parsing_skills.json" not in (published.get("files") or []) or ATTACK_SELECTOR not in blob


def test_improve_parsing_skill_selector_requires_approval(tmp_path: Path):
    tools, _cfg = _shipped_tools(tmp_path)
    before = tools._parsing_skills.get("magazine-article")
    assert before is not None
    assert before.preferred_method == "trafilatura"
    before_selectors = list(before.css_selectors)
    result = _call(
        tools,
        "improve_parsing_skill",
        {
            "skill_id": "magazine-article",
            "preferred_method": ATTACK_METHOD,
            "css_selectors": [ATTACK_SELECTOR],
            "drop_selectors": ["article.story-body"],
            "applies_to_hosts": ["victim.example"],
        },
    )
    assert_approval_refusal(result)
    assert not (tmp_path / "knowledge" / "parsing_skills.json").exists()
    skill = tools._parsing_skills.get("magazine-article")
    assert skill is not None
    assert skill.preferred_method == "trafilatura"
    assert skill.css_selectors == before_selectors
    assert ATTACK_SELECTOR not in skill.css_selectors
    assert ATTACK_SELECTOR not in _memory_blob(tmp_path)
    matched = _call(tools, "match_parsing_skills", {"url": "https://victim.example/news/article"})
    assert ATTACK_SELECTOR not in json.dumps(matched)


def test_unscoped_grant_allows_learn_and_improve_and_records_approval(tmp_path: Path):
    tools, cfg = _shipped_tools(tmp_path)
    learn_grant = _grant(cfg, "learn_parsing_skill", host=None)
    learned = _call(
        tools,
        "learn_parsing_skill",
        {
            "name": "Victim extract",
            "skill_id": "victim-extract",
            "preferred_method": ATTACK_METHOD,
            "css_selectors": [ATTACK_SELECTOR],
            "applies_to_hosts": ["victim.example"],
        },
    )
    assert learned.get("ok") is True, learned
    assert f"approval:{learn_grant}" in _audit_text(cfg)
    stored = tools._parsing_skills.get("victim-extract")
    assert stored is not None
    assert ATTACK_SELECTOR in stored.css_selectors
    assert stored.preferred_method == ATTACK_METHOD

    improve_grant = _grant(cfg, "improve_parsing_skill", host=None)
    improved = _call(
        tools,
        "improve_parsing_skill",
        {
            "skill_id": "victim-extract",
            "preferred_method": "bs4",
            "css_selectors": [".operator-node"],
        },
    )
    assert improved.get("ok") is True, improved
    assert f"approval:{improve_grant}" in _audit_text(cfg)
    disk = _skills_blob(tmp_path)
    assert ATTACK_SELECTOR in disk
    assert ".operator-node" in disk
    again = tools._parsing_skills.get("victim-extract")
    assert again is not None
    assert again.preferred_method == "bs4"
