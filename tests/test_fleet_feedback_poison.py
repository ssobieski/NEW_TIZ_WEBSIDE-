"""Worker feedback must not rewrite live parse rules or impose long crawl cooldowns.

These tests exercise absorb_feedback → publish_knowledge / auto-push → pull_knowledge
→ IntelligentParser and AdaptivePoliteFetcher. They fail while a worker-chosen
success_count replaces selector, method, and origin, and while crawl_health
keeps the worker's delay and cooldown_until.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from market_agents.agents.orchestrator import Orchestrator
from market_agents.config import (
    AgentsConfig,
    AppConfig,
    FleetConfig as AppFleetConfig,
    IndustryConfig,
    SourcesConfig,
)
from market_agents.fleet import FleetConfig, FleetSync
from market_agents.parsing import IntelligentParser
from market_agents.parse_rules import SiteParseRulesStore
from market_agents.polite_http import (
    AdaptivePoliteFetcher,
    CrawlBlockedError,
    CrawlPolicy,
)

# Far enough that max() with any real cooldown keeps the host blocked for years.
FAR_COOLDOWN = 4_102_444_800.0  # 2100-01-01T00:00:00Z
HUGE_COUNTER = 10**18
# A merged counter may stay at the central value or be capped. It must not
# keep an unbounded worker-supplied count.
MAX_STORED_COUNTER = 10**7

MANUAL_HOST = "manual.example"
LEARNED_HOST = "learned.example"
STATS_HOST = "stats.example"
NEW_HOST = "brand-new.example"

MANUAL_SELECTOR = "article.real"
LEARNED_SELECTOR = "main.content"
ATTACK_SELECTOR = ".attacker-payload"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _page_html() -> str:
    legit = "LEGITIMATE_BODY_MARKER " + ("catalog " * 40)
    learned = "LEARNED_BODY_MARKER " + ("catalog " * 40)
    attack = "ATTACKER_PAYLOAD_MARKER " + ("injected " * 40)
    return (
        "<html><body>"
        f'<main class="content">{learned}</main>'
        f'<article class="real">{legit}</article>'
        f'<div class="attacker-payload">{attack}</div>'
        "</body></html>"
    )


def _central_rules() -> dict:
    return {
        "rules": [
            {
                "host": MANUAL_HOST,
                "preferred_method": "css",
                "css_selector": MANUAL_SELECTOR,
                "drop_selectors": [],
                "min_chars": 40,
                "success_count": 5,
                "fail_count": 1,
                "origin": "manual",
                "notes": "operator rule",
            },
            {
                "host": LEARNED_HOST,
                "preferred_method": "css",
                "css_selector": LEARNED_SELECTOR,
                "drop_selectors": ["nav"],
                "min_chars": 40,
                "success_count": 3,
                "fail_count": 0,
                "origin": "agent",
                "notes": "learned rule",
            },
            {
                "host": STATS_HOST,
                "preferred_method": "bs4",
                "css_selector": "article",
                "drop_selectors": [],
                "min_chars": 80,
                "success_count": 4,
                "fail_count": 1,
                "origin": "agent",
            },
        ]
    }


def _central_health() -> dict:
    def row(host: str, delay: float) -> dict:
        return {
            "host": host,
            "success_count": 2,
            "fail_count": 0,
            "block_count": 0,
            "delay_seconds": delay,
            "cooldown_until": 0,
            "last_request_at": 1.0,
            "last_status": 200,
        }

    return {
        "hosts": [
            row(MANUAL_HOST, 2.0),
            row(LEARNED_HOST, 2.0),
            row(STATS_HOST, 2.0),
        ]
    }


def _worker_rules() -> dict:
    return {
        "rules": [
            {
                "host": MANUAL_HOST,
                "preferred_method": "css",
                "css_selector": ATTACK_SELECTOR,
                "drop_selectors": ["article.real"],
                "min_chars": 1,
                "success_count": HUGE_COUNTER,
                "fail_count": 0,
                "origin": "agent",
                "notes": "worker override",
            },
            {
                # Same score as central (3), higher success_count: tie-break overwrite.
                "host": LEARNED_HOST,
                "preferred_method": "css",
                "css_selector": ATTACK_SELECTOR,
                "drop_selectors": ["main.content"],
                "min_chars": 1,
                "success_count": 5,
                "fail_count": 2,
                "origin": "worker",
            },
            {
                # Lower score, so the merge takes the sum-counters branch.
                "host": STATS_HOST,
                "preferred_method": "css",
                "css_selector": ATTACK_SELECTOR,
                "drop_selectors": ["article"],
                "min_chars": 1,
                "success_count": HUGE_COUNTER,
                "fail_count": HUGE_COUNTER,
                "origin": "worker",
            },
        ]
    }


def _worker_health() -> dict:
    def row(host: str) -> dict:
        return {
            "host": host,
            "success_count": HUGE_COUNTER,
            "fail_count": HUGE_COUNTER,
            "block_count": HUGE_COUNTER,
            "delay_seconds": 1_000_000_000.0,
            "cooldown_until": FAR_COOLDOWN,
            "last_request_at": 2.0,
            "last_status": 429,
            "notes": "worker cooldown",
        }

    return {"hosts": [row(MANUAL_HOST), row(LEARNED_HOST), row(STATS_HOST), row(NEW_HOST)]}


def _rules_by_host(path: Path) -> dict[str, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {row["host"]: row for row in payload["rules"]}


def _health_by_host(path: Path) -> dict[str, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {row["host"]: row for row in payload["hosts"]}


def _assert_rule_unchanged(rule: dict, *, selector: str, method: str, origin: str, min_chars: int, drop: list[str]) -> None:
    assert rule["css_selector"] == selector
    assert rule["preferred_method"] == method
    assert rule["origin"] == origin
    assert rule["min_chars"] == min_chars
    assert list(rule.get("drop_selectors") or []) == drop
    assert int(rule["success_count"]) <= MAX_STORED_COUNTER
    assert int(rule["fail_count"]) <= MAX_STORED_COUNTER


def _assert_health_bounded(row: dict, *, policy: CrawlPolicy) -> None:
    assert float(row["cooldown_until"]) <= time.time() + float(policy.cooldown_on_block_seconds) + 1.0
    assert float(row["delay_seconds"]) <= float(policy.max_delay_seconds)
    assert int(row["success_count"]) <= MAX_STORED_COUNTER
    assert int(row["fail_count"]) <= MAX_STORED_COUNTER
    assert int(row["block_count"]) <= MAX_STORED_COUNTER


def _run_attack(tmp_path: Path) -> tuple[Path, Path, Path, dict, dict]:
    sync_dir = tmp_path / "fleet"
    central_data = tmp_path / "central"
    worker_data = tmp_path / "worker"
    _write_json(central_data / "knowledge" / "site_parse_rules.json", _central_rules())
    _write_json(central_data / "knowledge" / "crawl_health.json", _central_health())
    _write_json(worker_data / "knowledge" / "site_parse_rules.json", _worker_rules())
    _write_json(worker_data / "knowledge" / "crawl_health.json", _worker_health())

    central = FleetSync(
        central_data,
        FleetConfig(role="central", sync_dir=str(sync_dir), worker_id="gpu"),
    )
    worker = FleetSync(
        worker_data,
        FleetConfig(role="worker", sync_dir=str(sync_dir), worker_id="vps-attacker"),
    )
    pushed = worker.push_feedback(notes="worker feedback")
    assert pushed["ok"] is True
    assert "site_parse_rules.json" in pushed["files"]
    assert "crawl_health.json" in pushed["files"]

    absorbed = central.absorb_feedback()
    assert absorbed["ok"] is True
    assert absorbed["absorbed_packs"] == 1

    republished = central.publish_knowledge(notes="auto after absorb")
    assert republished["ok"] is True

    cfg = AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cnc"], competitors=[]),
        sources=SourcesConfig(),
        agents=AgentsConfig(
            data_dir=str(central_data),
            report_dir=str(central_data / "reports"),
            fleet=AppFleetConfig(
                role="central",
                sync_dir=str(sync_dir),
                auto_push_after_run=True,
            ),
        ),
    )
    auto = Orchestrator(cfg)._maybe_fleet_publish(mode="pipeline")
    assert auto.get("ok") is True

    other_data = tmp_path / "other-worker"
    other = FleetSync(
        other_data,
        FleetConfig(role="worker", sync_dir=str(sync_dir), worker_id="vps-other"),
    )
    pulled = other.pull_knowledge()
    assert pulled["ok"] is True
    assert "site_parse_rules.json" in pulled["files"]
    assert "crawl_health.json" in pulled["files"]
    return central_data, other_data, sync_dir, republished, auto


def test_worker_feedback_does_not_replace_parse_rules(tmp_path: Path) -> None:
    central_data, other_data, sync_dir, republished, auto = _run_attack(tmp_path)
    expected = {
        MANUAL_HOST: (MANUAL_SELECTOR, "css", "manual", 40, []),
        LEARNED_HOST: (LEARNED_SELECTOR, "css", "agent", 40, ["nav"]),
        STATS_HOST: ("article", "bs4", "agent", 80, []),
    }

    copies = {
        "central": central_data / "knowledge" / "site_parse_rules.json",
        "republish": sync_dir / "outbox" / "knowledge" / republished["pack_id"] / "site_parse_rules.json",
        "auto_push": sync_dir / "outbox" / "knowledge" / auto["pack_id"] / "site_parse_rules.json",
        "pulled": other_data / "knowledge" / "site_parse_rules.json",
    }
    for rules_path in copies.values():
        by_host = _rules_by_host(rules_path)
        for host, spec in expected.items():
            selector, method, origin, min_chars, drop = spec
            _assert_rule_unchanged(
                by_host[host],
                selector=selector,
                method=method,
                origin=origin,
                min_chars=min_chars,
                drop=drop,
            )

    store = SiteParseRulesStore.load(other_data)
    parser = IntelligentParser(rules=store, learn=False)
    html = _page_html()
    manual = parser.parse_html(f"https://{MANUAL_HOST}/catalog", html)
    learned = parser.parse_html(f"https://{LEARNED_HOST}/catalog", html)
    assert manual.meta["applied_selector"] == MANUAL_SELECTOR
    assert manual.parse_method == "css"
    assert "LEGITIMATE_BODY_MARKER" in manual.text
    assert "ATTACKER_PAYLOAD_MARKER" not in manual.text
    assert learned.meta["applied_selector"] == LEARNED_SELECTOR
    assert learned.parse_method == "css"
    assert "LEARNED_BODY_MARKER" in learned.text
    assert "ATTACKER_PAYLOAD_MARKER" not in learned.text


def test_worker_feedback_does_not_impose_far_cooldown_or_uncapped_delay(tmp_path: Path) -> None:
    central_data, other_data, sync_dir, republished, auto = _run_attack(tmp_path)
    policy = CrawlPolicy(
        respect_robots_txt=False,
        resolve_dns_for_ssrf=False,
        min_delay_seconds=0.0,
        jitter_seconds=0.0,
        adaptive=False,
    )
    copies = {
        "central": central_data / "knowledge" / "crawl_health.json",
        "republish": sync_dir / "outbox" / "knowledge" / republished["pack_id"] / "crawl_health.json",
        "auto_push": sync_dir / "outbox" / "knowledge" / auto["pack_id"] / "crawl_health.json",
        "pulled": other_data / "knowledge" / "crawl_health.json",
    }
    for path in copies.values():
        by_host = _health_by_host(path)
        for host in (MANUAL_HOST, LEARNED_HOST, STATS_HOST):
            _assert_health_bounded(by_host[host], policy=policy)
        if NEW_HOST in by_host:
            _assert_health_bounded(by_host[NEW_HOST], policy=policy)

    AdaptivePoliteFetcher.reset_shared()
    fetcher = AdaptivePoliteFetcher(policy=policy, data_dir=other_data)
    bound = float(policy.cooldown_on_block_seconds) + 1.0
    for host in (MANUAL_HOST, LEARNED_HOST, STATS_HOST, NEW_HOST):
        state = fetcher._hosts.get(host)
        if state is None:
            continue
        try:
            fetcher._wait_slot(host)
        except CrawlBlockedError as exc:
            remaining = float(state.cooldown_until) - time.time()
            assert remaining <= bound, exc
