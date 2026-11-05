"""Approval gate for shared parsing skills.

These tests exercise ToolRegistry.call, the path the parsing agent uses, against
the shipped governance pack. They fail while an unapproved call can plant CSS
selectors, hints, or notes into the shared parsing-skill store (or, for
rate_parse, into the host parse-rule store) and while a grant for one host
authorizes a write that later runs load for a different host.

A matching grant must still allow a write whose hosts are covered by that grant.
"""

from __future__ import annotations

import json
from pathlib import Path

from market_agents.approvals import ApprovalStore
from market_agents.config import (
    AgentsConfig,
    AppConfig,
    FleetConfig,
    GovernanceConfig,
    IndustryConfig,
    SourcesConfig,
)
from market_agents.fleet import FleetConfig as SyncFleetConfig
from market_agents.fleet import FleetSync
from market_agents.memory import MarketMemory
from market_agents.parsing_skills import ParsingSkillsStore
from market_agents.tools import ToolRegistry

POISON_CSS = "div.injected-parse-override"
POISON_HINT = "INJECTED-PARSING-HINT-26752937"
POISON_NOTES = "INJECTED-NOTES-26752937"
BENIGN_CSS = "article.victim-body"
BENIGN_HINT = "read the article body"


def _cfg(tmp: Path, **agentic: object) -> AppConfig:
    cfg = AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cutting"]),
        sources=SourcesConfig(),
        agents=AgentsConfig(
            data_dir=str(tmp),
            report_dir=str(tmp / "reports"),
            fleet=FleetConfig(role="central", sync_dir=str(tmp / "fleet")),
            governance=GovernanceConfig(
                enabled=True,
                mode="enforce",
                policy_pack="config/governance/tiz.policy.yaml",
                audit_dir=str(tmp / "governance"),
                deny_require_approval=True,
            ),
        ),
    )
    for key, value in agentic.items():
        setattr(cfg.agents.agentic, key, value)
    return cfg


def _tools(tmp: Path, **agentic: object) -> ToolRegistry:
    return ToolRegistry(_cfg(tmp, **agentic), MarketMemory(tmp))


def _grant(tmp: Path, tool: str, *, host: str | None) -> None:
    store = ApprovalStore.load(tmp / "governance")
    store.approve(tool, approved_by="operator", host=host)
    store.save(tmp / "governance")


