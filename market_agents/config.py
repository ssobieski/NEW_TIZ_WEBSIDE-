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
    # ecatalog | pdf | eshop | publication | digital_catalogue | pricelist
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
    # Baza „Katalogi konkurencji — indeks” ze znanymi firmami
    known_firms_database: str | None = None
    # Baza „Pozycje” / literatura (book|paper|video|code) — Notion Type→local kind
    literature_database: str | None = None
    # Parent page dla opcjonalnego push lokalnych CRM tasks → Notion
    crm_parent_page: str | None = None
    # Opcjonalnie: query po tytule przy sync
    search_queries: list[str] = Field(default_factory=list)
    max_pages: int = 50
    include_child_pages: bool = True
    # Notion = katalog firm z przedstawieniem (właściwość lub treść strony)
    enrich_firm_presentations: bool = True
    presentation_max_chars: int = 4000


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




class IndustryMediaSource(BaseModel):
    """Targi branżowe / czasopisma / portale WWW do parsowania."""

    name: str
    url: str
    # trade_fair | magazine | portal (social → sources.social)
    kind: str = "portal"
    brand: str | None = None
    css_selector: str | None = None
    # słowa kluczowe w linkach (exhibitors, articles, news…)
    link_keywords: list[str] = Field(default_factory=list)
    extract_exhibitors: bool = False
    max_links: int = 40
    enabled: bool = True




class SocialMediaSource(BaseModel):
    """Publiczne profile / kanały social (bez logowania)."""

    name: str
    url: str
    # youtube | linkedin | twitter | x | facebook | instagram | tiktok | other
    platform: str = "other"
    brand: str | None = None
    # YouTube: oficjalny RSS działa bez logowania
    channel_id: str | None = None
    feed_url: str | None = None
    link_keywords: list[str] = Field(default_factory=list)
    max_items: int = 20
    enabled: bool = True


class LiteratureSource(BaseModel):
    """Książki / artykuły / wideo / proceedings (literatura branżowa)."""

    name: str
    url: str
    # book | article | video | proceedings | whitepaper | mixed
    kind: str = "mixed"
    brand: str | None = None
    publisher: str | None = None
    feed_url: str | None = None
    language: str | None = None
    link_keywords: list[str] = Field(default_factory=list)
    max_items: int = 25
    enabled: bool = True


class FirmDiscoveryConfig(BaseModel):
    """Parsing wyszukiwania nowych firm (spoza known_firms)."""

    enabled: bool = True
    language: str = "en"
    region: str = "US"
    max_items: int = 20
    # Zapytania Google News RSS — nowe marki / producenci / debiuty
    search_queries: list[str] = Field(
        default_factory=lambda: [
            '"cutting tools" (startup OR "new manufacturer" OR "new brand" OR debut)',
            '"carbide tools" ("enters the market" OR "new company" OR exhibitor)',
            '"tooling manufacturer" (launch OR unveils OR introduces) catalog',
            '"narzędzia skrawające" (nowy OR debiut OR producent)',
        ]
    )
    # Opcjonalne dodatkowe feedy RSS
    feed_urls: list[str] = Field(default_factory=list)
    # Próg fuzzy match do known_firms (0–1)
    known_match_threshold: float = 0.88


class SourcesConfig(BaseModel):
    rss: list[RssSource] = Field(default_factory=list)
    web: list[WebSource] = Field(default_factory=list)
    catalogs: list[CatalogSource] = Field(default_factory=list)
    media: list[IndustryMediaSource] = Field(default_factory=list)
    social: list[SocialMediaSource] = Field(default_factory=list)
    literature: list[LiteratureSource] = Field(default_factory=list)
    firm_discovery: FirmDiscoveryConfig = Field(default_factory=FirmDiscoveryConfig)
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


class CrawlConfig(BaseModel):
    """Adaptacyjny polite crawl — anty-bulk / anty-ban + SSRF guard."""

    enabled: bool = True
    min_delay_seconds: float = 1.5
    max_delay_seconds: float = 45.0
    jitter_seconds: float = 0.5
    per_host_concurrency: int = 1
    global_concurrency: int = 2
    max_retries: int = 3
    backoff_factor: float = 2.0
    respect_robots_txt: bool = True
    adaptive: bool = True
    cooldown_on_block_seconds: float = 900.0
    timeout_seconds: float = 35.0
    user_agent: str = (
        "MarketAgentsLocal/0.3 (+local research; polite adaptive crawler; contact: local-only)"
    )
    # Cybersecurity / SSRF
    block_private_networks: bool = True
    resolve_dns_for_ssrf: bool = True
    allowed_hosts: list[str] = Field(default_factory=list)
    max_redirects: int = 3
    max_response_bytes: int = 15_000_000


