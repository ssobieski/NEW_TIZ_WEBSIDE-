from __future__ import annotations

import re
from difflib import SequenceMatcher

from market_agents.collectors import RssCollector, WebCollector
from market_agents.config import AppConfig
from market_agents.models import MarketItem
from market_agents.storage import Storage


class CollectorAgent:
    """Zbiera sygnały rynkowe z RSS/WWW i filtruje po słowach kluczowych."""

    def __init__(self, config: AppConfig, storage: Storage) -> None:
        self.config = config
        self.storage = storage

    def run(self) -> list[MarketItem]:
        collectors = []
        for src in self.config.sources.rss:
            collectors.append(
                RssCollector(src, lookback_hours=self.config.agents.lookback_hours)
            )
        for src in self.config.sources.web:
            collectors.append(WebCollector(src))

        raw: list[MarketItem] = []
        for collector in collectors:
            try:
                raw.extend(collector.collect(self.config.agents.max_items_per_source))
            except Exception as exc:  # noqa: BLE001
                print(f"[collector:{collector.name}] pominięto źródło: {exc}")

        seen = self.storage.load_seen()
        unique: list[MarketItem] = []
        titles_norm: list[str] = []
        for item in raw:
            if item.url in seen:
                continue
            score = self._relevance(item)
            if score < self.config.agents.min_relevance_score:
                continue
            title_key = self._normalize_title(item.title)
            if any(SequenceMatcher(None, title_key, prev).ratio() >= 0.9 for prev in titles_norm):
                continue
            item.relevance_score = score
            item.tags = self._match_keywords(item)
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
        text = f"{item.title} {item.summary}".lower()
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
