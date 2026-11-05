"""Host applicability for shared parsing skills must follow DNS label boundaries.

A skill stored for a registrable domain (sandvik.com) may apply to that host and
to its subdomains. It must not apply to a lookalike that merely ends with the
same characters (notsandvik.com), and a bare public suffix (com, co.uk) must
not apply to every host under that suffix.

These tests exercise ParsingSkillsStore.match and the tool path that returns
css_selectors and hints to the agent. They fail while matching uses a raw
string suffix.
"""

from __future__ import annotations

import json
from pathlib import Path

from market_agents.config import (
    AgenticConfig,
    AgentsConfig,
    AppConfig,
    GovernanceConfig,
    IndustryConfig,
)
from market_agents.memory import MarketMemory
from market_agents.parsing_skills import ParsingSkill, ParsingSkillsStore
from market_agents.tools import ToolRegistry

VICTIM_SELECTOR = ".victim-grade-table"
VICTIM_HINT = "follow the victim cutting-data procedure"
PROMOTED_SELECTOR = ".promoted-victim-selector"
IMPROVED_SELECTOR = ".improved-second-host-selector"
PUBLIC_COM_SELECTOR = ".public-suffix-com-selector"
PUBLIC_DOT_COM_SELECTOR = ".public-suffix-dot-com-selector"
PUBLIC_CO_UK_SELECTOR = ".public-suffix-co-uk-selector"
UK_REGISTRABLE_SELECTOR = ".uk-registrable-selector"


def _markers(rows: list[dict], selector: str, hint: str | None = None) -> list[dict]:
    found: list[dict] = []
    for row in rows:
        selectors = list(row.get("css_selectors") or [])
        hints = list(row.get("hints") or [])
        if selector in selectors or (hint is not None and hint in hints):
            found.append(row)
    return found


def _assert_procedure_absent(rows: list[dict], selector: str, hint: str | None = None) -> None:
    leaked = _markers(rows, selector, hint)
    assert leaked == [], [
        {
            "id": row.get("id"),
            "css_selectors": row.get("css_selectors"),
            "hints": row.get("hints"),
            "applies_to_hosts": row.get("applies_to_hosts"),
            "match_score": row.get("match_score"),
        }
        for row in leaked
    ]


def _assert_procedure_present(rows: list[dict], selector: str, hint: str | None = None) -> None:
    assert _markers(rows, selector, hint), rows


def _skill(
    skill_id: str,
    host: str,
    selector: str,
    hint: str | None = None,
) -> ParsingSkill:
    return ParsingSkill(
        id=skill_id,
        name=skill_id,
        category="catalog_hub",
        preferred_method="css",
        css_selectors=[selector],
        hints=[hint] if hint else [],
        applies_to_hosts=[host],
        shared=True,
    )


def _load(tmp_path: Path, skills: list[ParsingSkill]) -> ParsingSkillsStore:
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    payload = {"skills": [skill.to_dict() for skill in skills]}
    (knowledge / "parsing_skills.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return ParsingSkillsStore.load(data_dir=tmp_path, with_seeds=False)


def _tools(tmp_path: Path) -> ToolRegistry:
    cfg = AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cutting"]),
        agents=AgentsConfig(
            data_dir=str(tmp_path),
            agentic=AgenticConfig(learn_parsing_skills=True, learn_parse_rules=False),
            governance=GovernanceConfig(
                enabled=True,
                audit_dir=str(tmp_path / "governance"),
                policy_pack="config/governance/tiz.policy.yaml",
            ),
        ),
    )
    return ToolRegistry(cfg, MarketMemory(tmp_path))


def test_reloaded_registrable_host_ignores_lookalike_labels(tmp_path: Path):
    """A skill already on disk must not be returned for a glued-label lookalike."""
    store = _load(
        tmp_path,
        [
            _skill("victim-sandvik", "sandvik.com", VICTIM_SELECTOR, VICTIM_HINT),
            _skill("victim-uk", "company.co.uk", UK_REGISTRABLE_SELECTOR, "uk procedure"),
            _skill("victim-short-suffix", "vik.com", ".short-suffix-selector", "short suffix"),
        ],
    )

    lookalikes = [
        "https://notsandvik.com/z",
        "https://WWW.NotSandvik.com/z",
        "https://notsandvik.com/sandvik.com",
        "https://xsandvik.com/z",
        "https://sandvik.com.evil.example/z",
        "https://evil.example/sandvik.com",
    ]
    for url in lookalikes:
        rows = store.match(url=url, limit=50)
        _assert_procedure_absent(rows, VICTIM_SELECTOR, VICTIM_HINT)

    uk_rows = store.match(url="https://notcompany.co.uk/z", limit=50)
    _assert_procedure_absent(uk_rows, UK_REGISTRABLE_SELECTOR, "uk procedure")
    other_uk = store.match(url="https://other.co.uk/z", limit=50)
    _assert_procedure_absent(other_uk, UK_REGISTRABLE_SELECTOR, "uk procedure")

    short = store.match(url="https://sandvik.com/z", limit=50)
    _assert_procedure_absent(short, ".short-suffix-selector", "short suffix")


