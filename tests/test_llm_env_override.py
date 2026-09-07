"""LLM config env overrides for Dell over Cybertech VPN."""

from __future__ import annotations

import os

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


def test_load_dotenv_sets_vllm_base(tmp_path, monkeypatch):
    from market_agents.config import load_dotenv_files

    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    monkeypatch.delenv("MARKET_AGENTS_LLM_BASE_URL", raising=False)
    env = tmp_path / ".env"
    env.write_text("VLLM_BASE_URL=http://10.9.8.7:8000\n", encoding="utf-8")
    loaded = load_dotenv_files(env)
    assert loaded
    assert os.environ["VLLM_BASE_URL"] == "http://10.9.8.7:8000"
    # existing env wins
    monkeypatch.setenv("VLLM_BASE_URL", "http://keep.me:8000")
    env.write_text("VLLM_BASE_URL=http://should-not-overwrite:8000\n", encoding="utf-8")
    load_dotenv_files(env)
    assert os.environ["VLLM_BASE_URL"] == "http://keep.me:8000"
