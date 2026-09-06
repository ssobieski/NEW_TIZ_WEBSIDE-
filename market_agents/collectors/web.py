from __future__ import annotations

import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from market_agents.collectors.base import BaseCollector
from market_agents.config import WebSource
from market_agents.models import MarketItem


class WebCollector(BaseCollector):
    """Prosty scraper publicznych stron — bez JS/paywall."""

    def __init__(self, source: WebSource) -> None:
        self.source = source
        self.name = source.name

    def collect(self, max_items: int = 15) -> list[MarketItem]:
        headers = {
            "User-Agent": "MarketAgentsLocal/0.2 (+local research bot; polite crawl)"
        }
        with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
            response = client.get(self.source.url)
            response.raise_for_status()
            html = response.text

        soup = BeautifulSoup(html, "lxml")
        nodes = (
            soup.select(self.source.css_selector)
            if self.source.css_selector
            else soup.find_all(["article", "h2", "h3"], limit=max_items * 3)
        )

        items: list[MarketItem] = []
        seen: set[str] = set()
        for node in nodes:
            link_tag = node.find("a") if hasattr(node, "find") else None
            if link_tag is None and getattr(node, "name", None) == "a":
                link_tag = node
            href = (link_tag.get("href") if link_tag else None) or self.source.url
            title = (
                link_tag.get_text(" ", strip=True)
                if link_tag
                else node.get_text(" ", strip=True)
            )
            title = re.sub(r"\s+", " ", title).strip()
            if not title or len(title) < 12 or title in seen:
                continue
            seen.add(title)
            summary = node.get_text(" ", strip=True)[:500]
            items.append(
                MarketItem(
                    title=title[:200],
                    url=_absolutize(self.source.url, href),
                    source=self.source.name,
                    summary=summary,
                )
            )
            if len(items) >= max_items:
                break
        return items


def _absolutize(base: str, href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        return "https:" + href
    return urljoin(base, href)
