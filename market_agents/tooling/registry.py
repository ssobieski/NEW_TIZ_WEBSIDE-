from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from market_agents.collectors.catalog import CatalogCollector
from market_agents.collectors.firm_discovery import FirmDiscoveryCollector
from market_agents.collectors.media import IndustryMediaCollector, extract_exhibitors_from_html
from market_agents.collectors.social import SocialMediaCollector, detect_platform, SOCIAL_PLATFORMS
from market_agents.collectors.literature import LiteratureCollector
from market_agents.collectors.notion import NotionCollector
from market_agents.collectors.r2 import build_r2_collector
from market_agents.config import (
    AppConfig,
    CatalogSource,
    IndustryMediaSource,
    LiteratureSource,
    SocialMediaSource,
)
from market_agents.firm_relations import (
    RELATION_TYPES,
    FirmRelationsGraph,
    extract_relation_candidates,
)
from market_agents.firm_profiles import (
    FirmProfile,
    FirmProfileRegistry,
    extract_contacts_from_html,
    firm_id_from_name,
    score_sentiment,
)
from market_agents.prospects import (
    ProspectRegistry,
    analyze_prospect_html,
    analyze_prospect_text,
    estimate_tooling_budget,
    load_vertical_assumptions,
)
from market_agents.crm_tasks import CrmTaskStore, create_tasks_from_prospect_scoreboard
from market_agents.firms import KnownFirmsIndex, extract_candidate_firm_names, filter_new_firms
from market_agents.literature import LITERATURE_KINDS, LiteratureRegistry, classify_literature_kind
from market_agents.memory import MarketMemory
from market_agents.models import MarketItem
from market_agents.ontology import (
    EDGE_TYPES,
    ENTITY_TYPES,
    MachiningOntology,
    extract_domain_context,
)
from market_agents.parse_rules import PARSE_METHODS, SiteParseRulesStore
from market_agents.parsing import IntelligentParser, ParsedDocument
from market_agents.parsing_skills import SKILL_CATEGORIES, ParsingSkillsStore
from market_agents.pricelists import PricelistRegistry
from market_agents.product_tech import (
    ProductTechRegistry,
    TECH_KINDS,
    classify_tech_kind,
    extract_tech_schema,
)
from market_agents.polite_http import build_fetcher_from_config
from market_agents.governance import GovernanceEngine
from market_agents.security import (
    SecurityPolicy,
    enforce_r2_key_prefix,
    redact_secrets,
    safe_error,
    security_status,
    validate_fetch_url,
)


ToolFn = Callable[[dict[str, Any]], dict[str, Any]]


