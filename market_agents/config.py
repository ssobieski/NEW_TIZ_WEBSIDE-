from __future__ import annotations

import os
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


class CatalogSource(BaseModel):
    """E-katalog / PDF / publikacja / digital catalogue / e-shop konkurencji."""

    name: str
    url: str
    brand: str | None = None
    # ecatalog | pdf | eshop | publication | digital_catalogue
    kind: str = "ecatalog"
    extract_text: bool = True
    max_pdf_pages: int = 8
    max_pdf_chars: int = 12000
    # jeśli True — zbieraj też zwykłe linki (rzadko potrzebne)
    discover_all_links: bool = False


class NotionConfig(BaseModel):
    """Istniejąca baza wiedzy w Notion — katalogi konkurencji, literatura, review."""

    enabled: bool = False
    # Integration token: env NOTION_TOKEN albo tu (lepiej env)
    token_env: str = "NOTION_TOKEN"
    # Root pages / databases do zsynchronizowania (URL lub ID)
    root_pages: list[str] = Field(default_factory=list)
    # Opcjonalnie: query po tytule przy sync
    search_queries: list[str] = Field(default_factory=list)
    max_pages: int = 50
    include_child_pages: bool = True


class CloudflareR2Config(BaseModel):
    """Duże archiwum na Cloudflare R2 (S3-compatible)."""

    enabled: bool = False
    account_id_env: str = "CF_ACCOUNT_ID"
    access_key_env: str = "CF_R2_ACCESS_KEY_ID"
    secret_key_env: str = "CF_R2_SECRET_ACCESS_KEY"
    bucket: str = "market-intel"
    prefix: str = "monitoring/"
    endpoint_url: str | None = None  # domyślnie https://<account>.r2.cloudflarestorage.com
    max_objects: int = 100
    # rozszerzenia traktowane jako źródła tekstowe
    text_extensions: list[str] = Field(
        default_factory=lambda: [
            ".json",
            ".jsonl",
            ".md",
            ".txt",
            ".html",
            ".csv",
            ".xml",
            ".pdf",
        ]
    )


class SourcesConfig(BaseModel):
    rss: list[RssSource] = Field(default_factory=list)
    web: list[WebSource] = Field(default_factory=list)
    catalogs: list[CatalogSource] = Field(default_factory=list)
    notion: NotionConfig = Field(default_factory=NotionConfig)
    cloudflare_r2: CloudflareR2Config = Field(default_factory=CloudflareR2Config)


class LlmConfig(BaseModel):
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

    def notion_token(self) -> str | None:
        return os.environ.get(self.sources.notion.token_env) or None

    def r2_credentials(self) -> dict[str, str | None]:
        r2 = self.sources.cloudflare_r2
        account = os.environ.get(r2.account_id_env)
        return {
            "account_id": account,
            "access_key": os.environ.get(r2.access_key_env),
            "secret_key": os.environ.get(r2.secret_key_env),
            "endpoint": r2.endpoint_url
            or (f"https://{account}.r2.cloudflarestorage.com" if account else None),
            "bucket": r2.bucket,
            "prefix": r2.prefix,
        }


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Brak pliku konfiguracji: {config_path}. "
            "Skopiuj config/tiz_cutting_tools.example.yaml → config/industry.yaml"
        )
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(raw)


def default_config_path() -> Path:
    for candidate in (
        Path("config/industry.yaml"),
        Path("config/tiz_cutting_tools.example.yaml"),
        Path("config/dell_a100.example.yaml"),
        Path("config/industry.example.yaml"),
    ):
        if candidate.exists():
            return candidate
    return Path("config/industry.yaml")
