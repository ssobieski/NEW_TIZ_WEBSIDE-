"""LLM config env overrides for Dell over Cybertech VPN."""

from __future__ import annotations

from market_agents.config import load_config


def test_vllm_base_url_env_override(monkeypatch):
    monkeypatch.setenv("VLLM_BASE_URL", "http://10.20.30.40:8000/")
    monkeypatch.setenv("VLLM_MODEL", "Qwen/Qwen2.5-32B-Instruct")
    cfg = load_config("config/dell_vpn.example.yaml")
    assert cfg.llm.base_url == "http://10.20.30.40:8000"
    assert cfg.llm.model == "Qwen/Qwen2.5-32B-Instruct"


def test_market_agents_llm_base_url_alias(monkeypatch):
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    monkeypatch.setenv("MARKET_AGENTS_LLM_BASE_URL", "http://192.168.100.5:8000")
    cfg = load_config("config/dell_a100.example.yaml")
    assert cfg.llm.base_url == "http://192.168.100.5:8000"