class ToolRegistry:
    """Narzędzia dla agenta ReAct — parsing + katalogi/PDF + Notion + R2."""

    def __init__(
        self,
        config: AppConfig,
        memory: MarketMemory,
        candidates: list[MarketItem] | None = None,
    ) -> None:
        self.config = config
        self.memory = memory
        self._fetcher = build_fetcher_from_config(config)
        self._parse_rules = (
            SiteParseRulesStore.load(data_dir=config.data_path)
            if config.agents.agentic.learn_parse_rules
            else SiteParseRulesStore()
        )
        if config.agents.agentic.learn_parsing_skills:
            self._parsing_skills = ParsingSkillsStore.load(
                data_dir=config.data_path, with_seeds=True
            )
        else:
            self._parsing_skills = ParsingSkillsStore()
        self.parser = IntelligentParser(
            rules=self._parse_rules if config.agents.agentic.learn_parse_rules else None,
            learn=bool(config.agents.agentic.learn_parse_rules),
            fetcher=self._fetcher,
        )
        self.candidates = candidates or []
        self.parsed_cache: dict[str, ParsedDocument] = {}
        self.structured: list[dict[str, Any]] = []
        self._notion: NotionCollector | None = None
        self._r2 = None
        self._security = self._build_security_policy(config)
        self._governance = GovernanceEngine.from_config(config)
        self._handlers: dict[str, ToolFn] = {
            "list_candidates": self.list_candidates,
            "fetch_and_parse": self.fetch_and_parse,
            "batch_parse": self.batch_parse,
            "crawl_status": self.crawl_status,
            "extract_market_intel": self.extract_market_intel,
            "search_memory": self.search_memory,
            "remember": self.remember,
            "score_item": self.score_item,
            "search_notion": self.search_notion,
            "fetch_notion_page": self.fetch_notion_page,
            "search_r2": self.search_r2,
            "fetch_r2_object": self.fetch_r2_object,
            "list_catalog_sources": self.list_catalog_sources,
            "discover_catalog_assets": self.discover_catalog_assets,
            "fetch_pdf_text": self.fetch_pdf_text,
            "list_media_sources": self.list_media_sources,
            "discover_media_links": self.discover_media_links,
            "parse_media_page": self.parse_media_page,
            "extract_fair_exhibitors": self.extract_fair_exhibitors,
            "list_social_sources": self.list_social_sources,
            "discover_social_posts": self.discover_social_posts,
            "parse_social_post": self.parse_social_post,
            "list_literature_sources": self.list_literature_sources,
            "discover_literature": self.discover_literature,
            "list_literature": self.list_literature,
            "register_literature": self.register_literature,
            "sync_notion_literature": self.sync_notion_literature,
            "get_domain_context": self.get_domain_context,
            "extract_domain_context": self.extract_domain_context_tool,
            "list_ontology": self.list_ontology,
            "ontology_neighborhood": self.ontology_neighborhood,
            "add_ontology_edge": self.add_ontology_edge,
            "build_ontology": self.build_ontology,
            "find_ontology_path": self.find_ontology_path,
            "list_known_firms": self.list_known_firms,
            "get_firm_presentation": self.get_firm_presentation,
            "discover_new_firms": self.discover_new_firms,
            "check_firm_known": self.check_firm_known,
            "list_relations": self.list_relations,
            "add_relation": self.add_relation,
            "discover_relations": self.discover_relations,
            "firm_neighborhood": self.firm_neighborhood,
            "list_firm_profiles": self.list_firm_profiles,
            "get_firm_profile": self.get_firm_profile,
            "score_firm_profile": self.score_firm_profile,
            "firm_scoreboard": self.firm_scoreboard,
            "build_firm_profiles": self.build_firm_profiles,
            "upsert_firm_profile": self.upsert_firm_profile,
            "enrich_firm_profile": self.enrich_firm_profile,
            "register_social_mention": self.register_social_mention,
            "list_prospects": self.list_prospects,
            "get_prospect_profile": self.get_prospect_profile,
            "prospect_scoreboard": self.prospect_scoreboard,
            "analyze_prospect": self.analyze_prospect,
            "estimate_tooling_budget": self.estimate_tooling_budget_tool,
            "ingest_prospect_seeds": self.ingest_prospect_seeds,
            "create_crm_task": self.create_crm_task,
            "list_crm_tasks": self.list_crm_tasks,
            "sync_crm_from_prospects": self.sync_crm_from_prospects,
            "push_crm_to_notion": self.push_crm_to_notion,
            "list_suppliers": self.list_suppliers,
            "supplier_scoreboard": self.supplier_scoreboard,
            "score_suppliers": self.score_suppliers,
            "resolve_firm_duplicates": self.resolve_firm_duplicates,
            "list_run_metrics": self.list_run_metrics,
            "refresh_change_digest": self.refresh_change_digest,
            "export_knowledge_pack": self.export_knowledge_pack_tool,
            "list_parse_rules": self.list_parse_rules,
            "upsert_parse_rule": self.upsert_parse_rule,
            "rate_parse": self.rate_parse,
            "list_parsing_skills": self.list_parsing_skills,
            "match_parsing_skills": self.match_parsing_skills,
            "learn_parsing_skill": self.learn_parsing_skill,
            "improve_parsing_skill": self.improve_parsing_skill,
            "rate_parsing_skill": self.rate_parsing_skill,
            "promote_host_skill": self.promote_host_skill,
            "discover_pricelists": self.discover_pricelists,
            "list_available_pricelists": self.list_available_pricelists,
            "register_pricelist": self.register_pricelist,
            "discover_product_tech": self.discover_product_tech,
            "list_product_tech": self.list_product_tech,
            "register_product_tech": self.register_product_tech,
            "extract_product_tech_schema": self.extract_product_tech_schema,
            "security_status": self.security_status_tool,
            "governance_status": self.governance_status_tool,
        }
        self._known: KnownFirmsIndex | None = None
        self._relations: FirmRelationsGraph | None = None
        self._ontology: MachiningOntology | None = None
        self._profiles: FirmProfileRegistry | None = None

    def _get_profiles(self) -> FirmProfileRegistry:
        if self._profiles is None:
            self._profiles = FirmProfileRegistry.load(self.config.data_path)
        return self._profiles

    @staticmethod
    def _build_security_policy(config: AppConfig) -> SecurityPolicy:
        sec = getattr(config.agents, "security", None)
        if sec is None:
            return SecurityPolicy()
        return SecurityPolicy(
            block_private_networks=bool(getattr(sec, "block_private_networks", True)),
            allowed_hosts=list(getattr(sec, "allowed_hosts", None) or []),
            allow_write_tools=bool(getattr(sec, "allow_write_tools", True)),
            allow_notion_tools=bool(getattr(sec, "allow_notion_tools", True)),
            allow_r2_tools=bool(getattr(sec, "allow_r2_tools", True)),
            strict_tool_mode=bool(getattr(sec, "strict_tool_mode", False)),
            redact_traces=bool(getattr(sec, "redact_traces", True)),
            trace_tool_result_max_chars=int(
                getattr(sec, "trace_tool_result_max_chars", 4000)
            ),
            fleet_allowlist_only=bool(getattr(sec, "fleet_allowlist_only", True)),
            enforce_r2_prefix=bool(getattr(sec, "enforce_r2_prefix", True)),
        )

    def _get_ontology(self) -> MachiningOntology:
        if self._ontology is None:
            self._ontology = MachiningOntology.load(self.config.data_path)
            if len(self._ontology.nodes) < 5:
                self._ontology.merge_seed("config/machining_ontology.seed.json")
                self._ontology.save(self.config.data_path)
        return self._ontology

    def _get_notion(self) -> NotionCollector:
        if self._notion is None:
            token = self.config.notion_token()
            if not token:
                raise RuntimeError("Brak NOTION_TOKEN — ustaw env z tokenem integracji Notion")
            self._notion = NotionCollector(self.config.sources.notion, token)
        return self._notion

    def _get_r2(self):
        if self._r2 is None:
            self._r2 = build_r2_collector(
                self.config.sources.cloudflare_r2,
                self.config.r2_credentials(),
            )
        return self._r2

    def openai_tools_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_candidates",
                    "description": "Lista kandydatów sygnałów rynkowych zebranych ze źródeł RSS/WWW.",
                    "parameters": {
                        "type": "object",
                        "properties": {"limit": {"type": "integer", "default": 20}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_and_parse",
                    "description": (
                        "Pobierz URL i inteligentnie wyodrębnij pełną treść. "
                        "Używa nauczonych reguł per-host (CSS/metoda). "
                        "Przy słabym wyniku: upsert_parse_rule + ponów fetch_and_parse."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "force_method": {
                                "type": "string",
                                "enum": ["auto", "trafilatura", "bs4", "css"],
                                "description": "Wymuś metodę parse (nadpisuje regułę hosta)",
                            },
                            "css_selector": {
                                "type": "string",
                                "description": "Opcjonalny CSS selector treści (method=css)",
                            },
                            "bypass_cache": {
                                "type": "boolean",
                                "default": False,
                                "description": "True = ponów parse mimo cache (po zmianie reguły)",
                            },
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "batch_parse",
                    "description": (
                        "Sparsuj kilka URL polite/adaptive (niski concurrency, delay per host). "
                        "NIE do bulk scrapingu — max kilka adresów. Ten sam host = sekwencyjnie."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "urls": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 8,
                            }
                        },
                        "required": ["urls"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "crawl_status",
                    "description": (
                        "Status adaptacyjnego crawlera anty-ban: delay/cooldown per host, "
                        "polityka robots/concurrency. Sprawdź przed kolejnymi fetchami."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host_or_url": {
                                "type": "string",
                                "description": "Opcjonalny host/URL; puste = cała mapa zdrowia",
                            }
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "security_status",
                    "description": (
                        "Status kontroli bezpieczeństwa: SSRF guard, tool policy "
                        "(Notion/R2/write), redaction, fleet allowlist."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "governance_status",
                    "description": (
                        "Status AI governance (policy-as-code): pack id, mode enforce/monitor, "
                        "liczba reguł, audit trail."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "extract_market_intel",
                    "description": (
                        "Zapisz ustrukturyzowany intel rynkowy z już sparsowanej treści "
                        "(encje, typ sygnału, wpływ, konkurenci, liczby)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "signal_type": {
                                "type": "string",
                                "enum": [
                                    "threat",
                                    "opportunity",
                                    "competitor",
                                    "new_firm",
                                    "relation",
                                    "regulation",
                                    "trend",
                                    "pricing",
                                    "product_tech",
                                    "social",
                                    "literature",
                                    "noise",
                                ],
                            },
                            "impact": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                            "summary": {"type": "string"},
                            "entities": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "numbers": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Kwoty, %, daty, wolumeny",
                            },
                            "action": {"type": "string"},
                            "relevance_0_to_1": {"type": "number"},
                        },
                        "required": [
                            "url",
                            "signal_type",
                            "impact",
                            "summary",
                            "relevance_0_to_1",
                        ],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_memory",
                    "description": "Przeszukaj lokalną pamięć rynkową z poprzednich cykli.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "default": 6},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "remember",
                    "description": "Zapisz fakt / wniosek do lokalnej pamięci rynkowej.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["kind", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "score_item",
                    "description": "Oceń istotność kandydata względem branży i konkurentów.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "score": {"type": "number"},
                            "reason": {"type": "string"},
                        },
                        "required": ["url", "score", "reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_notion",
                    "description": (
                        "Przeszukaj istniejącą bazę Notion (katalogi konkurencji TIZ, "
                        "handbooki, literatura, review stron)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "default": 10},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_notion_page",
                    "description": "Pobierz pełną treść strony Notion po URL lub ID.",
                    "parameters": {
                        "type": "object",
                        "properties": {"page_id_or_url": {"type": "string"}},
                        "required": ["page_id_or_url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_r2",
                    "description": (
                        "Przeszukaj duże archiwum na Cloudflare R2 (pliki JSON/MD/HTML/CSV)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "default": 15},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_r2_object",
                    "description": "Pobierz treść obiektu z Cloudflare R2 po kluczu.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "max_chars": {"type": "integer", "default": 12000},
                        },
                        "required": ["key"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_catalog_sources",
                    "description": (
                        "Lista skonfigurowanych źródeł: e-catalog, PDF, digital catalogue, "
                        "publikacje, e-shop konkurencji."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "description": "Opcjonalny filtr: pricelist|ecatalog|pdf|eshop|publication|digital_catalogue",
                            }
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "discover_catalog_assets",
                    "description": (
                        "Odkryj PDF-y / linki e-catalog / e-shop na stronie hubu katalogu "
                        "(po nazwie źródła lub URL)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source_name": {"type": "string"},
                            "url": {"type": "string"},
                            "max_links": {"type": "integer", "default": 30},
                            "asset_type": {
                                "type": "string",
                                "description": "Filtr: pricelist|pdf|ecatalog|eshop|publication|digital_catalogue",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_pdf_text",
                    "description": (
                        "Pobierz meta PDF i wyodrębnij tekst (pierwsze strony) z katalogu "
                        "lub publikacji konkurencji."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "source_name": {"type": "string"},
                            "max_pages": {"type": "integer", "default": 8},
                            "max_chars": {"type": "integer", "default": 12000},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "discover_pricelists",
                    "description": (
                        "Priorytet TIZ: odkryj DOSTĘPNE CENNIKI (pricelist / Preisliste / cennik) "
                        "z hubów katalogów znanych firm. Zapisuje trafienia do rejestru."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source_name": {
                                "type": "string",
                                "description": "Opcjonalnie jedna firma/źródło; puste = skan wszystkich catalogs",
                            },
                            "url": {"type": "string"},
                            "max_sources": {"type": "integer", "default": 12},
                            "max_links_per_source": {"type": "integer", "default": 40},
                            "register": {
                                "type": "boolean",
                                "default": True,
                                "description": "Zapisz znalezione cenniki do available_pricelists.json",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_available_pricelists",
                    "description": (
                        "Lista już znalezionych / zarejestrowanych dostępnych cenników konkurencji "
                        "(data/knowledge/available_pricelists.json)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "brand": {"type": "string"},
                            "access": {
                                "type": "string",
                                "description": "public|login|request|unknown",
                            },
                            "q": {"type": "string", "description": "Filtr tekstowy"},
                            "limit": {"type": "integer", "default": 50},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "discover_product_tech",
                    "description": (
                        "Priorytet TIZ: odkryj INFORMACJE TECHNICZNE o produktach — "
                        "cutting data, handbooki, application guides, ISO 13399, karty tech. "
                        "Rejestruje źródła (schemat pól), NIE kopiuje tabel vc/fz do CutData."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source_name": {"type": "string"},
                            "url": {"type": "string"},
                            "max_sources": {"type": "integer", "default": 12},
                            "max_links_per_source": {"type": "integer", "default": 40},
                            "register": {"type": "boolean", "default": True},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_product_tech",
                    "description": (
                        "Lista zarejestrowanych źródeł tech produktów "
                        "(data/knowledge/product_tech.json)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "brand": {"type": "string"},
                            "kind": {
                                "type": "string",
                                "description": "cutting_data|handbook|application_guide|iso13399|tech_datasheet|grade_chart",
                            },
                            "access": {"type": "string"},
                            "q": {"type": "string"},
                            "limit": {"type": "integer", "default": 50},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "register_product_tech",
                    "description": (
                        "Dodaj/aktualizuj źródło informacji technicznej o produktach "
                        "(handbook / cutting data / ISO 13399 / karta tech)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "title": {"type": "string"},
                            "brand": {"type": "string"},
                            "kind": {"type": "string"},
                            "access": {"type": "string", "default": "public"},
                            "year": {"type": "string"},
                            "notes": {"type": "string"},
                            "confirmed": {"type": "boolean", "default": True},
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "extract_product_tech_schema",
                    "description": (
                        "Z tekstu lub publicznego PDF wyodrębnij SCHEMAT pól (vc/fz/ap/ae, "
                        "grupy materiałowe, chłodzenie, rozdziały). Tylko układ — bez kopiowania "
                        "tabel wartości do CutData."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "url": {"type": "string", "description": "Opcjonalnie PDF/URL do pobrania tekstu"},
                            "source_name": {"type": "string"},
                            "brand": {"type": "string"},
                            "register": {"type": "boolean", "default": True},
                            "max_pages": {"type": "integer", "default": 10},
                        },
                    },
                },
            },
            
            {
                "type": "function",
                "function": {
                    "name": "register_pricelist",
                    "description": (
                        "Dodaj lub zaktualizuj dostępny cennik w lokalnym rejestrze "
                        "(URL + marka + dostępność public/login)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "title": {"type": "string"},
                            "brand": {"type": "string"},
                            "access": {
                                "type": "string",
                                "description": "public|login|request|unknown",
                                "default": "public",
                            },
                            "currency": {"type": "string"},
                            "year": {"type": "string"},
                            "notes": {"type": "string"},
                            "confirmed": {"type": "boolean", "default": True},
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_media_sources",
                    "description": (
                        "Lista źródeł mediów branżowych: targi (trade_fair), czasopisma (magazine), "
                        "portale WWW (portal). Konfiguracja: sources.media."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "description": "Filtr: trade_fair|magazine|portal",
                            },
                            "enabled_only": {
                                "type": "boolean",
                                "default": True,
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "discover_media_links",
                    "description": (
                        "Odkryj linki z hubu targów / czasopisma / portalu "
                        "(exhibitors, articles, news). Zwraca też heurystycznych wystawców."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source_name": {"type": "string"},
                            "url": {"type": "string"},
                            "kind": {
                                "type": "string",
                                "description": "trade_fair|magazine|portal (dla ad-hoc URL)",
                            },
                            "max_links": {"type": "integer", "default": 40},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "parse_media_page",
                    "description": (
                        "Pobierz i sparsuj stronę mediów branżowych (artykuł, lista wystawców, "
                        "portal news). Zwraca tekst + candidate_firms + exhibitors."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source_name": {"type": "string"},
                            "url": {"type": "string"},
                            "kind": {"type": "string"},
                            "max_chars": {"type": "integer", "default": 12000},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "extract_fair_exhibitors",
                    "description": (
                        "Wyodrębnij listę wystawców z URL targów / strony exhibitor list. "
                        "Używaj do discovery nowych firm na EMO/AMB/IMTS."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "source_name": {"type": "string"},
                            "limit": {"type": "integer", "default": 80},
                            "check_known": {
                                "type": "boolean",
                                "default": True,
                                "description": "True = oznacz known vs new vs known_firms",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_social_sources",
                    "description": (
                        "Lista publicznych profili social (YouTube/LinkedIn/X/Facebook/Instagram). "
                        "sources.social — bez logowania; YouTube preferuj channel_id/RSS."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "platform": {
                                "type": "string",
                                "description": "youtube|linkedin|x|twitter|facebook|instagram|tiktok|other",
                            },
                            "enabled_only": {"type": "boolean", "default": True},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "discover_social_posts",
                    "description": (
                        "Odkryj posty/wideo z publicznego profilu social. "
                        "YouTube: RSS; inne: publiczny HTML + sygnały katalog/launch/EMO."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source_name": {"type": "string"},
                            "url": {"type": "string"},
                            "platform": {"type": "string"},
                            "max_items": {"type": "integer", "default": 20},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "parse_social_post",
                    "description": (
                        "Sparsuj publiczny post/wideo social (meta + tekst). "
                        "Zwraca signal_hints (catalog/new_product/trade_fair/tech/pricing)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source_name": {"type": "string"},
                            "url": {"type": "string"},
                            "platform": {"type": "string"},
                            "max_chars": {"type": "integer", "default": 8000},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_literature_sources",
                    "description": (
                        "Lista źródeł literatury (książki / artykuły / wideo / proceedings). "
                        "sources.literature — huby wydawców, arXiv, CTE, kanały edukacyjne."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "description": "book|article|video|proceedings|whitepaper|mixed",
                            },
                            "enabled_only": {"type": "boolean", "default": True},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "discover_literature",
                    "description": (
                        "Odkryj książki/artykuły/wideo z hubu literatury (RSS lub HTML). "
                        "Opcjonalnie zapisz do data/knowledge/literature.json."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source_name": {"type": "string"},
                            "url": {"type": "string"},
                            "kind": {"type": "string"},
                            "max_items": {"type": "integer", "default": 25},
                            "register": {
                                "type": "boolean",
                                "default": True,
                                "description": "Zapisz znalezione pozycje do literature.json",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_literature",
                    "description": (
                        "Lista zarejestrowanej literatury "
                        "(data/knowledge/literature.json): book|article|video|…"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string"},
                            "topic": {"type": "string"},
                            "q": {"type": "string"},
                            "limit": {"type": "integer", "default": 50},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "register_literature",
                    "description": (
                        "Zarejestruj książkę / artykuł / wideo w literature.json "
                        "(URL, kind, authors, year, topics)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "title": {"type": "string"},
                            "kind": {
                                "type": "string",
                                "description": "book|article|video|proceedings|whitepaper",
                            },
                            "authors": {"type": "string"},
                            "year": {"type": "string"},
                            "source": {"type": "string"},
                            "brand": {"type": "string"},
                            "topics": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "access": {"type": "string", "default": "unknown"},
                            "notes": {"type": "string"},
                            "confirmed": {"type": "boolean", "default": False},
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "sync_notion_literature",
                    "description": (
                        "Zsynchronizuj bazę Notion „Pozycje” (Type=book|paper|video) "
                        "do lokalnego literature.json. paper→article."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "default": 200},
                            "save": {"type": "boolean", "default": True},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_domain_context",
                    "description": (
                        "Skrót lokalnej ontologii TIZ: materiały ISO P/M/K/N/S/H, maszyny, "
                        "chłodziwo, procesy. Tani lokalny kontekst zamiast Grok — "
                        "boty muszą rozumieć technologię obróbki."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "extract_domain_context",
                    "description": (
                        "Z tekstu/URL wyodrębnij kontekst: materiały, maszyny, chłodziwo, "
                        "procesy, parametry (tylko schemat vc/fz/ap — bez kopiowania wartości). "
                        "Opcjonalnie zapisz do knowledge graph ontology.json."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "url": {"type": "string"},
                            "ingest": {
                                "type": "boolean",
                                "default": True,
                                "description": "Zapisz wykryte encje/krawędzie do ontology.json",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_ontology",
                    "description": (
                        "Lista encji knowledge graph (material|machine|coolant|process|"
                        "tool_family|standard|parameter|firm)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "q": {"type": "string"},
                            "limit": {"type": "integer", "default": 50},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ontology_neighborhood",
                    "description": (
                        "Sąsiedztwo w ontologii (np. process:milling → materiały, maszyny, chłodziwo)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "node": {
                                "type": "string",
                                "description": "id lub label, np. process:milling / frezowanie",
                            },
                            "depth": {"type": "integer", "default": 1},
                        },
                        "required": ["node"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "add_ontology_edge",
                    "description": (
                        "Dodaj krawędź do knowledge graph "
                        f"(relations: {', '.join(EDGE_TYPES[:8])}…)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "target": {"type": "string"},
                            "relation": {"type": "string"},
                            "evidence": {"type": "string"},
                            "confidence": {"type": "number", "default": 0.8},
                        },
                        "required": ["source", "target", "relation"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "build_ontology",
                    "description": (
                        "Zbuduj / odśwież knowledge graph database z seed + known_firms + "
                        "firm_relations + literature + product_tech. "
                        "To jest lokalna ontology DB (zamiast Grok)."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "find_ontology_path",
                    "description": (
                        "Znajdź ścieżkę w knowledge graph "
                        "(np. firm:sandvik-coromant → material:iso-s / process:milling)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "target": {"type": "string"},
                            "max_depth": {"type": "integer", "default": 5},
                        },
                        "required": ["source", "target"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_known_firms",
                    "description": (
                        "Katalog znanych firm z Notion (przedstawienie / indeks konkurencji): "
                        "nazwa, kraj, site, downloads, fokus, presentation (opis firmy z Notion)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Filtr po nazwie / kraju / fokusie / przedstawieniu",
                            },
                            "limit": {"type": "integer", "default": 50},
                            "live": {
                                "type": "boolean",
                                "default": False,
                                "description": "True = odśwież z Notion API (z przedstawieniami)",
                            },
                            "include_presentation": {
                                "type": "boolean",
                                "default": True,
                                "description": "False = bez pola presentation (krótsza lista)",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_firm_presentation",
                    "description": (
                        "Pobierz przedstawienie jednej firmy z Notion katalogu "
                        "(cache known_firms.presentation albo live fetch_notion_page). "
                        "Używaj zanim scrape'ujesz WWW znanej marki."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string"},
                            "live": {
                                "type": "boolean",
                                "default": False,
                                "description": "True = dociągnij treść strony Notion na żywo",
                            },
                        },
                        "required": ["company"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "discover_new_firms",
                    "description": (
                        "Parsuj wyszukiwanie / kandydatów i wyodrębnij NOWYCH producentów "
                        "spoza listy known_firms. Używa Google News discovery + heurystyk "
                        "nazw firm. Zwraca kandydatów new_firm z evidence URL."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "run_search": {
                                "type": "boolean",
                                "default": True,
                                "description": "True = odpal FirmDiscoveryCollector (RSS search)",
                            },
                            "parse_candidates": {
                                "type": "boolean",
                                "default": True,
                                "description": "True = wyodrębnij firmy z list_candidates",
                            },
                            "texts": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string"},
                                        "url": {"type": "string"},
                                    },
                                },
                                "description": "Opcjonalne dodatkowe teksty do parsowania",
                            },
                            "limit": {"type": "integer", "default": 25},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "check_firm_known",
                    "description": (
                        "Sprawdź czy nazwa firmy jest już na liście known_firms "
                        "(fuzzy match). Zwraca known=true/false + dopasowanie."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "names": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_relations",
                    "description": (
                        "Lista powiązań firm w siatce (distributor_of, dealer_of, brand_of, "
                        "subsidiary_of, oem_group, partner_of, rebrand_of). "
                        "Np. Hoffmann Group distributor_of Sandvik Coromant; "
                        "GARANT brand_of Hoffmann Group."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "company": {
                                "type": "string",
                                "description": "Filtr po nazwie firmy (source lub target)",
                            },
                            "relation_type": {
                                "type": "string",
                                "enum": list(RELATION_TYPES),
                            },
                            "limit": {"type": "integer", "default": 50},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "add_relation",
                    "description": (
                        "Dodaj krawędź do siatki powiązań: source relation_type target. "
                        "Używaj gdy z tekstu wynika np. dystrybucja lub marka własna."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "target": {"type": "string"},
                            "relation_type": {
                                "type": "string",
                                "enum": list(RELATION_TYPES),
                            },
                            "evidence": {"type": "string"},
                            "url": {"type": "string"},
                            "confidence": {"type": "number", "default": 0.7},
                        },
                        "required": ["source", "target", "relation_type"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "discover_relations",
                    "description": (
                        "Wyodrębnij kandydatów relacji (dystrybutor / dealer / brand / grupa) "
                        "z tekstów, kandydatów newsów i opcjonalnie Notion. "
                        "Może zapisać znalezione krawędzie do lokalnego grafu."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "texts": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string"},
                                        "url": {"type": "string"},
                                    },
                                },
                            },
                            "parse_candidates": {
                                "type": "boolean",
                                "default": True,
                            },
                            "scan_notion_cache": {
                                "type": "boolean",
                                "default": False,
                            },
                            "persist": {
                                "type": "boolean",
                                "default": True,
                                "description": "True = dopisz do data/knowledge/firm_relations.json",
                            },
                            "limit": {"type": "integer", "default": 30},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "firm_neighborhood",
                    "description": (
                        "Sąsiedztwo firmy w siatce powiązań (1–2 hop): dystrybutorzy, "
                        "marki, spółki w grupie OEM, klienci, dostawcy."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string"},
                            "depth": {
                                "type": "integer",
                                "default": 1,
                                "minimum": 1,
                                "maximum": 2,
                            },
                        },
                        "required": ["company"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_firm_profiles",
                    "description": (
                        "Lista profili firm (konkurenci/dostawcy): role, scorecard, kraj. "
                        "Filtrowanie po roli i zapytaniu."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "role": {
                                "type": "string",
                                "enum": [
                                    "competitor",
                                    "supplier",
                                    "distributor",
                                    "dealer",
                                    "customer",
                                    "prospect",
                                    "partner",
                                    "unknown",
                                ],
                            },
                            "q": {"type": "string"},
                            "sort": {
                                "type": "string",
                                "enum": [
                                    "overall",
                                    "completeness",
                                    "identity_trust",
                                    "distribution_reach",
                                    "digital_commerce",
                                    "pr_presence",
                                    "company",
                                ],
                                "default": "overall",
                            },
                            "limit": {"type": "integer", "default": 30},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_firm_profile",
                    "description": (
                        "Pełny profil firmy: tożsamość (adres/tel/email/web + weryfikacja), "
                        "finanse (sygnały publiczne), PR/social, katalogi/cenniki/e-shop, sieć, scorecard."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"company": {"type": "string"}},
                        "required": ["company"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "firm_scoreboard",
                    "description": "Tabela ocen firm (scorecard) posortowana po overall.",
                    "parameters": {
                        "type": "object",
                        "properties": {"limit": {"type": "integer", "default": 25}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "score_firm_profile",
                    "description": "Przelicz scorecard dla firmy i zapisz.",
                    "parameters": {
                        "type": "object",
                        "properties": {"company": {"type": "string"}},
                        "required": ["company"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "build_firm_profiles",
                    "description": (
                        "Zbuduj/odśwież profile z known_firms + firm_relations + cenniki + literatura + social."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "default_role": {
                                "type": "string",
                                "default": "competitor",
                            }
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "upsert_firm_profile",
                    "description": "Ręcznie uzupełnij atrybuty profilu firmy (kontakty, role, finanse).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string"},
                            "roles": {"type": "array", "items": {"type": "string"}},
                            "country": {"type": "string"},
                            "email": {"type": "string"},
                            "phone": {"type": "string"},
                            "address": {"type": "string"},
                            "website": {"type": "string"},
                            "product_focus": {"type": "string"},
                            "notes": {"type": "string"},
                            "financial_note": {"type": "string"},
                        },
                        "required": ["company"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "enrich_firm_profile",
                    "description": (
                        "Pobierz publiczną stronę firmy (contact/about) i wyciągnij "
                        "e-mail, telefon, adres, sygnały finansowe; zweryfikuj atrybuty."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string"},
                            "url": {
                                "type": "string",
                                "description": "Opcjonalny URL; domyślnie primary website z profilu",
                            },
                        },
                        "required": ["company"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "register_social_mention",
                    "description": (
                        "Dodaj publiczną wzmiankę social/PR o firmie (tytuł, snippet, platforma) "
                        "i odśwież sentiment w profilu."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string"},
                            "platform": {"type": "string"},
                            "title": {"type": "string"},
                            "snippet": {"type": "string"},
                            "url": {"type": "string"},
                        },
                        "required": ["company"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_prospects",
                    "description": (
                        "Lista potencjalnych klientów (role=prospect) z oceną szansy, "
                        "branżą, jakością parku maszyn i budżetem tooling."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "q": {"type": "string"},
                            "limit": {"type": "integer", "default": 30},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_prospect_profile",
                    "description": (
                        "Pełny profil potencjalnego klienta: budżet 4–10%, od kogo kupuje, "
                        "sprzęt/jakość, mapa produktów→narzędzia, stakeholders zakupowi."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"company": {"type": "string"}},
                        "required": ["company"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "prospect_scoreboard",
                    "description": "Tabela ocen szans sprzedażowych (prospect opportunity).",
                    "parameters": {
                        "type": "object",
                        "properties": {"limit": {"type": "integer", "default": 25}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "analyze_prospect",
                    "description": (
                        "Przeanalizuj potencjalnego klienta z URL (fetch WWW) albo z przekazanego tekstu: "
                        "branża, budżet tooling, park maszyn, dostawcy narzędzi, procesy, decydenci."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string"},
                            "url": {"type": "string"},
                            "text": {
                                "type": "string",
                                "description": "Opcjonalna treść zamiast fetch (test / Notion excerpt)",
                            },
                            "persist": {"type": "boolean", "default": True},
                        },
                        "required": ["company"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "estimate_tooling_budget",
                    "description": (
                        "Oszacuj roczny budżet na narzędzia skrawające: 4–10% kosztów produkcji "
                        "wg branży (założenie). Podaj vertical i opcjonalnie revenue_eur."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "vertical": {
                                "type": "string",
                                "enum": [
                                    "automotive",
                                    "aerospace",
                                    "medical",
                                    "mold_die",
                                    "energy",
                                    "general_machining",
                                    "electronics",
                                    "unknown",
                                ],
                            },
                            "revenue_eur": {"type": "number"},
                            "production_cost_ratio": {
                                "type": "number",
                                "default": 0.65,
                            },
                        },
                        "required": ["vertical"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ingest_prospect_seeds",
                    "description": "Wczytaj seed potencjalnych klientów z config/prospects.seed.json.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_crm_task",
                    "description": (
                        "Utwórz lokalne zadanie CRM/follow-up dla firmy (hot prospect outreach)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string"},
                            "title": {"type": "string"},
                            "reason": {"type": "string"},
                            "opportunity_score": {"type": "number"},
                            "priority": {
                                "type": "string",
                                "enum": ["low", "medium", "high", "hot"],
                            },
                        },
                        "required": ["company"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_crm_tasks",
                    "description": "Lista otwartych zadań CRM.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "default": "open"},
                            "company": {"type": "string"},
                            "limit": {"type": "integer", "default": 30},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "sync_crm_from_prospects",
                    "description": (
                        "Utwórz CRM tasks z prospect scoreboard (domyślnie opportunity≥60)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "min_opportunity": {"type": "number", "default": 60},
                            "limit": {"type": "integer", "default": 20},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "push_crm_to_notion",
                    "description": (
                        "Opcjonalny push otwartych CRM tasks (hot/high) do Notion jako child pages "
                        "pod sources.notion.crm_parent_page. Wymaga NOTION_TOKEN."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "default": 20},
                            "only_hot": {"type": "boolean", "default": True},
                            "dry_run": {"type": "boolean", "default": False},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_suppliers",
                    "description": (
                        "Lista dostawców/dystrybutorów/dealerów z oceną kanału i pokrycia marek."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "q": {"type": "string"},
                            "limit": {"type": "integer", "default": 30},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "supplier_scoreboard",
                    "description": "Tabela ocen dostawców (supplier_overall, channel_reach, brand_coverage).",
                    "parameters": {
                        "type": "object",
                        "properties": {"limit": {"type": "integer", "default": 25}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "score_suppliers",
                    "description": "Przelicz i zapisz scorecard dostawców w firm_profiles.json.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "resolve_firm_duplicates",
                    "description": (
                        "Golden record: znajdź / scal duplikaty profili firm "
                        "(domyślnie dry_run=true)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "dry_run": {"type": "boolean", "default": True},
                            "threshold": {"type": "number", "default": 0.92},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_run_metrics",
                    "description": "Ostatnie metryki runów (knowledge counts, new_items, mode).",
                    "parameters": {
                        "type": "object",
                        "properties": {"limit": {"type": "integer", "default": 15}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "refresh_change_digest",
                    "description": (
                        "Porównaj scoreboardy firm/prospect/supplier z poprzednim runem "
                        "(deltas bez LLM)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "min_delta": {"type": "number", "default": 5},
                            "limit": {"type": "integer", "default": 25},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "export_knowledge_pack",
                    "description": (
                        "Eksport operatorski scoreboardów + CRM + digest do katalogu "
                        "(Markdown + JSON)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "out_dir": {
                                "type": "string",
                                "description": "Domyślnie reports/knowledge_export pod data_dir parent",
                            },
                            "limit": {"type": "integer", "default": 30},
                        },
                    },
                },
            },

            {
                "type": "function",
                "function": {
                    "name": "list_parse_rules",
                    "description": (
                        "Lista nauczonych reguł parsera per host "
                        "(preferred_method, css_selector, success/fail)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "default": 50},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "upsert_parse_rule",
                    "description": (
                        "Zapisz/aktualizuj regułę parsera dla hosta. "
                        "Używaj gdy fetch_and_parse dał mało tekstu — podaj CSS lub preferred_method, "
                        "potem ponów fetch_and_parse z bypass_cache=true."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host_or_url": {"type": "string"},
                            "preferred_method": {
                                "type": "string",
                                "enum": ["auto", "trafilatura", "bs4", "css"],
                            },
                            "css_selector": {"type": "string"},
                            "drop_selectors": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "min_chars": {"type": "integer", "default": 120},
                            "notes": {"type": "string"},
                        },
                        "required": ["host_or_url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "rate_parse",
                    "description": (
                        "Oceń jakość sparsowanej strony (0–1). "
                        "Wysoka ocena wzmacnia regułę hosta; niska — sygnalizuje potrzebę upsert_parse_rule."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "quality_0_to_1": {"type": "number"},
                            "parse_method": {
                                "type": "string",
                                "enum": ["trafilatura", "bs4", "css", "auto"],
                            },
                            "css_selector": {"type": "string"},
                            "notes": {"type": "string"},
                        },
                        "required": ["url", "quality_0_to_1"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_parsing_skills",
                    "description": (
                        "Wspólna baza umiejętności parsowania (shared across agents): "
                        "targi, artykuły, katalogi, ekstrakcja firm/powiązań. "
                        "Agenty uczą się razem i ulepszają te skills."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "category": {
                                "type": "string",
                                "enum": list(SKILL_CATEGORIES),
                            },
                            "limit": {"type": "integer", "default": 50},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "match_parsing_skills",
                    "description": (
                        "Dobierz najlepsze wspólne skills do URL / kind "
                        "(trade_fair, magazine, portal, catalog…)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "kind": {"type": "string"},
                            "limit": {"type": "integer", "default": 5},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "learn_parsing_skill",
                    "description": (
                        "Naucz / zapisz nową współdzieloną umiejętność parsowania "
                        "(CSS, keywords, hints, patterns). Inne agenty od razu z niej korzystają."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "skill_id": {"type": "string"},
                            "category": {
                                "type": "string",
                                "enum": list(SKILL_CATEGORIES),
                            },
                            "description": {"type": "string"},
                            "preferred_method": {
                                "type": "string",
                                "enum": list(PARSE_METHODS),
                            },
                            "css_selectors": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "drop_selectors": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "link_keywords": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "extract_patterns": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "hints": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "applies_to_hosts": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "applies_to_kinds": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "url_contains": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "notes": {"type": "string"},
                            "examples": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "taught_by": {"type": "string", "default": "parsing_agent"},
                            "merge": {"type": "boolean", "default": True},
                        },
                        "required": ["name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "improve_parsing_skill",
                    "description": (
                        "Ulepsz istniejącą umiejętność (nowy CSS / metoda / hints) "
                        "i podbij version — wspólna ewolucja parsera."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_id": {"type": "string"},
                            "preferred_method": {
                                "type": "string",
                                "enum": list(PARSE_METHODS),
                            },
                            "css_selectors": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "drop_selectors": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "link_keywords": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "extract_patterns": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "hints": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "applies_to_hosts": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "applies_to_kinds": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "url_contains": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "notes": {"type": "string"},
                            "taught_by": {"type": "string", "default": "parsing_agent"},
                        },
                        "required": ["skill_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "rate_parsing_skill",
                    "description": (
                        "Oceń skill po użyciu (0–1). Wysoka ocena wzmacnia skill; "
                        "niska → improve_parsing_skill."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_id": {"type": "string"},
                            "quality_0_to_1": {"type": "number"},
                            "notes": {"type": "string"},
                            "taught_by": {"type": "string"},
                        },
                        "required": ["skill_id", "quality_0_to_1"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "promote_host_skill",
                    "description": (
                        "Wypromuj udaną regułę hosta do wspólnej umiejętności "
                        "(inne agenty / kolejne runy od razu z niej korzystają)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "host_or_url": {"type": "string"},
                            "preferred_method": {
                                "type": "string",
                                "enum": list(PARSE_METHODS),
                            },
                            "css_selector": {"type": "string"},
                            "category": {
                                "type": "string",
                                "enum": list(SKILL_CATEGORIES),
                            },
                            "notes": {"type": "string"},
                            "taught_by": {"type": "string"},
                        },
                        "required": ["host_or_url"],
                    },
                },
            },
        ]

    def call(self, name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                arguments = {}
        handler = self._handlers.get(name)
        if not handler:
            return {"ok": False, "error": f"unknown tool: {name}"}
        # AI governance (policy-as-code) — przed cybersecurity tool policy
        gov = self._governance.authorize_tool(name, arguments or {})
        if not gov.allowed:
            return {
                "ok": False,
                "error": gov.reason,
                "tool": name,
                "governance": True,
                "effect": gov.effect,
                "matched_rules": gov.matched_rules,
            }
        allowed, reason = self._security.tool_allowed(name)
        if not allowed:
            return {"ok": False, "error": reason, "tool": name, "security": True}
        # SSRF pre-check for common url args
        url = str((arguments or {}).get("url") or "").strip()
        if url and name in {
            "fetch_and_parse",
            "fetch_pdf_text",
            "parse_media_page",
            "parse_social_post",
            "extract_fair_exhibitors",
            "extract_domain_context",
            "extract_product_tech_schema",
        }:
            try:
                validate_fetch_url(
                    url,
                    allowed_hosts=self._security.allowed_hosts or None,
                    allow_private=not self._security.block_private_networks,
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": safe_error(exc), "tool": name, "security": True}
            fetch_gov = self._governance.authorize_fetch(url)
            if not fetch_gov.allowed:
                return {
                    "ok": False,
                    "error": fetch_gov.reason,
                    "tool": name,
                    "governance": True,
                    "effect": fetch_gov.effect,
                    "matched_rules": fetch_gov.matched_rules,
                }
        try:
            result = handler(arguments or {})
            if self._security.redact_traces and isinstance(result, dict):
                # lekka redakcja stringów w wyniku
                for key, val in list(result.items()):
                    if isinstance(val, str) and key in {
                        "excerpt",
                        "text",
                        "content",
                        "error",
                        "summary",
                    }:
                        result[key] = redact_secrets(
                            val, max_chars=self._security.trace_tool_result_max_chars
                        )
            if gov.effect in {"monitor", "redact"} and isinstance(result, dict):
                result.setdefault("governance", {"effect": gov.effect, "rules": gov.matched_rules})
            return result
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": safe_error(exc), "tool": name}

    def security_status_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return security_status(self._security)

    def governance_status_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._governance.status()

    def list_candidates(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit") or 20)
        rows = [
            {
                "title": item.title,
                "url": item.url,
                "source": item.source,
                "summary": (item.summary or "")[:280],
                "relevance_score": item.relevance_score,
                "tags": item.tags,
                "domain_context": (item.analysis or {}).get("domain_context"),
            }
            for item in self.candidates[:limit]
        ]
        return {"ok": True, "count": len(rows), "candidates": rows}


    def fetch_and_parse(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "url required"}
        force_method = str(args.get("force_method") or "").strip() or None
        css_selector = args.get("css_selector")
        if css_selector is not None:
            css_selector = str(css_selector).strip() or None
        bypass_cache = bool(args.get("bypass_cache"))
        if url in self.parsed_cache and not bypass_cache and not force_method and css_selector is None:
            doc = self.parsed_cache[url]
        else:
            doc = self.parser.parse_url(
                url, force_method=force_method, css_selector=css_selector
            )
            self.parsed_cache[url] = doc
            for item in self.candidates:
                if item.url == url and doc.ok:
                    item.content = doc.text
                    if doc.title:
                        item.title = doc.title
                    if doc.published_at:
                        item.published_at = doc.published_at
                    from market_agents.ontology import attach_domain_context

                    attach_domain_context(item, ontology=self._get_ontology(), ingest=True)
            if self.config.agents.agentic.learn_parse_rules:
                self._parse_rules.save(self.config.data_path)
        hint = None
        if (not doc.ok) or len(doc.text) < 200:
            hint = (
                "Słaby parse — zaproponuj upsert_parse_rule "
                "(preferred_method/css_selector), potem fetch_and_parse z bypass_cache=true."
            )
        domain_ctx = None
        if doc.ok and doc.text:
            from market_agents.ontology import extract_domain_context

            domain_ctx = extract_domain_context(doc.text)
            # skróć do idów w odpowiedzi toola
            domain_ctx = {
                "materials": [m["id"] for m in domain_ctx.get("materials") or []],
                "machines": [m["id"] for m in domain_ctx.get("machines") or []],
                "coolants": [c["id"] for c in domain_ctx.get("coolants") or []],
                "processes": [p["id"] for p in domain_ctx.get("processes") or []],
                "parameters": [p["id"] for p in domain_ctx.get("parameters") or []],
                "summary": domain_ctx.get("summary"),
            }
        return {
            "ok": doc.ok,
            "url": url,
            "title": doc.title,
            "parse_method": doc.parse_method,
            "chars": len(doc.text),
            "excerpt": doc.excerpt,
            "links": doc.links[:10],
            "error": doc.error,
            "meta": doc.meta,
            "hint": hint,
            "domain_context": domain_ctx,
            "learn_parse_rules": bool(self.config.agents.agentic.learn_parse_rules),
        }


    def batch_parse(self, args: dict[str, Any]) -> dict[str, Any]:
        urls = [u for u in (args.get("urls") or []) if isinstance(u, str)]
        urls = urls[: self.config.agents.agentic.max_deep_parses]
        # Anty-bulk: nigdy nie przekraczaj global_concurrency z crawl policy
        crawl_cap = int(getattr(self.config.agents.crawl, "global_concurrency", 2) or 2)
        workers = min(
            self.config.agents.agentic.parallel_fetches,
            crawl_cap,
            max(len(urls), 1),
        )
        # Przy adaptive crawl i wielu URL z tego samego hosta — sekwencyjnie bezpieczniej
        hosts = {self._fetcher.host_of(u) for u in urls}
        if len(hosts) <= 1:
            workers = 1
        results: list[dict[str, Any]] = []

        def _one(u: str) -> dict[str, Any]:
            return self.fetch_and_parse({"url": u})

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_one, u): u for u in urls}
            for fut in as_completed(futs):
                results.append(fut.result())
        return {
            "ok": True,
            "results": results,
            "workers": workers,
            "polite": True,
            "crawl_cap": crawl_cap,
        }

    def crawl_status(self, args: dict[str, Any]) -> dict[str, Any]:
        host_or_url = str(args.get("host_or_url") or "").strip() or None
        status = self._fetcher.status(host_or_url)
        status["hint"] = (
            "Przy in_cooldown=true poczekaj / zmień źródło. "
            "Nie spamuj hosta — delay rośnie adaptacyjnie po 429/403/wolnych odpowiedziach."
        )
        return status

    def extract_market_intel(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "")
        payload = {
            "url": url,
            "signal_type": args.get("signal_type"),
            "impact": args.get("impact"),
            "summary": args.get("summary"),
            "entities": args.get("entities") or [],
            "numbers": args.get("numbers") or [],
            "action": args.get("action"),
            "relevance_0_to_1": float(args.get("relevance_0_to_1") or 0),
        }
        self.structured.append(payload)
        self.memory.add("intel", str(payload.get("summary") or ""), meta=payload)
        for item in self.candidates:
            if item.url == url:
                item.analysis = payload
                item.relevance_score = max(
                    item.relevance_score, float(payload["relevance_0_to_1"])
                )
                break
        return {"ok": True, "saved": payload}

    def search_memory(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "")
        limit = int(args.get("limit") or 6)
        return {"ok": True, "hits": self.memory.search(query, limit=limit)}

    def remember(self, args: dict[str, Any]) -> dict[str, Any]:
        self.memory.add(str(args.get("kind") or "note"), str(args.get("content") or ""))
        return {"ok": True}

    def score_item(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "")
        score = float(args.get("score") or 0)
        reason = str(args.get("reason") or "")
        for item in self.candidates:
            if item.url == url:
                item.relevance_score = max(item.relevance_score, score)
                item.analysis = {
                    **(item.analysis or {}),
                    "score_reason": reason,
                    "relevance_0_to_1": score,
                }
                break
        return {"ok": True, "url": url, "score": score}

    def search_notion(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "")
        limit = int(args.get("limit") or 10)
        hits = self._get_notion().search(query, limit=limit)
        self.memory.add("notion_search", query, meta={"hits": len(hits)})
        return {"ok": True, "hits": hits}

    def fetch_notion_page(self, args: dict[str, Any]) -> dict[str, Any]:
        page_id_or_url = str(args.get("page_id_or_url") or "")
        doc = self._get_notion().fetch_page_text(page_id_or_url)
        if doc.get("ok"):
            self.memory.add(
                "notion_page",
                f"{doc.get('title')}: {(doc.get('excerpt') or '')[:500]}",
                meta={"url": doc.get("url"), "id": doc.get("id")},
            )
        return doc

    def search_r2(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "")
        limit = int(args.get("limit") or 15)
        hits = self._get_r2().search(query, limit=limit)
        return {"ok": True, "hits": hits}

    def fetch_r2_object(self, args: dict[str, Any]) -> dict[str, Any]:
        key = str(args.get("key") or "").strip()
        max_chars = int(args.get("max_chars") or 12000)
        if not key:
            return {"ok": False, "error": "key required"}
        if self._security.enforce_r2_prefix:
            try:
                key = enforce_r2_key_prefix(key, self.config.sources.cloudflare_r2.prefix)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": safe_error(exc), "security": True}
        text = redact_secrets(self._get_r2().get_text(key), max_chars=max_chars)
        return {
            "ok": True,
            "key": key,
            "chars": len(text),
            "excerpt": text[:max_chars],
        }

    def list_catalog_sources(self, args: dict[str, Any]) -> dict[str, Any]:
        kind_filter = str(args.get("kind") or "").strip().lower()
        rows = []
        for src in self.config.sources.catalogs:
            if kind_filter and src.kind.lower() != kind_filter:
                continue
            rows.append(
                {
                    "name": src.name,
                    "brand": src.brand,
                    "kind": src.kind,
                    "url": src.url,
                }
            )
        return {"ok": True, "count": len(rows), "sources": rows}

    def _resolve_catalog(self, args: dict[str, Any]) -> CatalogCollector:
        source_name = str(args.get("source_name") or "").strip().lower()
        url = str(args.get("url") or "").strip()
        if source_name:
            for src in self.config.sources.catalogs:
                if src.name.lower() == source_name or (
                    src.brand and src.brand.lower() == source_name
                ):
                    return CatalogCollector(src)
        if url:
            # ad-hoc źródło z URL
            kind = "pdf" if ".pdf" in url.lower() else "ecatalog"
            return CatalogCollector(
                CatalogSource(name="ad-hoc", url=url, kind=kind, brand="unknown")
            )
        if self.config.sources.catalogs:
            return CatalogCollector(self.config.sources.catalogs[0])
        raise RuntimeError("Brak sources.catalogs w config — dodaj e-catalog/PDF/eshop")

    def discover_catalog_assets(self, args: dict[str, Any]) -> dict[str, Any]:
        max_links = int(args.get("max_links") or 30)
        asset_type = str(args.get("asset_type") or "").strip().lower()
        collector = self._resolve_catalog(args)
        assets = collector.discover_assets(max_links=max_links)
        if asset_type:
            assets = [
                a
                for a in assets
                if str(a.get("asset_type") or "").lower() == asset_type
            ]
        self.memory.add(
            "catalog_discover",
            f"{collector.name}: {len(assets)} assets",
            meta={"source": collector.name, "count": len(assets), "asset_type": asset_type or None},
        )
        return {
            "ok": True,
            "source": collector.name,
            "count": len(assets),
            "asset_type": asset_type or None,
            "assets": assets,
        }


    def fetch_pdf_text(self, args: dict[str, Any]) -> dict[str, Any]:
        max_pages = int(args.get("max_pages") or 8)
        max_chars = int(args.get("max_chars") or 12000)
        url = str(args.get("url") or "").strip() or None
        collector = self._resolve_catalog(args)
        result = collector.fetch_pdf_text(url=url, max_pages=max_pages, max_chars=max_chars)
        if result.get("ok"):
            self.memory.add(
                "pdf",
                f"{result.get('filename')}: {(result.get('text_excerpt') or '')[:400]}",
                meta={"url": result.get("url")},
            )
        return result

    def discover_pricelists(self, args: dict[str, Any]) -> dict[str, Any]:
        """Skan hubów katalogów pod kątem dostępnych cenników."""
        max_sources = int(args.get("max_sources") or 12)
        max_links = int(args.get("max_links_per_source") or 40)
        register = bool(args.get("register", True))
        source_name = str(args.get("source_name") or "").strip()
        url = str(args.get("url") or "").strip()

        targets: list[CatalogSource] = []
        if source_name or url:
            collector = self._resolve_catalog(args)
            # odtwórz CatalogSource z collectora
            for src in self.config.sources.catalogs:
                if src.name == collector.name or (src.brand and src.brand == getattr(collector.source, "brand", None)):
                    targets.append(src)
                    break
            else:
                targets.append(
                    CatalogSource(
                        name=collector.name,
                        url=url or collector.source.url,
                        brand=getattr(collector.source, "brand", None),
                        kind="pricelist",
                    )
                )
        else:
            targets = list(self.config.sources.catalogs)[:max_sources]

        registry = PricelistRegistry.load(self.config.data_path)
        found: list[dict[str, Any]] = []
        per_source: list[dict[str, Any]] = []
        for src in targets:
            collector = CatalogCollector(src)
            assets = collector.discover_assets(max_links=max_links)
            prices = [
                a
                for a in assets
                if isinstance(a, dict) and str(a.get("asset_type") or "").lower() == "pricelist"
            ]
            per_source.append(
                {
                    "source": src.name,
                    "brand": src.brand,
                    "hub": src.url,
                    "pricelists": len(prices),
                    "assets_scanned": len(assets),
                }
            )
            for row in prices:
                row = dict(row)
                row["brand"] = row.get("brand") or src.brand or src.name
                row["source_name"] = src.name
                found.append(row)
            if register and prices:
                registry.ingest_discovered(
                    prices,
                    brand=src.brand or src.name,
                    source=src.name,
                )

        if register:
            registry.save(self.config.data_path)

        self.memory.add(
            "pricelist_discover",
            f"Znaleziono {len(found)} cenników w {len(targets)} źródłach",
            meta={"count": len(found), "sources": len(targets)},
        )
        return {
            "ok": True,
            "count": len(found),
            "sources_scanned": len(targets),
            "per_source": per_source,
            "pricelists": found[:80],
            "registered_total": len(registry.items) if register else None,
        }

    def list_available_pricelists(self, args: dict[str, Any]) -> dict[str, Any]:
        registry = PricelistRegistry.load(self.config.data_path)
        items = registry.list(
            brand=str(args.get("brand") or "") or None,
            access=str(args.get("access") or "") or None,
            q=str(args.get("q") or "") or None,
            limit=int(args.get("limit") or 50),
        )
        return {
            "ok": True,
            "count": len(items),
            "total_registered": len(registry.items),
            "pricelists": [i.to_dict() for i in items],
        }

    def register_pricelist(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "url jest wymagany"}
        registry = PricelistRegistry.load(self.config.data_path)
        item = registry.upsert(
            url=url,
            title=str(args.get("title") or "") or None,
            brand=str(args.get("brand") or "") or None,
            source="agent",
            access=str(args.get("access") or "public") or "public",
            currency=str(args.get("currency") or "") or None,
            year=str(args.get("year") or "") or None,
            notes=str(args.get("notes") or ""),
            confirmed=bool(args.get("confirmed", True)),
        )
        path = registry.save(self.config.data_path)
        self.memory.add(
            "pricelist_register",
            f"{item.brand or '?'}: {item.title} → {item.url}",
            meta={"id": item.id, "url": item.url},
        )
        return {"ok": True, "pricelist": item.to_dict(), "path": str(path)}


    def discover_product_tech(self, args: dict[str, Any]) -> dict[str, Any]:
        """Skan hubów katalogów pod kątem handbooków / cutting data / ISO 13399."""
        max_sources = int(args.get("max_sources") or 12)
        max_links = int(args.get("max_links_per_source") or 40)
        register = bool(args.get("register", True))
        source_name = str(args.get("source_name") or "").strip()
        url = str(args.get("url") or "").strip()

        targets: list[CatalogSource] = []
        if source_name or url:
            collector = self._resolve_catalog(args)
            for src in self.config.sources.catalogs:
                if src.name == collector.name or (
                    src.brand and src.brand == getattr(collector.source, "brand", None)
                ):
                    targets.append(src)
                    break
            else:
                targets.append(
                    CatalogSource(
                        name=collector.name,
                        url=url or collector.source.url,
                        brand=getattr(collector.source, "brand", None),
                        kind="tech_datasheet",
                    )
                )
        else:
            targets = list(self.config.sources.catalogs)[:max_sources]

        registry = ProductTechRegistry.load(self.config.data_path)
        found: list[dict[str, Any]] = []
        per_source: list[dict[str, Any]] = []
        tech_types = set(TECH_KINDS)
        for src in targets:
            collector = CatalogCollector(src)
            assets = collector.discover_assets(max_links=max_links)
            tech_assets = []
            for a in assets:
                if not isinstance(a, dict):
                    continue
                atype = str(a.get("asset_type") or "").lower()
                if atype in tech_types:
                    tech_assets.append(a)
                    continue
                inferred = classify_tech_kind(str(a.get("url") or ""), str(a.get("text") or ""))
                if inferred:
                    row = dict(a)
                    row["asset_type"] = inferred
                    tech_assets.append(row)
            per_source.append(
                {
                    "source": src.name,
                    "brand": src.brand,
                    "hub": src.url,
                    "product_tech": len(tech_assets),
                    "assets_scanned": len(assets),
                }
            )
            for row in tech_assets:
                row = dict(row)
                row["brand"] = row.get("brand") or src.brand or src.name
                row["source_name"] = src.name
                found.append(row)
            if register and tech_assets:
                registry.ingest_discovered(
                    tech_assets,
                    brand=src.brand or src.name,
                    source=src.name,
                )

        if register:
            registry.save(self.config.data_path)

        self.memory.add(
            "product_tech_discover",
            f"Znaleziono {len(found)} źródeł tech w {len(targets)} hubach",
            meta={"count": len(found), "sources": len(targets)},
        )
        return {
            "ok": True,
            "count": len(found),
            "sources_scanned": len(targets),
            "per_source": per_source,
            "items": found[:80],
            "registered_total": len(registry.items) if register else None,
            "policy": "schema/layout only — nie kopiować tabel vc/fz do CutData",
        }

    def list_product_tech(self, args: dict[str, Any]) -> dict[str, Any]:
        registry = ProductTechRegistry.load(self.config.data_path)
        items = registry.list(
            brand=str(args.get("brand") or "") or None,
            kind=str(args.get("kind") or "") or None,
            access=str(args.get("access") or "") or None,
            q=str(args.get("q") or "") or None,
            limit=int(args.get("limit") or 50),
        )
        return {
            "ok": True,
            "count": len(items),
            "total_registered": len(registry.items),
            "items": [i.to_dict() for i in items],
        }

    def register_product_tech(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "url jest wymagany"}
        kind = str(args.get("kind") or "").strip() or (
            classify_tech_kind(url, str(args.get("title") or "")) or "tech_datasheet"
        )
        registry = ProductTechRegistry.load(self.config.data_path)
        item = registry.upsert(
            url=url,
            title=str(args.get("title") or "") or None,
            brand=str(args.get("brand") or "") or None,
            source="agent",
            kind=kind,
            access=str(args.get("access") or "public") or "public",
            year=str(args.get("year") or "") or None,
            notes=str(args.get("notes") or ""),
            confirmed=bool(args.get("confirmed", True)),
        )
        path = registry.save(self.config.data_path)
        self.memory.add(
            "product_tech_register",
            f"{item.brand or '?'}: {item.kind} — {item.title}",
            meta={"id": item.id, "url": item.url, "kind": item.kind},
        )
        return {"ok": True, "item": item.to_dict(), "path": str(path)}

    def extract_product_tech_schema(self, args: dict[str, Any]) -> dict[str, Any]:
        text = str(args.get("text") or "")
        url = str(args.get("url") or "").strip() or None
        brand = str(args.get("brand") or "") or None
        register = bool(args.get("register", True))
        if not text and url:
            max_pages = int(args.get("max_pages") or 10)
            collector = self._resolve_catalog({**args, "url": url})
            pdf = collector.fetch_pdf_text(url=url, max_pages=max_pages, max_chars=20000)
            if not pdf.get("ok"):
                return {"ok": False, "error": pdf.get("error") or "nie udało się pobrać PDF"}
            text = str(pdf.get("text_excerpt") or pdf.get("text") or "")
            if not brand:
                brand = str(pdf.get("brand") or "") or None
        if not text.strip():
            return {"ok": False, "error": "podaj text albo url PDF"}
        schema = extract_tech_schema(text)
        kind = classify_tech_kind(url or "", text[:500]) or "handbook"
        saved = None
        if register and url:
            registry = ProductTechRegistry.load(self.config.data_path)
            item = registry.upsert(
                url=url,
                title=str(args.get("title") or "") or None,
                brand=brand,
                source=str(args.get("source_name") or "agent") or "agent",
                kind=kind,
                access="public",
                notes="schema extracted",
                schema_summary={
                    "fields_present": schema.get("fields_present"),
                    "field_count": schema.get("field_count"),
                    "richness_0_to_1": schema.get("richness_0_to_1"),
                    "has_iso13399": schema.get("has_iso13399"),
                    "operations_mentioned": schema.get("operations_mentioned"),
                },
                confirmed=True,
            )
            registry.save(self.config.data_path)
            saved = item.to_dict()
        self.memory.add(
            "product_tech_schema",
            f"schema fields={schema.get('fields_present')} richness={schema.get('richness_0_to_1')}",
            meta={"url": url, "kind": kind},
        )
        return {
            "ok": True,
            "kind": kind,
            "schema": schema,
            "registered": saved,
            "policy": schema.get("purpose"),
        }


    def list_media_sources(self, args: dict[str, Any]) -> dict[str, Any]:
        kind_filter = str(args.get("kind") or "").strip().lower()
        enabled_only = args.get("enabled_only", True)
        rows = []
        for src in self.config.sources.media:
            if enabled_only and not src.enabled:
                continue
            if kind_filter and src.kind.lower() != kind_filter:
                continue
            rows.append(
                {
                    "name": src.name,
                    "kind": src.kind,
                    "brand": src.brand,
                    "url": src.url,
                    "extract_exhibitors": src.extract_exhibitors,
                    "enabled": src.enabled,
                }
            )
        return {"ok": True, "count": len(rows), "sources": rows}

    def _resolve_media(self, args: dict[str, Any]) -> IndustryMediaCollector:
        source_name = str(args.get("source_name") or "").strip().lower()
        url = str(args.get("url") or "").strip()
        kind = str(args.get("kind") or "portal").strip().lower() or "portal"
        if source_name:
            for src in self.config.sources.media:
                if src.name.lower() == source_name or (
                    src.brand and src.brand.lower() == source_name
                ):
                    return IndustryMediaCollector(src)
        if url:
            return IndustryMediaCollector(
                IndustryMediaSource(
                    name="ad-hoc",
                    url=url,
                    kind=kind if kind in {"trade_fair", "magazine", "portal"} else "portal",
                    extract_exhibitors=kind == "trade_fair",
                )
            )
        enabled = [s for s in self.config.sources.media if s.enabled]
        if enabled:
            return IndustryMediaCollector(enabled[0])
        raise RuntimeError(
            "Brak sources.media w config — dodaj targi / czasopisma / portale"
        )

    def discover_media_links(self, args: dict[str, Any]) -> dict[str, Any]:
        max_links = int(args.get("max_links") or 40)
        collector = self._resolve_media(args)
        result = collector.discover_links(max_links=max_links)
        self.memory.add(
            "media_discover",
            f"{collector.name}: {result.get('count', 0)} links, "
            f"{len(result.get('exhibitors') or [])} exhibitors",
            meta={
                "source": collector.name,
                "kind": result.get("kind"),
                "url": result.get("url"),
            },
        )
        return result

    def parse_media_page(self, args: dict[str, Any]) -> dict[str, Any]:
        max_chars = int(args.get("max_chars") or 12000)
        url = str(args.get("url") or "").strip() or None
        collector = self._resolve_media(args)
        result = collector.parse_page(url=url, max_chars=max_chars)
        if result.get("ok"):
            self.memory.add(
                "media_parse",
                f"{result.get('title')}: {(result.get('excerpt') or '')[:400]}",
                meta={
                    "url": result.get("url"),
                    "media_kind": result.get("media_kind"),
                    "firms": result.get("candidate_firms") or [],
                },
            )
        return result

    def extract_fair_exhibitors(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit") or 80)
        check_known = args.get("check_known", True)
        url = str(args.get("url") or "").strip()
        if not url and args.get("source_name"):
            collector = self._resolve_media({**args, "kind": "trade_fair"})
            hub = collector.discover_links(max_links=5)
            names = list(hub.get("exhibitors") or [])
            url = collector.source.url
        elif url:
            collector = self._resolve_media(
                {"url": url, "kind": str(args.get("kind") or "trade_fair")}
            )
            page = collector.parse_page(url=url, max_chars=8000)
            names = list(page.get("exhibitors") or [])
            if len(names) < 5:
                names = extract_exhibitors_from_html(
                    collector._fetch(url), limit=limit  # noqa: SLF001
                )
        else:
            return {"ok": False, "error": "url or source_name required"}

        names = names[:limit]
        rows: list[dict[str, Any]] = [{"company": n} for n in names]
        if check_known and names:
            known = self.check_firm_known({"names": names})
            by_name = {
                str(r.get("name") or "").lower(): r
                for r in (known.get("results") or [])
            }
            for row in rows:
                match = by_name.get(row["company"].lower()) or {}
                row["known"] = bool(match.get("known"))
                if match.get("match"):
                    row["match"] = match.get("match")
        new_count = sum(1 for r in rows if not r.get("known"))
        self.memory.add(
            "fair_exhibitors",
            f"{url}: {len(rows)} exhibitors ({new_count} potentially new)",
            meta={"url": url, "count": len(rows), "new": new_count},
        )
        return {
            "ok": True,
            "url": url,
            "count": len(rows),
            "new_count": new_count,
            "exhibitors": rows,
        }


    def list_social_sources(self, args: dict[str, Any]) -> dict[str, Any]:
        platform = str(args.get("platform") or "").strip().lower()
        if platform == "twitter":
            platform = "x"
        enabled_only = args.get("enabled_only", True)
        rows = []
        for src in self.config.sources.social:
            if enabled_only and not src.enabled:
                continue
            plat = (src.platform or detect_platform(src.url) or "other").lower()
            if plat == "twitter":
                plat = "x"
            if platform and plat != platform:
                continue
            rows.append(
                {
                    "name": src.name,
                    "platform": plat,
                    "brand": src.brand,
                    "url": src.url,
                    "channel_id": src.channel_id,
                    "feed_url": src.feed_url,
                    "enabled": src.enabled,
                }
            )
        return {"ok": True, "count": len(rows), "sources": rows}

    def _resolve_social(self, args: dict[str, Any]) -> SocialMediaCollector:
        source_name = str(args.get("source_name") or "").strip().lower()
        url = str(args.get("url") or "").strip()
        platform = str(args.get("platform") or "").strip().lower() or "other"
        if platform == "twitter":
            platform = "x"
        if source_name:
            for src in self.config.sources.social:
                if src.name.lower() == source_name or (
                    src.brand and src.brand.lower() == source_name
                ):
                    return SocialMediaCollector(src, fetcher=self._fetcher)
        if url:
            plat = platform if platform in SOCIAL_PLATFORMS else detect_platform(url)
            return SocialMediaCollector(
                SocialMediaSource(
                    name="ad-hoc",
                    url=url,
                    platform=plat,
                    brand="unknown",
                ),
                fetcher=self._fetcher,
            )
        enabled = [s for s in self.config.sources.social if s.enabled]
        if enabled:
            return SocialMediaCollector(enabled[0], fetcher=self._fetcher)
        raise RuntimeError(
            "Brak sources.social w config — dodaj YouTube/LinkedIn/X publiczne profile"
        )

    def discover_social_posts(self, args: dict[str, Any]) -> dict[str, Any]:
        max_items = int(args.get("max_items") or 20)
        collector = self._resolve_social(args)
        result = collector.discover_posts(max_items=max_items)
        self.memory.add(
            "social_discover",
            f"{collector.name}: {result.get('count', 0)} posts mode={result.get('mode')}",
            meta={
                "source": collector.name,
                "platform": result.get("platform"),
                "url": result.get("url"),
            },
        )
        return result

    def parse_social_post(self, args: dict[str, Any]) -> dict[str, Any]:
        max_chars = int(args.get("max_chars") or 8000)
        url = str(args.get("url") or "").strip() or None
        collector = self._resolve_social(args)
        result = collector.parse_post(url=url, max_chars=max_chars)
        if result.get("ok"):
            self.memory.add(
                "social_parse",
                f"{result.get('title')}: hints={result.get('signal_hints')}",
                meta={
                    "url": result.get("url"),
                    "platform": result.get("social_platform"),
                    "hints": result.get("signal_hints") or [],
                },
            )
        return result

    def list_literature_sources(self, args: dict[str, Any]) -> dict[str, Any]:
        kind = str(args.get("kind") or "").strip().lower()
        enabled_only = args.get("enabled_only", True)
        rows = []
        for src in self.config.sources.literature:
            if enabled_only and not src.enabled:
                continue
            sk = (src.kind or "mixed").lower()
            if kind and sk != kind and not (kind == "article" and sk in {"article", "mixed"}):
                if kind != sk:
                    continue
            rows.append(
                {
                    "name": src.name,
                    "kind": sk,
                    "brand": src.brand,
                    "publisher": src.publisher,
                    "url": src.url,
                    "feed_url": src.feed_url,
                    "language": src.language,
                    "enabled": src.enabled,
                }
            )
        return {"ok": True, "count": len(rows), "sources": rows}

    def _resolve_literature(self, args: dict[str, Any]) -> LiteratureCollector:
        source_name = str(args.get("source_name") or "").strip().lower()
        url = str(args.get("url") or "").strip()
        kind = str(args.get("kind") or "").strip().lower() or "mixed"
        if source_name:
            for src in self.config.sources.literature:
                if src.name.lower() == source_name or (
                    src.brand and src.brand.lower() == source_name
                ):
                    return LiteratureCollector(src, fetcher=self._fetcher)
        if url:
            return LiteratureCollector(
                LiteratureSource(
                    name="ad-hoc",
                    url=url,
                    kind=kind if kind in {*LITERATURE_KINDS, "mixed"} else "mixed",
                    brand="unknown",
                ),
                fetcher=self._fetcher,
            )
        enabled = [s for s in self.config.sources.literature if s.enabled]
        if enabled:
            return LiteratureCollector(enabled[0], fetcher=self._fetcher)
        raise RuntimeError(
            "Brak sources.literature w config — dodaj huby książek/artykułów/wideo"
        )

    def discover_literature(self, args: dict[str, Any]) -> dict[str, Any]:
        max_items = int(args.get("max_items") or 25)
        register = args.get("register", True)
        collector = self._resolve_literature(args)
        result = collector.discover_items(max_items=max_items)
        registered = 0
        if register and result.get("ok"):
            registry = LiteratureRegistry.load(self.config.data_path)
            saved = registry.ingest_discovered(
                list(result.get("items") or []),
                source=collector.name,
                auto_save_dir=self.config.data_path,
            )
            registered = len(saved)
            result["registered"] = registered
            result["registry_path"] = str(LiteratureRegistry.path_for(self.config.data_path))
        self.memory.add(
            "literature_discover",
            f"{collector.name}: {result.get('count', 0)} items registered={registered}",
            meta={
                "source": collector.name,
                "kind": getattr(collector, "kind", None),
                "url": result.get("url"),
            },
        )
        return result

    def list_literature(self, args: dict[str, Any]) -> dict[str, Any]:
        registry = LiteratureRegistry.load(self.config.data_path)
        items = registry.list(
            kind=str(args.get("kind") or "").strip() or None,
            topic=str(args.get("topic") or "").strip() or None,
            q=str(args.get("q") or "").strip() or None,
            limit=int(args.get("limit") or 50),
        )
        return {
            "ok": True,
            "count": len(items),
            "total": len(registry.items),
            "items": [i.to_dict() for i in items],
            "path": str(LiteratureRegistry.path_for(self.config.data_path)),
        }

    def register_literature(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "url jest wymagany"}
        kind = str(args.get("kind") or "").strip().lower() or None
        if kind and kind not in LITERATURE_KINDS:
            kind = classify_literature_kind(url, str(args.get("title") or ""))
        topics = args.get("topics")
        if isinstance(topics, str):
            topics = [t.strip() for t in topics.split(",") if t.strip()]
        registry = LiteratureRegistry.load(self.config.data_path)
        item = registry.upsert(
            url=url,
            title=str(args.get("title") or "").strip() or None,
            kind=kind,
            authors=str(args.get("authors") or "").strip() or None,
            year=str(args.get("year") or "").strip() or None,
            source=str(args.get("source") or "").strip() or None,
            brand=str(args.get("brand") or "").strip() or None,
            topics=list(topics or []) or None,
            access=str(args.get("access") or "unknown"),
            notes=str(args.get("notes") or ""),
            confirmed=bool(args.get("confirmed")),
        )
        path = registry.save(self.config.data_path)
        self.memory.add(
            "literature_register",
            f"{item.kind}: {item.title}",
            meta={"url": item.url, "kind": item.kind},
        )
        return {"ok": True, "item": item.to_dict(), "path": str(path)}

    def sync_notion_literature(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit") or 200)
        save = args.get("save", True)
        rows = self._get_notion().list_literature(limit=limit)
        registry = LiteratureRegistry.load(self.config.data_path)
        saved = 0
        for row in rows:
            url = str(row.get("url") or row.get("notion_url") or "").strip()
            if not url:
                continue
            kind = str(row.get("kind") or "article")
            if kind not in LITERATURE_KINDS:
                kind = "article"
            registry.upsert(
                url=url,
                title=str(row.get("title") or "") or None,
                kind=kind,
                authors=row.get("authors"),
                year=row.get("year"),
                source=str(row.get("source") or "notion:pozycje"),
                topics=list(row.get("topics") or []),
                access="unknown",
                notes=str(row.get("notes") or "")[:1000],
                confirmed=True,
            )
            saved += 1
        path = None
        if save and saved:
            path = str(registry.save(self.config.data_path))
        self.memory.add(
            "literature_notion_sync",
            f"synced {saved}/{len(rows)} from Notion Pozycje",
            meta={"saved": saved, "fetched": len(rows)},
        )
        return {
            "ok": True,
            "fetched": len(rows),
            "saved": saved,
            "path": path,
            "sample": rows[:5],
        }

    def get_domain_context(self, args: dict[str, Any]) -> dict[str, Any]:
        graph = self._get_ontology()
        brief = graph.domain_brief()
        self.memory.add(
            "ontology_brief",
            f"nodes={brief.get('node_count')} edges={brief.get('edge_count')}",
            meta=brief.get("by_type") or {},
        )
        return brief

    def extract_domain_context_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        text = str(args.get("text") or "").strip()
        url = str(args.get("url") or "").strip()
        ingest = args.get("ingest", True)
        if url and not text:
            try:
                doc = self.parser.parse_url(url)
                text = (doc.text or "") if hasattr(doc, "text") else ""
                if not text and url in self.parsed_cache:
                    text = self.parsed_cache[url].text or ""
                if doc and hasattr(doc, "url"):
                    self.parsed_cache[url] = doc
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"parse failed: {exc}", "url": url}
        if not text:
            return {"ok": False, "error": "Podaj text lub url"}
        ctx = extract_domain_context(text)
        result: dict[str, Any] = {**ctx, "url": url or None, "chars": len(text)}
        if ingest:
            graph = self._get_ontology()
            ingested = graph.ingest_context(ctx, source_url=url, origin="extracted")
            path = graph.save(self.config.data_path)
            result["ingested"] = ingested
            result["ontology_path"] = str(path)
        self.memory.add(
            "domain_context",
            str(ctx.get("summary") or ""),
            meta={"url": url, "materials": len(ctx.get("materials") or [])},
        )
        return result

    def list_ontology(self, args: dict[str, Any]) -> dict[str, Any]:
        graph = self._get_ontology()
        nodes = graph.list_nodes(
            entity_type=str(args.get("type") or "").strip() or None,
            q=str(args.get("q") or "").strip() or None,
            limit=int(args.get("limit") or 50),
        )
        return {
            "ok": True,
            "count": len(nodes),
            "total": len(graph.nodes),
            "entity_types": list(ENTITY_TYPES),
            "nodes": [n.to_dict() for n in nodes],
            "path": str(MachiningOntology.path_for(self.config.data_path)),
        }

    def ontology_neighborhood(self, args: dict[str, Any]) -> dict[str, Any]:
        node = str(args.get("node") or "").strip()
        if not node:
            return {"ok": False, "error": "node jest wymagany"}
        depth = int(args.get("depth") or 1)
        return self._get_ontology().neighborhood(node, depth=depth)

    def add_ontology_edge(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self._get_ontology().add_edge(
                str(args.get("source") or ""),
                str(args.get("target") or ""),
                str(args.get("relation") or ""),
                evidence=str(args.get("evidence") or ""),
                confidence=float(args.get("confidence") or 0.8),
                origin="agent",
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        path = self._get_ontology().save(self.config.data_path)
        result["path"] = str(path)
        return result

    def build_ontology(self, args: dict[str, Any]) -> dict[str, Any]:
        graph = self._get_ontology()
        result = graph.build_from_knowledge(self.config.data_path)
        self._ontology = graph
        self.memory.add(
            "ontology_build",
            f"nodes={result.get('node_count')} edges={result.get('edge_count')}",
            meta=result.get("stats") or {},
        )
        return result

    def find_ontology_path(self, args: dict[str, Any]) -> dict[str, Any]:
        source = str(args.get("source") or "").strip()
        target = str(args.get("target") or "").strip()
        if not source or not target:
            return {"ok": False, "error": "source i target są wymagane"}
        result = self._get_ontology().find_path(
            source, target, max_depth=int(args.get("max_depth") or 5)
        )
        if result.get("ok"):
            self.memory.add(
                "ontology_path",
                str(result.get("readable") or ""),
                meta={"length": result.get("length")},
            )
        return result

    def list_known_firms(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip().lower()
        limit = int(args.get("limit") or 50)
        live = bool(args.get("live"))
        include_presentation = args.get("include_presentation", True)
        firms: list[dict[str, Any]] = []
        cache_path = self.config.data_path / "knowledge" / "known_firms.json"
        seed_path = Path("config/known_firms.seed.json")
        source = "cache"

        if live:
            firms = self._get_notion().list_known_firms(
                limit=max(limit, 200),
                enrich_presentations=True,
            )
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(firms, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            source = "notion_live"
        elif cache_path.exists():
            try:
                firms = json.loads(cache_path.read_text(encoding="utf-8"))
                source = "cache"
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"cache read failed: {exc}"}
        elif seed_path.exists():
            firms = json.loads(seed_path.read_text(encoding="utf-8"))
            source = "seed"
        else:
            try:
                firms = self._get_notion().list_known_firms(
                    limit=max(limit, 200),
                    enrich_presentations=True,
                )
                source = "notion_live"
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "error": (
                        f"Brak cache/seed i Notion niedostępne: {exc}. "
                        "Uruchom: python -m market_agents sync-firms"
                    ),
                }

        if query:
            firms = [
                f
                for f in firms
                if query
                in " ".join(
                    str(f.get(k) or "")
                    for k in (
                        "company",
                        "country",
                        "product_focus",
                        "note",
                        "presentation",
                    )
                ).lower()
            ]
        if not include_presentation:
            slim = []
            for f in firms:
                row = dict(f)
                row.pop("presentation", None)
                slim.append(row)
            firms = slim
        firms = firms[:limit]
        with_pres = sum(1 for f in firms if f.get("presentation"))
        self.memory.add(
            "known_firms",
            f"query={query or '*'} → {len(firms)} firm ({source}), "
            f"z przedstawieniem={with_pres}",
            meta={"count": len(firms), "source": source, "with_presentation": with_pres},
        )
        return {
            "ok": True,
            "count": len(firms),
            "with_presentation": with_pres,
            "firms": firms,
            "source": source,
            "cache": str(cache_path),
            "catalog": "notion",
        }

    def get_firm_presentation(self, args: dict[str, Any]) -> dict[str, Any]:
        """Przedstawienie jednej firmy z Notion katalogu."""
        company = str(args.get("company") or "").strip()
        if not company:
            return {"ok": False, "error": "company required"}
        live = bool(args.get("live"))
        listed = self.list_known_firms(
            {"query": company, "limit": 20, "live": live, "include_presentation": True}
        )
        if not listed.get("ok"):
            return listed
        firms = listed.get("firms") or []
        # prefer exact / best match
        from market_agents.firms import normalize_firm_name

        want = normalize_firm_name(company)
        match = None
        for f in firms:
            name = str(f.get("company") or "")
            if normalize_firm_name(name) == want:
                match = f
                break
        if match is None and firms:
            match = firms[0]
        if match is None:
            return {
                "ok": False,
                "error": f"Brak firmy w Notion katalogu: {company}",
                "hint": "Uruchom sync-firms albo sprawdź list_known_firms",
            }
        presentation = str(match.get("presentation") or "").strip()
        notion_url = str(match.get("notion_url") or match.get("id") or "")
        if (live or not presentation) and notion_url:
            try:
                doc = self.fetch_notion_page({"page_id_or_url": notion_url})
                if doc.get("ok") and doc.get("text"):
                    presentation = str(doc.get("text") or "")[:4000]
                    match = {
                        **match,
                        "presentation": presentation,
                        "presentation_source": "notion_page_live",
                    }
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": True,
                    "firm": match,
                    "presentation": presentation,
                    "warning": str(exc),
                }
        self.memory.add(
            "firm_presentation",
            f"{match.get('company')}: {(presentation or match.get('product_focus') or '')[:300]}",
            meta={"company": match.get("company"), "notion_url": notion_url},
        )
        return {
            "ok": True,
            "company": match.get("company"),
            "presentation": presentation,
            "product_focus": match.get("product_focus"),
            "country": match.get("country"),
            "site_url": match.get("site_url"),
            "downloads": match.get("downloads"),
            "notion_url": match.get("notion_url"),
            "presentation_source": match.get("presentation_source"),
            "firm": match,
        }


    def _get_known(self) -> KnownFirmsIndex:
        if self._known is None:
            self._known = KnownFirmsIndex.load(
                data_dir=self.config.data_path,
                competitors=self.config.industry.competitors,
            )
        return self._known

    def _get_relations(self) -> FirmRelationsGraph:
        if self._relations is None:
            self._relations = FirmRelationsGraph.load(data_dir=self.config.data_path)
        return self._relations


    def list_parse_rules(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip() or None
        limit = int(args.get("limit") or 50)
        rows = self._parse_rules.list_rules(query=query, limit=limit)
        return {
            "ok": True,
            "count": len(rows),
            "rules": rows,
            "methods": list(PARSE_METHODS),
            "learn_enabled": bool(self.config.agents.agentic.learn_parse_rules),
        }

    def upsert_parse_rule(self, args: dict[str, Any]) -> dict[str, Any]:
        host_or_url = str(args.get("host_or_url") or "").strip()
        if not host_or_url:
            return {"ok": False, "error": "host_or_url required"}
        try:
            result = self._parse_rules.upsert(
                host_or_url,
                preferred_method=str(args.get("preferred_method") or "auto"),
                css_selector=str(args.get("css_selector") or ""),
                drop_selectors=list(args.get("drop_selectors") or []) or None,
                min_chars=int(args.get("min_chars") or 120),
                notes=str(args.get("notes") or ""),
                origin="agent",
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        path = self._parse_rules.save(self.config.data_path)
        # refresh parser rules reference
        self.parser.rules = self._parse_rules
        self.memory.add(
            "parse_rule",
            f"upsert {result['rule']['host']} method={result['rule']['preferred_method']} "
            f"css={result['rule'].get('css_selector') or '-'}",
            meta=result["rule"],
        )
        return {**result, "path": str(path)}

    def rate_parse(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "url required"}
        quality = float(args.get("quality_0_to_1") or 0)
        result = self._parse_rules.rate(
            url,
            quality_0_to_1=quality,
            parse_method=str(args.get("parse_method") or "") or None,
            css_selector=str(args.get("css_selector") or "") or None,
            notes=str(args.get("notes") or ""),
        )
        path = self._parse_rules.save(self.config.data_path)
        self.parser.rules = self._parse_rules
        self.memory.add(
            "parse_rate",
            f"rate {url} q={quality:.2f}",
            meta=result.get("rule") or {},
        )
        promoted = None
        if (
            self.config.agents.agentic.learn_parsing_skills
            and self.config.agents.agentic.auto_promote_host_skills
            and quality >= 0.75
            and result.get("rule")
        ):
            rule = result["rule"]
            promoted = self._parsing_skills.promote_host_rule(
                url,
                preferred_method=str(rule.get("preferred_method") or "auto"),
                css_selector=str(rule.get("css_selector") or ""),
                notes=f"auto-promote from rate_parse q={quality:.2f}",
                taught_by="parsing_agent",
            )
            self._parsing_skills.save(self.config.data_path)
        out = {**result, "path": str(path)}
        if promoted:
            out["promoted_skill"] = promoted.get("skill")
        return out

    def list_parsing_skills(self, args: dict[str, Any]) -> dict[str, Any]:
        rows = self._parsing_skills.list_skills(
            query=str(args.get("query") or "").strip() or None,
            category=str(args.get("category") or "").strip() or None,
            limit=int(args.get("limit") or 50),
        )
        return {
            "ok": True,
            "count": len(rows),
            "skills": rows,
            "categories": list(SKILL_CATEGORIES),
            "learn_enabled": bool(self.config.agents.agentic.learn_parsing_skills),
        }

    def match_parsing_skills(self, args: dict[str, Any]) -> dict[str, Any]:
        rows = self._parsing_skills.match(
            url=str(args.get("url") or "").strip() or None,
            kind=str(args.get("kind") or "").strip() or None,
            limit=int(args.get("limit") or 5),
        )
        return {"ok": True, "count": len(rows), "skills": rows}

    def learn_parsing_skill(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name required"}
        try:
            result = self._parsing_skills.learn(
                name=name,
                skill_id=str(args.get("skill_id") or "").strip() or None,
                category=str(args.get("category") or "generic"),
                description=str(args.get("description") or ""),
                preferred_method=str(args.get("preferred_method") or "auto"),
                css_selectors=list(args.get("css_selectors") or []) or None,
                drop_selectors=list(args.get("drop_selectors") or []) or None,
                link_keywords=list(args.get("link_keywords") or []) or None,
                extract_patterns=list(args.get("extract_patterns") or []) or None,
                hints=list(args.get("hints") or []) or None,
                applies_to_hosts=list(args.get("applies_to_hosts") or []) or None,
                applies_to_kinds=list(args.get("applies_to_kinds") or []) or None,
                url_contains=list(args.get("url_contains") or []) or None,
                taught_by=str(args.get("taught_by") or "parsing_agent"),
                notes=str(args.get("notes") or ""),
                examples=list(args.get("examples") or []) or None,
                merge=bool(args.get("merge", True)),
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        path = self._parsing_skills.save(self.config.data_path)
        self.memory.add(
            "parsing_skill",
            f"learn {result['skill']['id']} v{result['skill']['version']} "
            f"({result.get('action')})",
            meta=result.get("skill") or {},
        )
        return {**result, "path": str(path)}

    def improve_parsing_skill(self, args: dict[str, Any]) -> dict[str, Any]:
        skill_id = str(args.get("skill_id") or "").strip()
        if not skill_id:
            return {"ok": False, "error": "skill_id required"}
        result = self._parsing_skills.improve(
            skill_id,
            preferred_method=str(args.get("preferred_method") or "") or None,
            css_selectors=list(args.get("css_selectors") or []) or None,
            drop_selectors=list(args.get("drop_selectors") or []) or None,
            link_keywords=list(args.get("link_keywords") or []) or None,
            extract_patterns=list(args.get("extract_patterns") or []) or None,
            hints=list(args.get("hints") or []) or None,
            applies_to_hosts=list(args.get("applies_to_hosts") or []) or None,
            applies_to_kinds=list(args.get("applies_to_kinds") or []) or None,
            url_contains=list(args.get("url_contains") or []) or None,
            notes=str(args.get("notes") or ""),
            taught_by=str(args.get("taught_by") or "parsing_agent"),
        )
        if not result.get("ok"):
            return result
        path = self._parsing_skills.save(self.config.data_path)
        self.memory.add(
            "parsing_skill",
            f"improve {skill_id} → v{result['skill']['version']}",
            meta=result.get("skill") or {},
        )
        return {**result, "path": str(path)}

    def rate_parsing_skill(self, args: dict[str, Any]) -> dict[str, Any]:
        skill_id = str(args.get("skill_id") or "").strip()
        if not skill_id:
            return {"ok": False, "error": "skill_id required"}
        result = self._parsing_skills.rate(
            skill_id,
            quality_0_to_1=float(args.get("quality_0_to_1") or 0),
            notes=str(args.get("notes") or ""),
            taught_by=str(args.get("taught_by") or "parsing_agent"),
        )
        if not result.get("ok"):
            return result
        path = self._parsing_skills.save(self.config.data_path)
        return {**result, "path": str(path)}

    def promote_host_skill(self, args: dict[str, Any]) -> dict[str, Any]:
        host_or_url = str(args.get("host_or_url") or "").strip()
        if not host_or_url:
            return {"ok": False, "error": "host_or_url required"}
        result = self._parsing_skills.promote_host_rule(
            host_or_url,
            preferred_method=str(args.get("preferred_method") or "auto"),
            css_selector=str(args.get("css_selector") or ""),
            category=str(args.get("category") or "generic"),
            notes=str(args.get("notes") or ""),
            taught_by=str(args.get("taught_by") or "parsing_agent"),
        )
        if not result.get("ok"):
            return result
        path = self._parsing_skills.save(self.config.data_path)
        self.memory.add(
            "parsing_skill",
            f"promote host → {result['skill']['id']}",
            meta=result.get("skill") or {},
        )
        return {**result, "path": str(path)}


    def list_relations(self, args: dict[str, Any]) -> dict[str, Any]:
        graph = self._get_relations()
        company = str(args.get("company") or "").strip() or None
        relation_type = str(args.get("relation_type") or "").strip() or None
        limit = int(args.get("limit") or 50)
        rows = graph.list_relations(
            company=company, relation_type=relation_type, limit=limit
        )
        return {
            "ok": True,
            "count": len(rows),
            "relations": rows,
            "relation_types": list(RELATION_TYPES),
        }

    def add_relation(self, args: dict[str, Any]) -> dict[str, Any]:
        graph = self._get_relations()
        source = str(args.get("source") or "").strip()
        target = str(args.get("target") or "").strip()
        relation_type = str(args.get("relation_type") or "").strip()
        evidence = str(args.get("evidence") or "")
        url = str(args.get("url") or "")
        confidence = float(args.get("confidence") or 0.7)
        result = graph.add_relation(
            source,
            target,
            relation_type,
            evidence=evidence,
            url=url,
            confidence=confidence,
            origin="agent",
        )
        path = graph.save(self.config.data_path)
        self.memory.add(
            "firm_relation",
            f"{source} --{relation_type}--> {target}",
            meta={"url": url, "evidence": evidence[:200]},
        )
        return {**result, "path": str(path), "total_edges": len(graph.edges)}

    def discover_relations(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit") or 30)
        parse_candidates = args.get("parse_candidates", True)
        scan_notion = bool(args.get("scan_notion_cache"))
        persist = args.get("persist", True)
        known = self._get_known()
        graph = self._get_relations()
        found: list[dict[str, Any]] = []
        seen: set[str] = set()

        texts: list[dict[str, Any]] = list(args.get("texts") or [])
        if parse_candidates:
            for item in self.candidates:
                blob = f"{item.title}. {item.summary}. {(item.content or '')[:1200]}"
                texts.append({"text": blob, "url": item.url})

        if scan_notion:
            notion_cache = self.config.data_path / "knowledge" / "notion_pages.jsonl"
            if notion_cache.exists():
                with notion_cache.open(encoding="utf-8") as fh:
                    for i, line in enumerate(fh):
                        if i >= 80:
                            break
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        texts.append(
                            {
                                "text": " ".join(
                                    str(row.get(k) or "")
                                    for k in ("title", "summary", "content")
                                )[:4000],
                                "url": str(row.get("url") or ""),
                            }
                        )

        for row in texts:
            if isinstance(row, str):
                row = {"text": row, "url": ""}
            if not isinstance(row, dict):
                continue
            cands = extract_relation_candidates(
                str(row.get("text") or ""),
                url=str(row.get("url") or ""),
                known=known,
                max_relations=8,
            )
            for cand in cands:
                key = (
                    f"{cand.get('normalized_source') or cand.get('source')}|"
                    f"{cand.get('relation_type')}|"
                    f"{cand.get('normalized_target') or cand.get('target')}"
                ).lower()
                if key in seen:
                    continue
                seen.add(key)
                found.append(cand)
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break

        added = 0
        if persist and found:
            for cand in found:
                before = len(graph.edges)
                graph._ingest({**cand, "origin": cand.get("origin") or "extracted"})
                if len(graph.edges) > before:
                    added += 1
            graph.save(self.config.data_path)

        self.memory.add(
            "discover_relations",
            f"kandydaci={len(found)} added={added}: "
            + ", ".join(
                f"{r.get('source')}→{r.get('target')}" for r in found[:8]
            ),
            meta={"count": len(found), "added": added},
        )
        return {
            "ok": True,
            "count": len(found),
            "added": added,
            "relations": found[:limit],
            "total_edges": len(graph.edges),
        }

    def firm_neighborhood(self, args: dict[str, Any]) -> dict[str, Any]:
        company = str(args.get("company") or "").strip()
        if not company:
            return {"ok": False, "error": "company required"}
        depth = int(args.get("depth") or 1)
        graph = self._get_relations()
        nb = graph.neighborhood(company, depth=depth)
        return {"ok": True, **nb}

    def list_firm_profiles(self, args: dict[str, Any]) -> dict[str, Any]:
        reg = self._get_profiles()
        rows = reg.list(
            role=str(args.get("role") or "").strip() or None,
            q=str(args.get("q") or "").strip() or None,
            sort=str(args.get("sort") or "overall"),
            limit=int(args.get("limit") or 30),
        )
        return {
            "ok": True,
            "count": len(rows),
            "profiles": [
                {
                    "id": p.id,
                    "company": p.company,
                    "roles": p.roles,
                    "country": p.country,
                    "scores": {
                        k: (p.scores or {}).get(k)
                        for k in (
                            "overall",
                            "completeness",
                            "identity_trust",
                            "distribution_reach",
                            "digital_commerce",
                            "pr_presence",
                        )
                    },
                    "websites": [w.get("url") for w in (p.websites or [])[:3]],
                }
                for p in rows
            ],
        }

    def get_firm_profile(self, args: dict[str, Any]) -> dict[str, Any]:
        company = str(args.get("company") or "").strip()
        if not company:
            return {"ok": False, "error": "company required"}
        reg = self._get_profiles()
        p = reg.get(company)
        if not p:
            return {
                "ok": False,
                "error": f"brak profilu: {company}",
                "hint": "Uruchom build_firm_profiles / sync-profiles",
            }
        return {"ok": True, "profile": p.to_dict()}

    def firm_scoreboard(self, args: dict[str, Any]) -> dict[str, Any]:
        reg = self._get_profiles()
        limit = int(args.get("limit") or 25)
        return {"ok": True, "scoreboard": reg.scoreboard(limit=limit)}

    def score_firm_profile(self, args: dict[str, Any]) -> dict[str, Any]:
        company = str(args.get("company") or "").strip()
        if not company:
            return {"ok": False, "error": "company required"}
        reg = self._get_profiles()
        p = reg.get(company)
        if not p:
            return {"ok": False, "error": f"brak profilu: {company}"}
        p = reg.upsert(p)
        reg.save(self.config.data_path)
        return {"ok": True, "company": p.company, "scores": p.scores}

    def build_firm_profiles(self, args: dict[str, Any]) -> dict[str, Any]:
        reg = self._get_profiles()
        result = reg.build_from_ecosystem(
            self.config.data_path,
            competitors=list(self.config.industry.competitors or []),
            default_role=str(args.get("default_role") or "competitor"),
        )
        self._profiles = reg
        self.memory.add(
            "build_firm_profiles",
            f"profiles={result.get('count')}",
            meta=result,
        )
        return result

    def upsert_firm_profile(self, args: dict[str, Any]) -> dict[str, Any]:
        company = str(args.get("company") or "").strip()
        if not company:
            return {"ok": False, "error": "company required"}
        reg = self._get_profiles()
        p = reg.get(company) or FirmProfile(
            id=firm_id_from_name(company),
            company=company,
            roles=["unknown"],
            origin="manual",
        )
        if args.get("roles"):
            p.roles = [str(r) for r in args["roles"]]
        if args.get("country"):
            p.country = str(args["country"])
        if args.get("product_focus"):
            p.product_focus = str(args["product_focus"])
        if args.get("notes"):
            p.notes = str(args["notes"])[:4000]
        if args.get("website"):
            p.websites = list(p.websites) + [
                {
                    "url": str(args["website"]),
                    "primary": True,
                    "verified": False,
                    "status": "manual",
                    "source": "manual",
                }
            ]
        if args.get("email"):
            p.emails = list(p.emails) + [
                {"value": str(args["email"]), "verified": False, "status": "manual", "source": "manual"}
            ]
        if args.get("phone"):
            p.phones = list(p.phones) + [
                {"value": str(args["phone"]), "verified": False, "status": "manual", "source": "manual"}
            ]
        if args.get("address"):
            p.addresses = list(p.addresses) + [
                {"raw": str(args["address"]), "verified": False, "status": "manual", "source": "manual"}
            ]
        if args.get("financial_note"):
            fin = dict(p.financial or {})
            fin["notes"] = str(args["financial_note"])[:500]
            p.financial = fin
        if "manual" not in p.sources:
            p.sources.append("manual")
        p = reg.upsert(p)
        reg.save(self.config.data_path)
        return {"ok": True, "profile": p.to_dict()}

    def enrich_firm_profile(self, args: dict[str, Any]) -> dict[str, Any]:
        company = str(args.get("company") or "").strip()
        if not company:
            return {"ok": False, "error": "company required"}
        reg = self._get_profiles()
        p = reg.get(company)
        if not p:
            # stub from known firms
            known = self._get_known().match(company)
            if known:
                from market_agents.firm_profiles import profile_from_known_row

                p = profile_from_known_row(known)
                reg.upsert(p)
            else:
                p = FirmProfile(id=firm_id_from_name(company), company=company, roles=["competitor"])
                reg.upsert(p)
        url = str(args.get("url") or "").strip()
        if not url:
            for w in p.websites or []:
                if w.get("url"):
                    url = str(w["url"])
                    break
        if not url:
            return {"ok": False, "error": "brak URL — podaj url lub website w profilu"}
        try:
            resp = self._fetcher.get(url)
            html = resp.text or ""
            final_url = str(resp.url) if resp.url else url
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": safe_error(exc), "url": url}
        extracted = extract_contacts_from_html(str(html), base_url=str(final_url), company=company)
        p = reg.apply_enrichment(p, extracted)
        reg.save(self.config.data_path)
        return {
            "ok": True,
            "company": p.company,
            "url": final_url,
            "extracted": {
                "emails": len(extracted.get("emails") or []),
                "phones": len(extracted.get("phones") or []),
                "addresses": len(extracted.get("addresses") or []),
                "financial_signals": len(extracted.get("financial_signals") or []),
            },
            "scores": p.scores,
            "verification": p.verification,
        }

    def register_social_mention(self, args: dict[str, Any]) -> dict[str, Any]:
        company = str(args.get("company") or "").strip()
        if not company:
            return {"ok": False, "error": "company required"}
        mention = {
            "company": company,
            "platform": str(args.get("platform") or "web"),
            "title": str(args.get("title") or "")[:300],
            "snippet": str(args.get("snippet") or "")[:800],
            "url": str(args.get("url") or ""),
        }
        path = self.config.data_path / "knowledge" / "social_mentions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, Any]] = []
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                rows = raw if isinstance(raw, list) else list(raw.get("mentions") or [])
            except Exception:  # noqa: BLE001
                rows = []
        rows.append(mention)
        from market_agents.firm_profiles import utc_now_iso

        path.write_text(
            json.dumps(
                {"mentions": rows[-500:], "updated_at": utc_now_iso()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        reg = self._get_profiles()
        p = reg.get(company) or FirmProfile(
            id=firm_id_from_name(company), company=company, roles=["competitor"], origin="social"
        )
        mentions = list((p.public_relations or {}).get("mentions") or []) + [mention]
        texts = [str(m.get("snippet") or m.get("title") or "") for m in mentions]
        p.public_relations = {
            "summary": f"{len(mentions)} publicznych wzmianek",
            "themes": list((p.public_relations or {}).get("themes") or []),
            "sentiment": score_sentiment(texts),
            "mentions": mentions[-30:],
        }
        if "social" not in p.sources:
            p.sources.append("social")
        p = reg.upsert(p)
        reg.save(self.config.data_path)
        return {"ok": True, "company": p.company, "sentiment": p.public_relations.get("sentiment"), "scores": p.scores}

    def _get_prospects(self) -> ProspectRegistry:
        return ProspectRegistry(profiles=self._get_profiles(), assumptions=load_vertical_assumptions())

    def list_prospects(self, args: dict[str, Any]) -> dict[str, Any]:
        pr = self._get_prospects()
        rows = pr.list_prospects(q=str(args.get("q") or "").strip() or None, limit=int(args.get("limit") or 30))
        return {
            "ok": True,
            "count": len(rows),
            "prospects": [
                {
                    "company": p.company,
                    "vertical": (p.prospect or {}).get("vertical"),
                    "quality_tier": (p.prospect or {}).get("quality_tier"),
                    "opportunity": ((p.prospect or {}).get("opportunity") or {}).get("overall"),
                    "budget": (p.prospect or {}).get("budget"),
                    "buys_from": [b.get("brand") for b in ((p.prospect or {}).get("buys_from") or [])[:8]],
                    "processes": ((p.prospect or {}).get("product_map") or {}).get("likely_processes"),
                }
                for p in rows
            ],
        }

    def get_prospect_profile(self, args: dict[str, Any]) -> dict[str, Any]:
        company = str(args.get("company") or "").strip()
        if not company:
            return {"ok": False, "error": "company required"}
        p = self._get_profiles().get(company)
        if not p or not (p.prospect or "prospect" in (p.roles or [])):
            return {
                "ok": False,
                "error": f"brak prospect: {company}",
                "hint": "Uruchom analyze_prospect lub ingest_prospect_seeds",
            }
        return {
            "ok": True,
            "company": p.company,
            "roles": p.roles,
            "prospect": p.prospect,
            "financial": p.financial,
            "network": p.network,
            "scores": p.scores,
            "stakeholders": (p.prospect or {}).get("stakeholders"),
        }

    def prospect_scoreboard(self, args: dict[str, Any]) -> dict[str, Any]:
        pr = self._get_prospects()
        return {"ok": True, "scoreboard": pr.scoreboard(limit=int(args.get("limit") or 25))}

    def analyze_prospect(self, args: dict[str, Any]) -> dict[str, Any]:
        company = str(args.get("company") or "").strip()
        if not company:
            return {"ok": False, "error": "company required"}
        url = str(args.get("url") or "").strip()
        text = str(args.get("text") or "")
        persist = bool(args.get("persist", True))
        assumptions = load_vertical_assumptions()
        if text.strip():
            analysis = analyze_prospect_text(
                text, company=company, website=url, assumptions=assumptions
            )
        elif url:
            try:
                resp = self._fetcher.get(url)
                html = resp.text or ""
                final_url = str(resp.url) if resp.url else url
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": safe_error(exc), "url": url}
            analysis = analyze_prospect_html(
                html, company=company, base_url=final_url, assumptions=assumptions
            )
        else:
            return {"ok": False, "error": "podaj url albo text"}
        if persist:
            pr = self._get_prospects()
            profile = pr.upsert_from_analysis(company, analysis, self.config.data_path)
            self._profiles = pr.profiles
            return {
                "ok": True,
                "company": profile.company,
                "prospect": profile.prospect,
                "scores": profile.scores,
                "disclaimer": analysis.get("disclaimer"),
            }
        return {"ok": True, "analysis": analysis}

    def estimate_tooling_budget_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        vertical = str(args.get("vertical") or "unknown").strip()
        revenue = args.get("revenue_eur")
        revenue_f = float(revenue) if revenue is not None else None
        ratio = float(args.get("production_cost_ratio") or 0.65)
        budget = estimate_tooling_budget(
            vertical=vertical,
            revenue_eur=revenue_f,
            production_cost_ratio=ratio,
            assumptions=load_vertical_assumptions(),
        )
        return {"ok": True, "budget": budget}

    def ingest_prospect_seeds(self, args: dict[str, Any]) -> dict[str, Any]:
        pr = self._get_prospects()
        result = pr.ingest_seeds(self.config.data_path)
        self._profiles = pr.profiles
        return result

    def create_crm_task(self, args: dict[str, Any]) -> dict[str, Any]:
        company = str(args.get("company") or "").strip()
        if not company:
            return {"ok": False, "error": "company required"}
        store = CrmTaskStore.load(self.config.data_path)
        task, _created = store.create(
            company=company,
            title=str(args.get("title") or "") or None,
            reason=str(args.get("reason") or ""),
            opportunity_score=(
                float(args["opportunity_score"])
                if args.get("opportunity_score") is not None
                else None
            ),
            priority=args.get("priority"),  # type: ignore[arg-type]
        )
        store.save(self.config.data_path)
        return {"ok": True, "task": task.to_dict(), "created": _created}

    def list_crm_tasks(self, args: dict[str, Any]) -> dict[str, Any]:
        store = CrmTaskStore.load(self.config.data_path)
        rows = store.list(
            status=str(args.get("status") or "open"),
            company=str(args.get("company") or "").strip() or None,
            limit=int(args.get("limit") or 30),
        )
        return {"ok": True, "count": len(rows), "tasks": [t.to_dict() for t in rows]}

    def sync_crm_from_prospects(self, args: dict[str, Any]) -> dict[str, Any]:
        return create_tasks_from_prospect_scoreboard(
            self.config.data_path,
            min_opportunity=float(args.get("min_opportunity") or 60),
            limit=int(args.get("limit") or 20),
        )

    def push_crm_to_notion(self, args: dict[str, Any]) -> dict[str, Any]:
        from market_agents.crm_tasks import push_crm_tasks_to_notion

        return push_crm_tasks_to_notion(
            self.config,
            only_hot=bool(args.get("only_hot", True)),
            limit=int(args.get("limit") or 20),
            dry_run=bool(args.get("dry_run", False)),
        )

    def list_suppliers(self, args: dict[str, Any]) -> dict[str, Any]:
        from market_agents.suppliers import SupplierRegistry, compute_supplier_scorecard

        reg = SupplierRegistry(self._get_profiles())
        rows = reg.list_suppliers(
            q=str(args.get("q") or "").strip() or None,
            limit=int(args.get("limit") or 30),
        )
        return {
            "ok": True,
            "count": len(rows),
            "suppliers": [
                {
                    "company": p.company,
                    "roles": p.roles,
                    "country": p.country,
                    "supplier_overall": compute_supplier_scorecard(p)["overall"],
                    "customers": (p.network or {}).get("customers") or [],
                    "scores": {
                        k: (p.scores or {}).get(k)
                        for k in ("completeness", "identity_trust", "digital_commerce")
                    },
                }
                for p in rows
            ],
        }

    def supplier_scoreboard(self, args: dict[str, Any]) -> dict[str, Any]:
        from market_agents.suppliers import SupplierRegistry

        reg = SupplierRegistry(self._get_profiles())
        return {"ok": True, "scoreboard": reg.scoreboard(limit=int(args.get("limit") or 25))}

    def score_suppliers(self, args: dict[str, Any]) -> dict[str, Any]:
        from market_agents.suppliers import SupplierRegistry

        reg = SupplierRegistry.load(self.config.data_path)
        result = reg.refresh_and_save(self.config.data_path)
        self._profiles = reg.profiles
        return result

    def resolve_firm_duplicates(self, args: dict[str, Any]) -> dict[str, Any]:
        from market_agents.entity_resolution import resolve_golden_records

        result = resolve_golden_records(
            self.config.data_path,
            threshold=float(args.get("threshold") or 0.92),
            dry_run=bool(args.get("dry_run", True)),
        )
        if not result.get("dry_run"):
            self._profiles = None
        return result

    def list_run_metrics(self, args: dict[str, Any]) -> dict[str, Any]:
        from market_agents.metrics import recent_metrics

        rows = recent_metrics(self.config.data_path, limit=int(args.get("limit") or 15))
        return {"ok": True, "count": len(rows), "metrics": rows}

    def refresh_change_digest(self, args: dict[str, Any]) -> dict[str, Any]:
        from market_agents.change_digest import refresh_digest

        return refresh_digest(
            self.config.data_path,
            min_delta=float(args.get("min_delta") or 5),
            limit=int(args.get("limit") or 25),
        )

    def export_knowledge_pack_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        from market_agents.knowledge_export import export_knowledge_pack

        out = str(args.get("out_dir") or "").strip()
        out_path = Path(out) if out else Path(self.config.report_path) / "knowledge_export"
        return export_knowledge_pack(
            self.config.data_path,
            out_path,
            limit=int(args.get("limit") or 30),
        )

    def discover_new_firms(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit") or 25)
        run_search = args.get("run_search", True)
        parse_candidates = args.get("parse_candidates", True)
        extra_texts = args.get("texts") or []
        known = self._get_known()
        found: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _add(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                key = str(row.get("normalized") or row.get("company") or "").lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                found.append(row)

        if run_search and self.config.sources.firm_discovery.enabled:
            collector = FirmDiscoveryCollector(
                self.config.sources.firm_discovery,
                known,
                lookback_hours=max(self.config.agents.lookback_hours, 168),
            )
            for item in collector.collect(max_items=limit):
                firms = (item.analysis or {}).get("new_firms") or []
                if isinstance(firms, list):
                    _add([f for f in firms if isinstance(f, dict)])
                # dopisz też do candidates cyklu
                if item.url not in {c.url for c in self.candidates}:
                    self.candidates.append(item)

        texts: list[dict[str, Any]] = []
        if parse_candidates:
            for item in self.candidates:
                blob = f"{item.title}. {item.summary}. {(item.content or '')[:800]}"
                texts.append({"text": blob, "url": item.url})
        for row in extra_texts:
            if isinstance(row, dict):
                texts.append(row)
            elif isinstance(row, str):
                texts.append({"text": row, "url": ""})

        for row in texts:
            names = extract_candidate_firm_names(str(row.get("text") or ""), max_names=10)
            _add(
                filter_new_firms(
                    names,
                    known,
                    evidence=str(row.get("text") or "")[:400],
                    url=str(row.get("url") or ""),
                )
            )

        found = found[:limit]
        self.memory.add(
            "new_firms",
            f"odkryto {len(found)} kandydatów: "
            + ", ".join(str(f.get("company")) for f in found[:12]),
            meta={"count": len(found)},
        )
        return {"ok": True, "count": len(found), "new_firms": found}

    def check_firm_known(self, args: dict[str, Any]) -> dict[str, Any]:
        known = self._get_known()
        names: list[str] = []
        if args.get("name"):
            names.append(str(args["name"]))
        for n in args.get("names") or []:
            if isinstance(n, str) and n.strip():
                names.append(n.strip())
        if not names:
            return {"ok": False, "error": "podaj name lub names[]"}
        rows = []
        for name in names:
            match = known.match(name)
            rows.append(
                {
                    "name": name,
                    "known": known.is_known(name),
                    "match": match,
                }
            )
        return {"ok": True, "results": rows}
