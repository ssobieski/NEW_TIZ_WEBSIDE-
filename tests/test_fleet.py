from __future__ import annotations

import json
from pathlib import Path

from market_agents.fleet import FleetConfig, FleetSync


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_publish_pull_push_absorb_roundtrip(tmp_path: Path):
    sync_dir = tmp_path / "fleet"
    central_data = tmp_path / "central"
    worker_data = tmp_path / "worker"

    _write_json(
        central_data / "knowledge" / "parsing_skills.json",
        {"skills": [{"id": "pdf-catalog", "title": "PDF catalog", "success_count": 3}]},
    )
    _write_json(
        central_data / "knowledge" / "site_parse_rules.json",
        {
            "rules": [
                {
                    "host": "sandvik.coromant.com",
                    "css_selector": "article",
                    "success_count": 5,
                    "fail_count": 0,
                }
            ]
        },
    )
    _write_json(
        central_data / "knowledge" / "crawl_health.json",
        {
            "hosts": [
                {
                    "host": "sandvik.coromant.com",
                    "success_count": 2,
                    "fail_count": 0,
                    "block_count": 0,
                    "delay_seconds": 2.0,
                    "cooldown_until": 0,
                    "last_request_at": 1.0,
                    "last_status": 200,
                }
            ]
        },
    )

    central = FleetSync(
        central_data,
        FleetConfig(role="central", sync_dir=str(sync_dir), worker_id="gpu"),
    )
    published = central.publish_knowledge(notes="test pack")
    assert published["ok"] is True
    assert "parsing_skills.json" in published["files"]
    assert "site_parse_rules.json" in published["files"]

    worker = FleetSync(
        worker_data,
        FleetConfig(role="worker", sync_dir=str(sync_dir), worker_id="vps-1"),
    )
    pulled = worker.pull_knowledge()
    assert pulled["ok"] is True
    assert (worker_data / "knowledge" / "parsing_skills.json").exists()
    assert (worker_data / "knowledge" / "fleet_pulled.json").exists()

    # lokalny feedback z VPS — gorszy delay i nowy host
    _write_json(
        worker_data / "knowledge" / "crawl_health.json",
        {
            "hosts": [
                {
                    "host": "sandvik.coromant.com",
                    "success_count": 4,
                    "fail_count": 1,
                    "block_count": 1,
                    "delay_seconds": 8.0,
                    "cooldown_until": 99.0,
                    "last_request_at": 10.0,
                    "last_status": 429,
                    "notes": "rate limited",
                },
                {
                    "host": "new-oem.example",
                    "success_count": 1,
                    "fail_count": 0,
                    "block_count": 0,
                    "delay_seconds": 1.5,
                    "cooldown_until": 0,
                    "last_request_at": 5.0,
                    "last_status": 200,
                },
            ]
        },
    )
    _write_json(
        worker_data / "knowledge" / "site_parse_rules.json",
        {
            "rules": [
                {
                    "host": "new-oem.example",
                    "css_selector": ".product",
                    "success_count": 3,
                    "fail_count": 0,
                }
            ]
        },
    )
    (worker_data / "items.jsonl").write_text(
        '{"id":"1"}\n{"id":"2"}\n', encoding="utf-8"
    )

    pushed = worker.push_feedback(notes="vps feedback")
    assert pushed["ok"] is True
    assert pushed["worker_id"] == "vps-1"
    assert "crawl_health.json" in pushed["files"]
    assert "items_tail.jsonl" in pushed["files"]

    st = central.status()
    assert st["feedback_pending"] == 1
    assert st["latest_knowledge"] == published["pack_id"]

    absorbed = central.absorb_feedback()
    assert absorbed["ok"] is True
    assert absorbed["absorbed_packs"] == 1
    assert absorbed["merged_hosts"] >= 1
    assert absorbed["merged_rules"] >= 1

    # drugi absorb nie powinien ponownie liczyć tego samego packa
    again = central.absorb_feedback()
    assert again["absorbed_packs"] == 0

    crawl = json.loads(
        (central_data / "knowledge" / "crawl_health.json").read_text(encoding="utf-8")
    )
    hosts = {h["host"]: h for h in crawl["hosts"]}
    assert hosts["sandvik.coromant.com"]["delay_seconds"] == 8.0
    assert hosts["sandvik.coromant.com"]["success_count"] == 6  # 2+4
    assert "new-oem.example" in hosts

    rules = json.loads(
        (central_data / "knowledge" / "site_parse_rules.json").read_text(encoding="utf-8")
    )
    by_host = {r["host"]: r for r in rules["rules"]}
    assert "new-oem.example" in by_host
    assert by_host["new-oem.example"]["css_selector"] == ".product"

    assert central.status()["feedback_pending"] == 0


def test_worker_cannot_publish(tmp_path: Path):
    sync = FleetSync(
        tmp_path,
        FleetConfig(role="worker", sync_dir=str(tmp_path / "fleet"), worker_id="vps-x"),
    )
    result = sync.publish_knowledge()
    assert result["ok"] is False


def test_fleet_config_from_app_defaults():
    from types import SimpleNamespace

    from market_agents.fleet import fleet_config_from_app

    cfg = SimpleNamespace(agents=SimpleNamespace(fleet=None))
    fleet = fleet_config_from_app(cfg)
    assert fleet.role == "central"
    assert fleet.sync_dir == "data/fleet"
