from __future__ import annotations

import io
from abc import ABC, abstractmethod
from typing import Any

import feedparser

from market_agents.models import MarketItem
from market_agents.polite_http import AdaptivePoliteFetcher

FEED_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
    "text/xml;q=0.9, */*;q=0.5"
)


class BaseCollector(ABC):
    name: str = "base"

    @abstractmethod
    def collect(self, max_items: int = 15) -> list[MarketItem]:
        raise NotImplementedError


def fetch_feed(fetcher: AdaptivePoliteFetcher, url: str, **kwargs: Any) -> Any:
    """Pobierz feed przez guarded fetcher i sparsuj bajty (feedparser nie robi własnych requestów)."""
    response = fetcher.get(url, headers={"Accept": FEED_ACCEPT}, **kwargs)
    response_headers = {"content-location": str(response.url)}
    content_type = response.headers.get("content-type")
    if content_type:
        response_headers["content-type"] = content_type
    # Stream, not bytes: feedparser would treat a bytes body as a local file path first.
    return feedparser.parse(io.BytesIO(response.content), response_headers=response_headers)
