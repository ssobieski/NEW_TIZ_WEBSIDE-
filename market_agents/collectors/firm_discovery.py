from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

import feedparser

from market_agents.collectors.base import BaseCollector
from market_agents.config import FirmDiscoveryConfig
from market_agents.firms import (
    KnownFirmsIndex,
    extract_candidate_firm_names,
    filter_new_firms,
    is_tooling_relevant,
)
from market_agents.models import MarketItem


class FirmDiscoveryCollector(BaseCollector):
    """
    Parsing wyszukiwania NOWYCH firm (spoza known_firms).
    Google News RSS + własne feedy → ekstrakcja nazw → filtr znanych + domeny tooling.
    """

    name = "firm_discovery"

    def __init__(
        self,
        config: FirmDiscoveryConfig,
        known: KnownFirmsIndex,
        lookback_hours: int = 168,
    ) -> None:
        self.config = config
        self.known = known
        self.lookback_hours = lookback_hours

    def collect(self, max_items: int = 20) -> list[MarketItem]:
        if not self.config.enabled:
            return []

        feed_urls = list(self.config.feed_urls)
        for query in self.config.search_queries:
            feed_urls.append(
                _google_news_rss(query, self.config.language, self.config.region)
            )

        items: list[MarketItem] = []
        seen_urls: set[str] = set()
        seen_firms: set[str] = set()

        for feed_url in feed_urls:
            try:
                entries = feedparser.parse(feed_url).entries
            except Exception as exc:  # noqa: BLE001
                print(f"[firm_discovery] feed error: {exc}")
                continue

            for entry in entries[: max_items * 3]:
                title = (getattr(entry, "title", "") or "").strip()
                link = (getattr(entry, "link", "") or "").strip()
                if not title or not link or link in seen_urls:
                    continue

                summary = (
                    getattr(entry, "summary", None)
                    or getattr(entry, "description", None)
                    or ""
                )
                summary = _strip_html(str(summary))
                blob = f"{title}. {summary}"

                # Odrzuć off-topic (zegarki, F1, space, AI hype…) — wymagaj cue tooling
                if not is_tooling_relevant(blob):
                    continue

                names = extract_candidate_firm_names(blob, max_names=8)
                new_firms = filter_new_firms(
                    names,
                    self.known,
                    evidence=blob,
                    url=link,
                    require_domain=True,
                )
                # Bez wiarygodnej nazwy — nie używaj całego tytułu jako „firmy”
                if not new_firms:
                    continue

                fresh: list[dict[str, Any]] = []
                for firm in new_firms:
                    key = str(firm.get("normalized") or firm.get("company") or "").lower()
                    if not key or key in seen_firms:
                        continue
                    seen_firms.add(key)
                    fresh.append(firm)
                if not fresh:
                    continue

                seen_urls.add(link)
                firm_names = ", ".join(str(f.get("company")) for f in fresh[:4])
                items.append(
                    MarketItem(
                        title=f"Nowa firma? {firm_names}",
                        url=link,
                        source="firm_discovery",
                        summary=summary[:500] or title,
                        content=blob[:3000],
                        tags=["new_firm", "firm_discovery"],
                        relevance_score=0.72,
                        analysis={
                            "discovery": True,
                            "new_firms": fresh,
                            "headline": title,
                        },
                    )
                )
                if len(items) >= max_items:
                    return items
        return items

    def discover_from_texts(
        self, texts: list[dict[str, Any]], max_firms: int = 40
    ) -> list[dict[str, Any]]:
        """API dla tooli — parsuj listę {text,url} i zwróć nowe firmy."""
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in texts:
            text = str(row.get("text") or row.get("title") or "")
            url = str(row.get("url") or "")
            if text and not is_tooling_relevant(text):
                continue
            names = extract_candidate_firm_names(text, max_names=10)
            for firm in filter_new_firms(
                names, self.known, evidence=text, url=url, require_domain=True
            ):
                key = str(firm.get("normalized") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                found.append(firm)
                if len(found) >= max_firms:
                    return found
        return found


def _google_news_rss(query: str, lang: str = "en", region: str = "US") -> str:
    q = quote_plus(query)
    return (
        f"https://news.google.com/rss/search?q={q}"
        f"&hl={lang}&gl={region}&ceid={region}:{lang}"
    )


def _strip_html(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", cleaned).strip()