class SecurityConfig(BaseModel):
    """Polityka bezpieczeństwa agentów (SSRF, tool policy, fleet, redaction)."""

    enabled: bool = True
    block_private_networks: bool = True
    allow_write_tools: bool = True
    allow_notion_tools: bool = True
    allow_r2_tools: bool = True
    strict_tool_mode: bool = False
    redact_traces: bool = True
    trace_tool_result_max_chars: int = 4000
    fleet_allowlist_only: bool = True
    enforce_r2_prefix: bool = True
    # opcjonalna allowlista hostów (pusta = wszystkie publiczne HTTP/S)
    allowed_hosts: list[str] = Field(default_factory=list)


class GovernanceConfig(BaseModel):
    """AI governance — Policy-as-Code engine (see GOVERNANCE.md)."""

    enabled: bool = True
    mode: Literal["enforce", "monitor"] = "enforce"
    policy_pack: str = "config/governance/tiz.policy.yaml"
    audit_dir: str = "data/governance"
    # true = brak/uszkodzony pack ⇒ deny; false = wyłącz governance przy błędzie load
    fail_closed: bool = False
    # require_approval traktowane jako deny w trybie enforce
    deny_require_approval: bool = True


class AgenticConfig(BaseModel):
    enabled: bool = True
    max_steps: int = 12
    max_deep_parses: int = 8
    # Niski default — bulk = ban; polite fetcher i tak serializuje per host
    parallel_fetches: int = 2
    require_tool_use: bool = True
    trace_dir: str = "data/agent_traces"
    learn_parse_rules: bool = True
    parse_rules_path: str = "data/knowledge/site_parse_rules.json"
    # Wspólna baza umiejętności parsowania (agenty uczą się razem)
    learn_parsing_skills: bool = True
    parsing_skills_path: str = "data/knowledge/parsing_skills.json"
    auto_promote_host_skills: bool = False  # safer default (cyber)


class FleetConfig(BaseModel):
    """
    Centrala (GPU) uczy skills/rules; VPS tylko parsują i odsyłają feedback.
    Sync przez wspólny katalog (NFS/rsync/R2 mirror): data/fleet/
    """

    role: str = "central"  # central | worker | both
    worker_id: str | None = None
    sync_dir: str = "data/fleet"
    auto_pull_before_run: bool = True
    auto_push_after_run: bool = True
    publish_known_firms: bool = True
    publish_relations: bool = True
    publish_ontology: bool = True
    publish_firm_profiles: bool = True
    publish_crm_tasks: bool = False
    publish_tenders: bool = True
    publish_pricelists: bool = True
    publish_literature: bool = True
    publish_product_tech: bool = True
    publish_run_status: bool = True


class AgentsConfig(BaseModel):
    max_items_per_source: int = 20
    lookback_hours: int = 72
    report_dir: str = "reports"
    data_dir: str = "data"
    min_relevance_score: float = 0.35
    # Po każdym run: odśwież profiles + CRM tasks z hot prospectów
    post_enrich_profiles: bool = True
    post_enrich_crm_tasks: bool = True
    post_enrich_suppliers: bool = True
    # Merge duplikatów firm — wyłączone domyślnie (bezpieczniej ręcznie / dry-run)
    post_enrich_resolve_duplicates: bool = False
    # Opcjonalny push CRM → Notion (wymaga crm_parent_page + token); domyślnie OFF
    post_enrich_crm_notion: bool = False
    post_enrich_digest: bool = True
    crm_min_opportunity: float = 60.0
    agentic: AgenticConfig = Field(default_factory=AgenticConfig)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    fleet: FleetConfig = Field(default_factory=FleetConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)


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
    cfg = AppConfig.model_validate(raw)
    _apply_llm_env_overrides(cfg)
    return cfg


def _apply_llm_env_overrides(cfg: AppConfig) -> None:
    """Override LLM endpoint from env — for Dell over Cybertech VPN / remote vLLM.

    MARKET_AGENTS_LLM_BASE_URL or VLLM_BASE_URL → llm.base_url
    MARKET_AGENTS_LLM_MODEL or VLLM_MODEL → llm.model
    MARKET_AGENTS_LLM_API_KEY → llm.api_key
    """
    base = (
        os.environ.get("MARKET_AGENTS_LLM_BASE_URL")
        or os.environ.get("VLLM_BASE_URL")
        or ""
    ).strip()
    if base:
        cfg.llm.base_url = base.rstrip("/")
    model = (
        os.environ.get("MARKET_AGENTS_LLM_MODEL")
        or os.environ.get("VLLM_MODEL")
        or ""
    ).strip()
    if model:
        cfg.llm.model = model
    api_key = os.environ.get("MARKET_AGENTS_LLM_API_KEY")
    if api_key is not None and api_key != "":
        cfg.llm.api_key = api_key


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
