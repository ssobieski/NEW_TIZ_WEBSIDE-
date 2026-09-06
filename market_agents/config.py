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
    provider: Literal["ollama", "openai_compatible"] = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "llama3.2"
    temperature: float = 0.2
    timeout_seconds: int = 120
    api_key: str | None = None


class AgentsConfig(BaseModel):
    max_items_per_source: int = 15
    lookback_hours: int = 48
    report_dir: str = "reports"
    data_dir: str = "data"
    min_relevance_score: float = 0.35


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
            "Skopiuj config/industry.example.yaml → config/industry.yaml"
        )
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(raw)


def default_config_path() -> Path:
    candidates = [
        Path("config/industry.yaml"),
        Path("config/industry.example.yaml"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]
