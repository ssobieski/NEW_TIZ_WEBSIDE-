from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from market_agents.collectors.catalog import CatalogCollector
from market_agents.collectors.notion import NotionCollector
from market_agents.collectors.r2 import build_r2_collector
from market_agents.config import AppConfig, CatalogSource
from market_agents.memory import MarketMemory
from market_agents.models import MarketItem
from market_agents.parsing import IntelligentParser, ParsedDocument


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
        self.parser = IntelligentParser()
        self.candidates = candidates or []
        self.parsed_cache: dict[str, ParsedDocument] = {}
        self.structured: list[dict[str, Any]] = []
        self._notion: NotionCollector | None = None
        self._r2 = None
        self._handlers: dict[str, ToolFn] = {
            "list_candidates": self.list_candidates,
            "fetch_and_parse": self.fetch_and_parse,
            "batch_parse": self.batch_parse,
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
            "list_known_firms": self.list_known_firms,
        }

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
                        "Pobierz URL i inteligentnie wyodrębnij pełną treść artykułu "
                        "(trafilatura + fallback). Używaj do głębokiego parsowania."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "batch_parse",
                    "description": "Równolegle sparsuj wiele URL-i (głęboki research).",
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
                                    "regulation",
                                    "trend",
                                    "pricing",
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
                                "description": "Opcjonalny filtr: ecatalog|pdf|eshop|publication|digital_catalogue",
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
                    "name": "list_known_firms",
                    "description": (
                        "Lista znanych firm z Notion (Katalogi konkurencji — indeks): "
                        "nazwa, kraj, Site URL, Downloads, fokus produktowy."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Opcjonalny filtr po nazwie firmy / kraju / fokusie",
                            },
                            "limit": {"type": "integer", "default": 50},
                            "live": {
                                "type": "boolean",
                                "default": False,
                                "description": "True = odśwież z Notion API; False = lokalny cache",
                            },
                        },
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
        try:
            return handler(arguments or {})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "tool": name}

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
            }
            for item in self.candidates[:limit]
        ]
        return {"ok": True, "count": len(rows), "candidates": rows}

    def fetch_and_parse(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "url required"}
        if url in self.parsed_cache:
            doc = self.parsed_cache[url]
        else:
            doc = self.parser.parse_url(url)
            self.parsed_cache[url] = doc
            for item in self.candidates:
                if item.url == url and doc.ok:
                    item.content = doc.text
                    if doc.title:
                        item.title = doc.title
                    if doc.published_at:
                        item.published_at = doc.published_at
        return {
            "ok": doc.ok,
            "url": url,
            "title": doc.title,
            "parse_method": doc.parse_method,
            "chars": len(doc.text),
            "excerpt": doc.excerpt,
            "links": doc.links[:10],
            "error": doc.error,
        }

    def batch_parse(self, args: dict[str, Any]) -> dict[str, Any]:
        urls = [u for u in (args.get("urls") or []) if isinstance(u, str)]
        urls = urls[: self.config.agents.agentic.max_deep_parses]
        workers = min(self.config.agents.agentic.parallel_fetches, max(len(urls), 1))
        results: list[dict[str, Any]] = []

        def _one(u: str) -> dict[str, Any]:
            return self.fetch_and_parse({"url": u})

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_one, u): u for u in urls}
            for fut in as_completed(futs):
                results.append(fut.result())
        return {"ok": True, "results": results}

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
        key = str(args.get("key") or "")
        max_chars = int(args.get("max_chars") or 12000)
        text = self._get_r2().get_text(key)
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
        collector = self._resolve_catalog(args)
        assets = collector.discover_assets(max_links=max_links)
        self.memory.add(
            "catalog_discover",
            f"{collector.name}: {len(assets)} assets",
            meta={"source": collector.name, "count": len(assets)},
        )
        return {"ok": True, "source": collector.name, "count": len(assets), "assets": assets}

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

    def list_known_firms(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip().lower()
        limit = int(args.get("limit") or 50)
        live = bool(args.get("live"))
        firms: list[dict[str, Any]] = []
        cache_path = self.config.data_path / "knowledge" / "known_firms.json"
        seed_path = Path("config/known_firms.seed.json")
        source = "cache"

        if live:
            firms = self._get_notion().list_known_firms(limit=max(limit, 200))
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
                firms = self._get_notion().list_known_firms(limit=max(limit, 200))
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
                    for k in ("company", "country", "product_focus", "note")
                ).lower()
            ]
        firms = firms[:limit]
        self.memory.add(
            "known_firms",
            f"query={query or '*'} → {len(firms)} firm ({source})",
            meta={"count": len(firms), "source": source},
        )
        return {
            "ok": True,
            "count": len(firms),
            "firms": firms,
            "source": source,
            "cache": str(cache_path),
        }
