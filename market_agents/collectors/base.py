from __future__ import annotations

import io
from abc import ABC, abstractmethod
from typing import Any

import feedparser

from market_agents.models import MarketItem
from market_agents.polite_http import AdaptivePoliteFetcher

_FEED_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
    "text/xml;q=0.9, */*;q=0.8"
)


class BaseCollector(ABC):
    name: str = "base"

    @abstractmethod
    def collect(self, max_items: int = 15) -> list[MarketItem]:
        raise NotImplementedError


def fetch_feed(url: str, fetcher: AdaptivePoliteFetcher | None = None) -> Any:
    """Pobierz feed przez SSRF-guarded fetcher; feedparser dostaje tylko bajty."""
    fetcher = fetcher or AdaptivePoliteFetcher.shared()
    response = fetcher.fetch_once(url, headers={"Accept": _FEED_ACCEPT})
    response.raise_for_status()
    return feedparser.parse(
        io.BytesIO(response.content),
        response_headers={
            "content-type": response.headers.get("content-type", ""),
            "content-location": str(response.url),
        },
    )
