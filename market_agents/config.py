from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class IndustryConfig(BaseModel):
    name: str
    language: str = "pl"
    keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    focus_questions: list[str] = Field(default_factory=list)


class RssSource(BaseModel):
    name: str
    url: str


class WebSource(BaseModel):
    name: str
    url: str
    css_selector: str | None = None


class SourcesConfig(BaseModel):
    rss: list[RssSource] = Field(default_factory=list)
    web: list[WebSource] = Field(default_factory=list)


class LlmConfig(BaseModel):
    """Lokalny inference — domyślnie vLLM (OpenAI-compatible) na 4x A100."""

    provider: Literal["vllm", "ollama", "openai_compatible"] = "vllm"
    base_url: str = "http://127.0.0.1:8000"
    model: str = "Qwen/Qwen2.5-72B-Instruct-AWQ"
    temperature: float = 0.1
    timeout_seconds: int = 300
    api_key: str | None = "EMPTY"
    max_tokens: int = 4096
    tensor_parallel_size: int = 4
    gpu_memory_utilization: float = 0.90
    max_model_len: int = 32768


class AgenticConfig(BaseModel):
    enabled: bool = True
    max_steps: int = 12
    max_deep_parses: int = 8
    parallel_fetches: int = 6
    require_tool_use: bool = True
    trace_dir: str = "data/agent_traces"


class AgentsConfig(BaseModel):
    max_items_per_source: int = 20
    lookback_hours: int = 72
    report_dir: str = "reports"
    data_dir: str = "data"
    min_relevance_score: float = 0.35
    agentic: AgenticConfig = Field(default_factory=AgenticConfig)


class ScheduleConfig(BaseModel):
    every_hours: int = 6


class AppConfig(BaseModel):
    industry: IndustryConfig
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)

    @property
    def report_path(self) -> Path:
        return Path(self.agents.report_dir)

    @property
    def data_path(self) -> Path:
        return Path(self.agents.data_dir)


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Brak pliku konfiguracji: {config_path}. "
            "Skopiuj config/dell_a100.example.yaml → config/industry.yaml"
        )
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(raw)


def default_config_path() -> Path:
    for candidate in (
        Path("config/industry.yaml"),
        Path("config/dell_a100.example.yaml"),
        Path("config/industry.example.yaml"),
    ):
        if candidate.exists():
            return candidate
    return Path("config/industry.yaml")
