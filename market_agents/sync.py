from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from market_agents.collectors.notion import NotionCollector
from market_agents.collectors.r2 import build_r2_collector
from market_agents.config import AppConfig
from market_agents.firm_relations import FirmRelationsGraph, extract_relation_candidates
from market_agents.firms import KnownFirmsIndex
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
            try:
                results.append(self.sync_known_firms())
            except Exception as exc:  # noqa: BLE001
                print(f"[sync-firms] pominięto: {exc}")
            try:
                results.append(self.sync_relations())
            except Exception as exc:  # noqa: BLE001
                print(f"[sync-relations] pominięto: {exc}")
        if self.config.sources.cloudflare_r2.enabled:
            results.append(self.sync_r2())
        return results

    def sync_known_firms(self) -> SyncResult:
        """Pobierz listę znanych firm z Notion indeksu → lokalny cache + podpowiedzi katalogów."""
        token = self.config.notion_token()
        if not token:
            raise RuntimeError(
                f"Ustaw {self.config.sources.notion.token_env}=secret_... "
                "(Notion Internal Integration Token)"
            )
        collector = NotionCollector(self.config.sources.notion, token)
        firms = collector.list_known_firms()
        out = self.cache_dir / "known_firms.json"
        out.write_text(json.dumps(firms, ensure_ascii=False, indent=2), encoding="utf-8")

        # podpowiedzi URL katalogów (Site URL + Downloads)
        catalog_hints: list[dict[str, Any]] = []
        for firm in firms:
            company = firm.get("company") or ""
            site = firm.get("site_url") or ""
            downloads = firm.get("downloads") or ""
            if site:
                catalog_hints.append(
                    {
                        "name": f"{company} site",
                        "brand": company,
                        "kind": "digital_catalogue",
                        "url": site,
                    }
                )
            if downloads and downloads.rstrip("/") != site.rstrip("/"):
                catalog_hints.append(
                    {
                        "name": f"{company} downloads",
                        "brand": company,
                        "kind": "ecatalog",
                        "url": downloads,
                    }
                )
        hints_path = self.cache_dir / "known_firms_catalog_hints.json"
        hints_path.write_text(
            json.dumps(catalog_hints, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        names = [str(f.get("company")) for f in firms if f.get("company")]
        self.memory.add(
            "known_firms",
            f"Zsynchronizowano {len(names)} firm: {', '.join(names[:20])}"
            + ("…" if len(names) > 20 else ""),
            meta={"count": len(names), "path": str(out)},
        )
        return SyncResult(
            source="known_firms",
            items=len(firms),
            path=out,
            details={
                "companies": names,
                "catalog_hints": len(catalog_hints),
                "hints_path": str(hints_path),
            },
        )

    def sync_relations(self, scan_notion_cache: bool = True) -> SyncResult:
        """
        Zbuduj / odśwież siatkę powiązań firm (distributor / brand / OEM group).
        Merge: seed + istniejący cache + heurystyki z lokalnego cache Notion.
        """
        seed_path = Path("config/firm_relations.seed.json")
        graph = FirmRelationsGraph.load(
            data_dir=self.config.data_path,
            seed_path=seed_path,
        )
        # zawsze dołącz seed (nawet jeśli cache już istnieje)
        if seed_path.exists():
            try:
                payload = json.loads(seed_path.read_text(encoding="utf-8"))
                seed_edges = payload.get("edges", [])
                if isinstance(seed_edges, list):
                    graph.merge(seed_edges)
            except Exception as exc:  # noqa: BLE001
                print(f"[sync-relations] seed: {exc}")

        extracted = 0
        if scan_notion_cache:
            known = KnownFirmsIndex.load(
                data_dir=self.config.data_path,
                competitors=self.config.industry.competitors,
            )
            notion_cache = self.cache_dir / "notion_pages.jsonl"
            if notion_cache.exists():
                with notion_cache.open(encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        blob = " ".join(
                            str(row.get(k) or "")
                            for k in ("title", "summary", "content")
                        )
                        url = str(row.get("url") or "")
                        cands = extract_relation_candidates(
                            blob, url=url, known=known, max_relations=10
                        )
                        for cand in cands:
                            cand["origin"] = "notion_extract"
                            before = len(graph.edges)
                            graph._ingest(cand)
                            if len(graph.edges) > before:
                                extracted += 1

        out = graph.save(self.config.data_path)
        by_type: dict[str, int] = {}
        for e in graph.edges:
            rt = str(e.get("relation_type") or "")
            by_type[rt] = by_type.get(rt, 0) + 1

        self.memory.add(
            "firm_relations",
            f"Siatka powiązań: {len(graph.edges)} krawędzi "
            f"(+{extracted} z Notion cache). Typy: {by_type}",
            meta={"count": len(graph.edges), "extracted": extracted, "by_type": by_type},
        )
        return SyncResult(
            source="firm_relations",
            items=len(graph.edges),
            path=out,
            details={"by_type": by_type, "extracted_from_notion": extracted},
        )
