from __future__ import annotations

import re
from difflib import SequenceMatcher

from market_agents.collectors import CatalogCollector, FirmDiscoveryCollector, RssCollector, WebCollector
from market_agents.collectors.notion import NotionCollector
from market_agents.collectors.r2 import build_r2_collector
from market_agents.config import AppConfig
from market_agents.firms import KnownFirmsIndex
from market_agents.models import MarketItem
from market_agents.storage import Storage


class CollectorAgent:
    """Zbiera sygnały: RSS/WWW + katalogi + discovery nowych firm + Notion + R2."""

    def __init__(self, config: AppConfig, storage: Storage) -> None:
        self.config = config
        self.storage = storage

    def run(self) -> list[MarketItem]:
        known = KnownFirmsIndex.load(
            data_dir=self.config.data_path,
            competitors=self.config.industry.competitors,
        )
        collectors: list[object] = []
        for src in self.config.sources.rss:
            collectors.append(
                RssCollector(src, lookback_hours=self.config.agents.lookback_hours)
            )
        for src in self.config.sources.web:
            collectors.append(WebCollector(src))
        for src in self.config.sources.catalogs:
            collectors.append(CatalogCollector(src))
        if self.config.sources.firm_discovery.enabled:
            collectors.append(
                FirmDiscoveryCollector(
                    self.config.sources.firm_discovery,
                    known,
                    lookback_hours=max(self.config.agents.lookback_hours, 168),
                )
            )

        raw: list[MarketItem] = []
        for collector in collectors:
            try:
                limit = self.config.agents.max_items_per_source
                if getattr(collector, "name", "") == "firm_discovery":
                    limit = self.config.sources.firm_discovery.max_items
                raw.extend(collector.collect(limit))  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                print(f"[collector:{getattr(collector, 'name', '?')}] pominięto: {exc}")

        # Notion — Twoja istniejąca baza
        if self.config.sources.notion.enabled:
            token = self.config.notion_token()
            if not token:
                print(
                    "[notion] brak tokenu — ustaw NOTION_TOKEN "
                    f"(env: {self.config.sources.notion.token_env})"
                )
            else:
                try:
                    notion = NotionCollector(self.config.sources.notion, token)
                    raw.extend(notion.collect(self.config.sources.notion.max_pages))
                except Exception as exc:  # noqa: BLE001
                    print(f"[notion] pominięto: {exc}")

        # Cloudflare R2 — duże archiwum
        if self.config.sources.cloudflare_r2.enabled:
            try:
                r2 = build_r2_collector(
                    self.config.sources.cloudflare_r2,
                    self.config.r2_credentials(),
                )
                raw.extend(r2.collect(self.config.sources.cloudflare_r2.max_objects))
            except Exception as exc:  # noqa: BLE001
                print(f"[r2] pominięto: {exc}")

        seen = self.storage.load_seen()
        unique: list[MarketItem] = []
        titles_norm: list[str] = []
        for item in raw:
            if item.url in seen:
                continue
            score = self._relevance(item)
            # Notion/R2/katalogi/discovery — wysoka wartość nawet bez keyword match
            if item.source in {"notion", "cloudflare_r2", "firm_discovery"} or str(
                item.source
            ).startswith("catalog:"):
                score = max(score, float(item.relevance_score or 0.45))
            if score < self.config.agents.min_relevance_score and item.source not in {
                "notion",
                "cloudflare_r2",
                "firm_discovery",
            } and not str(item.source).startswith("catalog:"):
                continue
            title_key = self._normalize_title(item.title)
            if any(SequenceMatcher(None, title_key, prev).ratio() >= 0.9 for prev in titles_norm):
                continue
            item.relevance_score = score
            item.tags = list({*item.tags, *self._match_keywords(item)})
            unique.append(item)
            titles_norm.append(title_key)

        unique.sort(key=lambda x: x.relevance_score, reverse=True)
        return unique

    @staticmethod
    def _normalize_title(title: str) -> str:
        cleaned = re.sub(r"\s+", " ", title.lower())
        cleaned = re.sub(r"\[.*?\]|\(.*?\)", "", cleaned)
        return cleaned.strip()

    def _match_keywords(self, item: MarketItem) -> list[str]:
        text = f"{item.title} {item.summary}".lower()
        return [kw for kw in self.config.industry.keywords if kw.lower() in text]

    def _relevance(self, item: MarketItem) -> float:
        text = f"{item.title} {item.summary} {item.content[:500]}".lower()
        for bad in self.config.industry.exclude_keywords:
            if bad.lower() in text:
                return 0.0
        keywords = [kw.lower() for kw in self.config.industry.keywords]
        if not keywords:
            return 0.5
        hits = sum(1 for kw in keywords if kw in text)
        fuzzy = 0.0
        tokens = re.findall(r"[\wąćęłńóśźż-]{4,}", text)
        for kw in keywords:
            for token in tokens[:40]:
                if SequenceMatcher(None, kw, token).ratio() >= 0.86:
                    fuzzy += 0.15
                    break
        score = min(1.0, (hits / max(len(keywords), 1)) * 0.85 + fuzzy)
        if hits == 0 and fuzzy == 0:
            return 0.2
        return max(score, 0.35 if hits else score)