def test_reloaded_registrable_host_matches_exact_host_and_subdomains(tmp_path: Path):
    store = _load(
        tmp_path,
        [
            _skill("victim-sandvik", "sandvik.com", VICTIM_SELECTOR, VICTIM_HINT),
            _skill("victim-uk", "company.co.uk", UK_REGISTRABLE_SELECTOR, "uk procedure"),
        ],
    )
    for url in (
        "https://sandvik.com/z",
        "https://www.sandvik.com/z",
        "https://tools.sandvik.com/z",
        "https://cdn.assets.sandvik.com/z",
    ):
        rows = store.match(url=url, limit=50)
        _assert_procedure_present(rows, VICTIM_SELECTOR, VICTIM_HINT)

    uk_rows = store.match(url="https://shop.company.co.uk/z", limit=50)
    _assert_procedure_present(uk_rows, UK_REGISTRABLE_SELECTOR, "uk procedure")
    exact_uk = store.match(url="https://company.co.uk/z", limit=50)
    _assert_procedure_present(exact_uk, UK_REGISTRABLE_SELECTOR, "uk procedure")


def test_reloaded_public_suffix_does_not_match_unrelated_hosts(tmp_path: Path):
    """Bare public suffixes already stored must not score as host applicability."""
    store = _load(
        tmp_path,
        [
            _skill("suffix-com", "com", PUBLIC_COM_SELECTOR, "com procedure"),
            _skill("suffix-dot-com", ".com", PUBLIC_DOT_COM_SELECTOR, "dot com procedure"),
            _skill("suffix-co-uk", "co.uk", PUBLIC_CO_UK_SELECTOR, "co.uk procedure"),
        ],
    )
    probes = [
        ("https://example.com/z", PUBLIC_COM_SELECTOR, "com procedure"),
        ("https://notsandvik.com/z", PUBLIC_COM_SELECTOR, "com procedure"),
        ("https://example.com/z", PUBLIC_DOT_COM_SELECTOR, "dot com procedure"),
        ("https://attacker.co.uk/z", PUBLIC_CO_UK_SELECTOR, "co.uk procedure"),
        ("https://shop.example.co.uk/z", PUBLIC_CO_UK_SELECTOR, "co.uk procedure"),
    ]
    for url, selector, hint in probes:
        rows = store.match(url=url, limit=50)
        _assert_procedure_absent(rows, selector, hint)


def test_learn_improve_and_promote_use_the_same_host_boundary(tmp_path: Path):
    store = ParsingSkillsStore()
    learned = store.learn(
        name="Victim catalog",
        category="catalog_hub",
        preferred_method="css",
        css_selectors=[VICTIM_SELECTOR],
        hints=[VICTIM_HINT],
        applies_to_hosts=["https://www.sandvik.com/catalog"],
        skill_id="learned-sandvik",
    )
    assert learned["ok"] is True

    promoted = store.promote_host_rule(
        "https://www.secotools.com/products",
        preferred_method="css",
        css_selector=PROMOTED_SELECTOR,
        category="catalog_hub",
    )
    assert promoted["ok"] is True

    improved = store.improve(
        "learned-sandvik",
        applies_to_hosts=["walter-tools.com"],
        css_selectors=[IMPROVED_SELECTOR],
    )
    assert improved["ok"] is True

    lookalike = store.match(url="https://notsandvik.com/z", limit=50)
    _assert_procedure_absent(lookalike, VICTIM_SELECTOR, VICTIM_HINT)
    _assert_procedure_absent(lookalike, IMPROVED_SELECTOR)

    glued_second = store.match(url="https://notwalter-tools.com/z", limit=50)
    _assert_procedure_absent(glued_second, IMPROVED_SELECTOR)
    _assert_procedure_absent(glued_second, VICTIM_SELECTOR, VICTIM_HINT)

    promoted_lookalike = store.match(url="https://notsecotools.com/z", limit=50)
    _assert_procedure_absent(promoted_lookalike, PROMOTED_SELECTOR)

    exact = store.match(url="https://sandvik.com/z", limit=50)
    _assert_procedure_present(exact, VICTIM_SELECTOR, VICTIM_HINT)
    subdomain = store.match(url="https://shop.walter-tools.com/z", limit=50)
    _assert_procedure_present(subdomain, IMPROVED_SELECTOR)
    promoted_sub = store.match(url="https://www.shop.secotools.com/z", limit=50)
    _assert_procedure_present(promoted_sub, PROMOTED_SELECTOR)


