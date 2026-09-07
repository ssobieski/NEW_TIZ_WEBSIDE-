"""MVP offline bootstrap / acceptance."""

from __future__ import annotations

import json
from pathlib import Path

from market_agents.mvp import bootstrap_mvp, mvp_acceptance, mvp_status


def test_mvp_bootstrap_acceptance(tmp_path: Path):
    data = tmp_path / "mvp"
    result = bootstrap_mvp(
        data,
        config_path="config/mvp.example.yaml",
        reset=True,
    )
    assert result["ok"] is True
    assert result["acceptance"]["ok"] is True
    assert result["acceptance"]["counts"]["profiles"] >= 5
    assert result["acceptance"]["counts"]["crm_open"] >= 1
    assert result["sample_prospect"]["company"] == "Beta Precision CNC"
    assert (result["sample_prospect"].get("opportunity") or 0) >= 50
    assert result["fleet"].get("ok") is True
    assert result["run"]["mode"] == "collect_only"

    # artifacts
    assert (data / "export" / "knowledge_export.md").is_file()
    assert (data / "metrics" / "digest_latest.md").is_file()
    assert (data / "knowledge" / "run_status.json").is_file()

    status = mvp_status(data)
    assert status["acceptance"]["ok"] is True


def test_mvp_config_loads():
    from market_agents.config import load_config

    cfg = load_config("config/mvp.example.yaml")
    assert cfg.agents.fleet.role == "central"
    assert cfg.sources.notion.enabled is False
    assert cfg.agents.agentic.enabled is False
    assert str(cfg.data_path).endswith("mvp") or "mvp" in str(cfg.data_path)


def test_mvp_acceptance_fails_empty(tmp_path: Path):
    acc = mvp_acceptance(tmp_path)
    assert acc["ok"] is False
    assert acc["failed"]
