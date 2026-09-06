from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from market_agents.config import AppConfig
from market_agents.memory import MarketMemory
from market_agents.models import MarketItem
from market_agents.parsing import IntelligentParser, ParsedDocument


ToolFn = Callable[[dict[str, Any]], dict[str, Any]]


class ToolRegistry:
    """Narzędzia dla agenta ReAct — inteligentny parsing i wywiad rynkowy."""

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
        self._handlers: dict[str, ToolFn] = {
            "list_candidates": self.list_candidates,
            "fetch_and_parse": self.fetch_and_parse,
            "batch_parse": self.batch_parse,
            "extract_market_intel": self.extract_market_intel,
            "search_memory": self.search_memory,
            "remember": self.remember,
            "score_item": self.score_item,
        }

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