def _read(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _knowledge_blob(tmp: Path) -> str:
    knowledge = tmp / "knowledge"
    return "\n".join(
        _read(knowledge / name)
        for name in ("parsing_skills.json", "site_parse_rules.json")
    )


def _memory_blob(tmp: Path) -> str:
    return _read(tmp / "memory.jsonl")


def _fleet_blob(tmp: Path) -> str:
    """Publish the central knowledge pack and pull it on a worker."""
    central = FleetSync(
        tmp,
        SyncFleetConfig(role="central", sync_dir=str(tmp / "fleet")),
    )
    published = central.publish_knowledge(notes="skill gate")
    chunks = [json.dumps(published)]
    pack = Path(str(published.get("path") or ""))
    if pack.is_dir():
        chunks.append(_read(pack / "parsing_skills.json"))
        chunks.append(_read(pack / "site_parse_rules.json"))
    worker_root = tmp / "worker-data"
    worker = FleetSync(
        worker_root,
        SyncFleetConfig(
            role="worker",
            sync_dir=str(tmp / "fleet"),
            worker_id="vps-edge",
        ),
    )
    pulled = worker.pull_knowledge()
    chunks.append(json.dumps(pulled))
    chunks.append(_knowledge_blob(worker_root))
    return "\n".join(chunks)


def _loaded_blob(tmp: Path) -> str:
    """What the next process loads from disk and would hand to a later run."""
    store = ParsingSkillsStore.load(data_dir=tmp, with_seeds=False)
    matched = store.match(url="https://evil.example/article", kind="magazine", limit=20)
    listed = store.list_skills(limit=100)
    return json.dumps({"matched": matched, "listed": listed})


def _live_blob(tools: ToolRegistry) -> str:
    matched = tools.call(
        "match_parsing_skills",
        {"url": "https://evil.example/article", "kind": "magazine", "limit": 20},
    )
    listed = tools.call("list_parsing_skills", {"limit": 100})
    doc = tools.parser.parse_html(
        "https://evil.example/article",
        (
            "<html><body><article><p>"
            + ("visible " * 40)
            + "</p><div class='injected-parse-override'><p>"
            + ("secret " * 40)
            + "</p></div></article></body></html>"
        ),
    )
    return json.dumps(
        {
            "matched": matched,
            "listed": listed,
            "applied_selector": (doc.meta or {}).get("applied_selector"),
            "parse_method": doc.parse_method,
        }
    )


def _assert_unapproved_write_rejected(
    tmp: Path,
    tools: ToolRegistry,
    out: dict,
    *,
    require_policy_effect: bool = True,
) -> None:
    """The call must be refused before any parsing instruction is stored or shipped."""
    problems: list[str] = []
    if out.get("ok") is not False:
        problems.append(f"call was allowed: {out!r}"[:500])
    if require_policy_effect and out.get("effect") != "require_approval":
        problems.append(f"effect={out.get('effect')!r} matched={out.get('matched_rules')!r}")
    for label, blob in (
        ("knowledge", _knowledge_blob(tmp)),
        ("memory", _memory_blob(tmp)),
        ("live", _live_blob(tools)),
        ("reloaded", _loaded_blob(tmp)),
        ("fleet", _fleet_blob(tmp)),
    ):
        if POISON_CSS in blob or POISON_HINT in blob or POISON_NOTES in blob:
            problems.append(f"{label} contains planted parsing instructions")
    applied = json.loads(_live_blob(tools))
    if applied.get("applied_selector") == POISON_CSS:
        problems.append("parser applied the planted CSS selector")
    assert not problems, "; ".join(problems)


def test_unapproved_promote_host_skill_is_blocked(tmp_path: Path):
    """Control: the existing gate already stops promote_host_skill."""
    tools = _tools(tmp_path)
    out = tools.call(
        "promote_host_skill",
        {
            "host_or_url": "https://evil.example/article",
            "css_selector": POISON_CSS,
            "notes": POISON_NOTES,
        },
    )
    _assert_unapproved_write_rejected(tmp_path, tools, out)


def test_unapproved_learn_parsing_skill_does_not_persist_or_publish(tmp_path: Path):
    tools = _tools(tmp_path)
    raw = json.dumps(
        {
            "name": "Injected article override",
            "skill_id": "injected-override",
            "category": "article",
            "preferred_method": "css",
            "css_selectors": [POISON_CSS],
            "hints": [POISON_HINT],
            "notes": POISON_NOTES,
            "applies_to_hosts": ["https://evil.example/article"],
            "applies_to_kinds": ["magazine"],
            "taught_by": "page",
        }
    )
    out = tools.call("learn_parsing_skill", raw)
    _assert_unapproved_write_rejected(tmp_path, tools, out)


def test_unapproved_improve_parsing_skill_does_not_persist_or_publish(tmp_path: Path):
    tools = _tools(tmp_path)
    out = tools.call(
        "improve_parsing_skill",
        {
            "skill_id": "magazine-article",
            "preferred_method": "css",
            "css_selectors": [POISON_CSS],
            "hints": [POISON_HINT],
            "notes": POISON_NOTES,
            "applies_to_hosts": ["https://evil.example/article"],
            "taught_by": "page",
        },
    )
    _assert_unapproved_write_rejected(tmp_path, tools, out)


def test_unapproved_rate_parsing_skill_does_not_persist_or_publish(tmp_path: Path):
    tools = _tools(tmp_path)
    out = tools.call(
        "rate_parsing_skill",
        {
            "skill_id": "magazine-article",
            "quality_0_to_1": 0.99,
            "notes": POISON_NOTES,
            "taught_by": "page",
        },
    )
    _assert_unapproved_write_rejected(tmp_path, tools, out)


def test_unapproved_rate_parse_does_not_plant_rule_or_promoted_skill(tmp_path: Path):
    tools = _tools(tmp_path, auto_promote_host_skills=True)
    out = tools.call(
        "rate_parse",
        {
            "url": "https://evil.example/article",
            "quality_0_to_1": 0.95,
            "parse_method": "css",
            "css_selector": POISON_CSS,
            "notes": POISON_NOTES,
        },
    )
    _assert_unapproved_write_rejected(tmp_path, tools, out)


def test_decoy_host_grant_does_not_authorize_learn_for_another_host(tmp_path: Path):
    """A grant for allowed.example must not cover a skill that applies to evil.example.

    host_or_url and url are the fields the approval lookup reads today. The
    skill that gets stored is applies_to_hosts, which is a different host.
    """
    _grant(tmp_path, "learn_parsing_skill", host="allowed.example")
    tools = _tools(tmp_path)
    out = tools.call(
        "learn_parsing_skill",
        {
            "name": "Decoy host override",
            "skill_id": "decoy-host-override",
            "category": "article",
            "preferred_method": "css",
            "css_selectors": [POISON_CSS],
            "hints": [POISON_HINT],
            "notes": POISON_NOTES,
            "applies_to_hosts": [
                "https://allowed.example/ok",
                "https://evil.example/article",
            ],
            "url_contains": ["https://"],
            "applies_to_kinds": ["magazine"],
            "host_or_url": "https://allowed.example/ok",
            "url": "https://allowed.example/ok",
            "taught_by": "page",
        },
    )
    _assert_unapproved_write_rejected(
        tmp_path, tools, out, require_policy_effect=False
    )


def test_decoy_host_grant_does_not_authorize_improve_of_another_skill(tmp_path: Path):
    _grant(tmp_path, "improve_parsing_skill", host="allowed.example")
    tools = _tools(tmp_path)
    out = tools.call(
        "improve_parsing_skill",
        {
            "skill_id": "magazine-article",
            "preferred_method": "css",
            "css_selectors": [POISON_CSS],
            "hints": [POISON_HINT],
            "notes": POISON_NOTES,
            "applies_to_hosts": ["https://evil.example/article"],
            "url_contains": ["https://"],
            "host_or_url": "https://allowed.example/ok",
            "url": "https://allowed.example/ok",
            "taught_by": "page",
        },
    )
    _assert_unapproved_write_rejected(
        tmp_path, tools, out, require_policy_effect=False
    )


def test_decoy_host_grant_does_not_authorize_rate_of_another_skill(tmp_path: Path):
    store = ParsingSkillsStore.load(data_dir=tmp_path, with_seeds=False)
    store.learn(
        name="Evil baseline",
        skill_id="evil-baseline",
        category="article",
        applies_to_hosts=["evil.example"],
        taught_by="seed",
    )
    store.save(tmp_path)
    _grant(tmp_path, "rate_parsing_skill", host="allowed.example")
    tools = _tools(tmp_path)
    before_knowledge = _knowledge_blob(tmp_path)
    before_memory = _memory_blob(tmp_path)
    out = tools.call(
        "rate_parsing_skill",
        {
            "skill_id": "evil-baseline",
            "quality_0_to_1": 0.99,
            "notes": POISON_NOTES,
            "url": "https://allowed.example/ok",
            "host_or_url": "https://allowed.example/ok",
            "taught_by": "page",
        },
    )
    problems: list[str] = []
    if out.get("ok") is not False:
        problems.append(f"call was allowed: {out!r}"[:500])
    if _knowledge_blob(tmp_path) != before_knowledge:
        problems.append("parsing skill file changed")
    if _memory_blob(tmp_path) != before_memory:
        problems.append("memory changed")
    if POISON_NOTES in _live_blob(tools) or POISON_NOTES in _loaded_blob(tmp_path):
        problems.append("reloaded skill carries the unapproved note")
    if POISON_NOTES in _fleet_blob(tmp_path):
        problems.append("fleet pack or worker pull carries the unapproved note")
    assert not problems, "; ".join(problems)


def test_promote_grant_does_not_authorize_learn(tmp_path: Path):
    _grant(tmp_path, "promote_host_skill", host=None)
    tools = _tools(tmp_path)
    out = tools.call(
        "learn_parsing_skill",
        {
            "name": "Wrong tool grant",
            "skill_id": "wrong-tool-grant",
            "category": "article",
            "css_selectors": [POISON_CSS],
            "hints": [POISON_HINT],
            "notes": POISON_NOTES,
            "applies_to_hosts": ["https://evil.example/article"],
        },
    )
    _assert_unapproved_write_rejected(tmp_path, tools, out)


def test_matching_host_grant_allows_learn_and_fleet_pull(tmp_path: Path):
    """The gate is an approval, not a ban: a grant for the written host must stick."""
    _grant(tmp_path, "learn_parsing_skill", host="victim.example")
    tools = _tools(tmp_path)
    out = tools.call(
        "learn_parsing_skill",
        {
            "name": "Victim article",
            "skill_id": "victim-article",
            "category": "article",
            "preferred_method": "css",
            "css_selectors": [BENIGN_CSS],
            "hints": [BENIGN_HINT],
            "applies_to_hosts": ["https://www.victim.example/news"],
            "host_or_url": "https://victim.example/news",
            "taught_by": "operator",
        },
    )
    assert out.get("ok") is True, out
    reloaded = ParsingSkillsStore.load(data_dir=tmp_path, with_seeds=False)
    skill = reloaded.get("victim-article")
    assert skill is not None
    assert skill.applies_to_hosts == ["victim.example"]
    assert BENIGN_CSS in skill.css_selectors
    assert BENIGN_HINT in skill.hints
    matched = reloaded.match(url="https://www.victim.example/news", kind="article")
    assert any(row.get("id") == "victim-article" for row in matched)
    fleet = _fleet_blob(tmp_path)
    assert "victim-article" in fleet
    assert BENIGN_CSS in fleet
    outsider = reloaded.match(url="https://evil.example/article", kind="magazine")
    assert all(row.get("id") != "victim-article" for row in outsider)


def test_matching_host_grant_allows_improve(tmp_path: Path):
    store = ParsingSkillsStore.load(data_dir=tmp_path, with_seeds=False)
    store.learn(
        name="Victim baseline",
        skill_id="victim-baseline",
        category="article",
        preferred_method="auto",
        applies_to_hosts=["victim.example"],
        taught_by="operator",
    )
    store.save(tmp_path)
    _grant(tmp_path, "improve_parsing_skill", host="victim.example")
    tools = _tools(tmp_path)
    out = tools.call(
        "improve_parsing_skill",
        {
            "skill_id": "victim-baseline",
            "preferred_method": "css",
            "css_selectors": [BENIGN_CSS],
            "hints": [BENIGN_HINT],
            "host_or_url": "https://victim.example/news",
            "taught_by": "operator",
        },
    )
    assert out.get("ok") is True, out
    reloaded = ParsingSkillsStore.load(data_dir=tmp_path, with_seeds=False)
    skill = reloaded.get("victim-baseline")
    assert skill is not None
    assert BENIGN_CSS in skill.css_selectors
    assert BENIGN_HINT in skill.hints
    assert skill.applies_to_hosts == ["victim.example"]