def test_tool_match_parsing_skills_does_not_hand_over_lookalike_procedure(tmp_path: Path):
    """The agent-facing tool must return the victim selectors only for that host."""
    tools = _tools(tmp_path)
    learned = tools.call(
        "learn_parsing_skill",
        {
            "name": "Victim catalog",
            "skill_id": "tool-victim-sandvik",
            "category": "catalog_hub",
            "preferred_method": "css",
            "css_selectors": [VICTIM_SELECTOR],
            "hints": [VICTIM_HINT],
            "applies_to_hosts": ["sandvik.com"],
        },
    )
    assert learned["ok"] is True

    promoted = tools.promote_host_skill(
        {
            "host_or_url": "https://sandvik.com/products",
            "preferred_method": "css",
            "css_selector": PROMOTED_SELECTOR,
            "category": "catalog_hub",
        }
    )
    assert promoted["ok"] is True

    public = tools.call(
        "learn_parsing_skill",
        {
            "name": "Public suffix com",
            "skill_id": "tool-public-com",
            "category": "generic",
            "preferred_method": "css",
            "css_selectors": [PUBLIC_COM_SELECTOR],
            "hints": ["com procedure"],
            "applies_to_hosts": ["com"],
        },
    )
    # Rejection at write time is acceptable. Keeping the suffix is acceptable
    # only when matching does not apply it.
    assert "ok" in public

    legit = tools.call(
        "match_parsing_skills",
        {"url": "https://tools.sandvik.com/z", "limit": 50},
    )
    assert legit["ok"] is True
    _assert_procedure_present(legit["skills"], VICTIM_SELECTOR, VICTIM_HINT)
    _assert_procedure_present(legit["skills"], PROMOTED_SELECTOR)

    attack = tools.call(
        "match_parsing_skills",
        {"url": "https://notsandvik.com/z", "limit": 50},
    )
    assert attack["ok"] is True
    _assert_procedure_absent(attack["skills"], VICTIM_SELECTOR, VICTIM_HINT)
    _assert_procedure_absent(attack["skills"], PROMOTED_SELECTOR)

    public_hit = tools.call(
        "match_parsing_skills",
        {"url": "https://example.com/z", "limit": 50},
    )
    assert public_hit["ok"] is True
    _assert_procedure_absent(public_hit["skills"], PUBLIC_COM_SELECTOR, "com procedure")

    reloaded = ParsingSkillsStore.load(data_dir=tmp_path, with_seeds=False)
    for url in ("https://notsandvik.com/z", "https://www.notsandvik.com/z"):
        rows = reloaded.match(url=url, limit=50)
        _assert_procedure_absent(rows, VICTIM_SELECTOR, VICTIM_HINT)
        _assert_procedure_absent(rows, PROMOTED_SELECTOR)
        _assert_procedure_absent(rows, PUBLIC_COM_SELECTOR, "com procedure")

    public_uk = tools.call(
        "learn_parsing_skill",
        {
            "name": "Public suffix uk",
            "skill_id": "tool-public-co-uk",
            "category": "generic",
            "preferred_method": "css",
            "css_selectors": [PUBLIC_CO_UK_SELECTOR],
            "hints": ["co.uk procedure"],
            "applies_to_hosts": ["co.uk"],
        },
    )
    assert "ok" in public_uk
    uk_hit = tools.call(
        "match_parsing_skills",
        {"url": "https://attacker.co.uk/z", "limit": 50},
    )
    assert uk_hit["ok"] is True
    _assert_procedure_absent(uk_hit["skills"], PUBLIC_CO_UK_SELECTOR, "co.uk procedure")
