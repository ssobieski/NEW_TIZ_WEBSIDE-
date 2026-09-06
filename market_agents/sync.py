from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from market_agents.collectors.notion import NotionCollector
from market_agents.collectors.r2 import build_r2_collector
from market_agents.config import AppConfig
from market_agents.memory import MarketMemory
from market_agents.storage import Storage


@dataclass
class SyncResult:
    source: str
    items: int
    path: Path
    details: dict[str, Any]


class KnowledgeSync:
    """Synchronizuje istniejące dane z Notion i Cloudflare R2 do lokalnego cache."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.storage = Storage(config.data_path, config.report_path)
        self.memory = MarketMemory(config.data_path)
        self.cache_dir = config.data_path / "knowledge"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def sync_notion(self, enrich_text: bool = True) -> SyncResult:
        token = self.config.notion_token()
        if not token:
            raise RuntimeError(
                f"Ustaw {self.config.sources.notion.token_env}=secret_... "
                "(Notion Internal Integration Token)"
            )
        collector = NotionCollector(self.config.sources.notion, token)
        items = collector.collect(self.config.sources.notion.max_pages)

        if enrich_text:
            for item in items:
                notion_id = (item.analysis or {}).get("notion_id")
                if not notion_id:
                    continue
                try:
                    doc = collector.fetch_page_text(str(notion_id))
                    if doc.get("ok"):
                        item.content = str(doc.get("text") or "")[:80000]
                        item.summary = str(doc.get("excerpt") or item.summary)[:800]
                except Exception as exc:  # noqa: BLE001
                    print(f"[sync-notion] enrich {item.title}: {exc}")

        out = self.cache_dir / "notion_pages.jsonl"
        with out.open("w", encoding="utf-8") as fh:
            for item in items:
                fh.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
                self.memory.add(
                    "notion_sync",
                    f"{item.title}: {item.summary[:300]}",
                    meta={"url": item.url, "source": "notion"},
                )

        self.storage.append_items(items)
        return SyncResult(
            source="notion",
            items=len(items),
            path=out,
            details={"roots": self.config.sources.notion.root_pages},
        )

    def sync_r2(self) -> SyncResult:
        if not self.config.sources.cloudflare_r2.enabled:
            raise RuntimeError("cloudflare_r2.enabled=false w configu")
        r2 = build_r2_collector(
            self.config.sources.cloudflare_r2,
            self.config.r2_credentials(),
        )
        items = r2.collect()
        out = self.cache_dir / "r2_objects.jsonl"
        with out.open("w", encoding="utf-8") as fh:
            for item in items:
                fh.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
                self.memory.add(
                    "r2_sync",
                    f"{item.title}: {item.summary[:300]}",
                    meta={"url": item.url, "source": "cloudflare_r2"},
                )
        index = {
            "bucket": self.config.sources.cloudflare_r2.bucket,
            "prefix": self.config.sources.cloudflare_r2.prefix,
            "count": len(items),
            "keys": [(i.analysis or {}).get("r2_key") for i in items],
        }
        (self.cache_dir / "r2_index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.storage.append_items(items)
        return SyncResult(
            source="cloudflare_r2",
            items=len(items),
            path=out,
            details=index,
        )

    def sync_all(self) -> list[SyncResult]:
        results: list[SyncResult] = []
        if self.config.sources.notion.enabled:
            results.append(self.sync_notion())
        if self.config.sources.cloudflare_r2.enabled:
            results.append(self.sync_r2())
        return results
