from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser

from market_agents.collectors.base import BaseCollector
from market_agents.config import RssSource
from market_agents.models import MarketItem


class RssCollector(BaseCollector):
    def __init__(self, source: RssSource, lookback_hours: int = 48) -> None:
        self.source = source
        self.name = source.name
        self.lookback_hours = lookback_hours

    def collect(self, max_items: int = 15) -> list[MarketItem]:
        feed = feedparser.parse(self.source.url)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)
        items: list[MarketItem] = []
        for entry in feed.entries[: max_items * 2]:
            published = self._parse_date(entry)
            if published and published < cutoff:
                continue
            title = (getattr(entry, "title", "") or "").strip()
            link = (getattr(entry, "link", "") or "").strip()
            if not title or not link:
                continue
            summary = getattr(entry, "summary", None) or getattr(entry, "description", None) or ""
            items.append(
                MarketItem(
                    title=title,
                    url=link,
                    source=self.source.name,
                    published_at=published.isoformat() if published else None,
                    summary=_strip_html(str(summary)),
                )
            )
            if len(items) >= max_items:
                break
        return items

    @staticmethod
    def _parse_date(entry: object) -> datetime | None:
        for attr in ("published", "updated"):
            value = getattr(entry, attr, None)
            if not value:
                continue
            try:
                dt = parsedate_to_datetime(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except (TypeError, ValueError, IndexError):
                continue
        return None


def _strip_html(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", cleaned).strip()
