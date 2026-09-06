"""Collect books, articles and videos from configured literature sources."""

from __future__ import annotations

import re
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from market_agents.collectors.base import BaseCollector
from market_agents.config import LiteratureSource
from market_agents.literature import (
    LITERATURE_KINDS,
    classify_literature_kind,
    literature_topics,
)
from market_agents.models import MarketItem
from market_agents.polite_http import AdaptivePoliteFetcher

_DEFAULT_KEYWORDS = [
    "machining",
    "cutting",
    "tool",
    "tooling",
    "milling",
    "turning",
    "drilling",
    "chatter",
    "vibration",
    "handbook",
    "isbn",
    "doi",
    "webinar",
    "lecture",
    "proceedings",
    "obrób",
    "skrawan",
    "frez",
    "toczen",
]


def _abs(base: str, href: str) -> str | None:
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    return urljoin(base, href)


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class LiteratureCollector(BaseCollector):
    """
    Książki / artykuły / wideo / proceedings / whitepapers.
    Preferuj feed_url (RSS/Atom); inaczej polite HTML hub.
    """

    def __init__(
        self,
        source: LiteratureSource,
        fetcher: AdaptivePoliteFetcher | None = None,
    ) -> None:
        self.source = source
        kind = (source.kind or "mixed").lower()
        self.kind = kind if kind in {*LITERATURE_KINDS, "mixed"} else "mixed"
        self.name = f"literature:{self.kind}:{source.name}"
        self.fetcher = fetcher or AdaptivePoliteFetcher.shared()

    def collect(self, max_items: int | None = None) -> list[MarketItem]:
        if not self.source.enabled:
            return []
        limit = int(max_items or self.source.max_items or 25)
        discovered = self.discover_items(max_items=limit)
        items: list[MarketItem] = []
        items.append(
            MarketItem(
                title=f"[literature:{self.kind}] {self.source.name}",
                url=self.source.url,
                source=self.name,
                summary=discovered.get("excerpt")
                or f"Hub literatury ({self.kind}): {self.source.name}",
                content=str(discovered.get("excerpt") or "")[:4000],
                tags=[self.kind, "literature", "hub"],
                analysis={
                    "literature_kind": self.kind,
                    "literature_name": self.source.name,
                    "publisher": self.source.publisher,
                    "item_count": discovered.get("count", 0),
                    "mode": discovered.get("mode"),
                },
                relevance_score=0.55,
            )
        )
        for row in (discovered.get("items") or [])[: max(0, limit - 1)]:
            lit_kind = str(row.get("kind") or self.kind or "article")
            topics = list(row.get("topics") or [])
            items.append(
                MarketItem(
                    title=str(row.get("title") or row.get("url") or "")[:240],
                    url=str(row.get("url") or ""),
                    source=self.name,
                    published_at=row.get("published_at"),
                    summary=str(row.get("summary") or row.get("title") or "")[:500],
                    content=str(row.get("summary") or "")[:4000],
                    tags=[lit_kind, "literature", "discovered", *topics[:4]],
                    analysis={
                        "literature_kind": lit_kind,
                        "literature_name": self.source.name,
                        "publisher": self.source.publisher or self.source.brand,
                        "topics": topics,
                        "from_hub": self.source.url,
                    },
                    relevance_score=0.6 if topics else 0.5,
                )
            )
        return items

    def discover_items(self, max_items: int = 25) -> dict[str, Any]:
        page_url = (self.source.url or "").strip()
        feed = (self.source.feed_url or "").strip()
        if feed:
            return self._from_feed(feed, max_items=max_items)
        if any(tok in page_url.lower() for tok in ("/rss", "/atom", "/feed", ".xml")):
            return self._from_feed(page_url, max_items=max_items)
        return self._from_html(page_url, max_items=max_items)

    def _from_feed(self, feed_url: str, max_items: int = 25) -> dict[str, Any]:
        text = self.fetcher.get_text(feed_url, timeout=45.0) or ""
        if not text:
            return {
                "ok": False,
                "mode": "feed",
                "url": feed_url,
                "count": 0,
                "items": [],
                "error": "empty feed",
            }
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            return {
                "ok": False,
                "mode": "feed",
                "url": feed_url,
                "count": 0,
                "items": [],
                "error": str(exc),
            }

        rows: list[dict[str, Any]] = []
        for node in list(root.iter()):
            tag = node.tag.rsplit("}", 1)[-1].lower()
            if tag not in {"item", "entry"}:
                continue
            title = ""
            link = ""
            summary = ""
            published: str | None = None
            for child in list(node):
                ctag = child.tag.rsplit("}", 1)[-1].lower()
                if ctag == "title" and child.text:
                    title = child.text.strip()
                elif ctag == "link":
                    href = (child.attrib.get("href") or child.text or "").strip()
                    if href:
                        link = href
                elif ctag in {"description", "summary", "content"} and (child.text or "").strip():
                    summary = re.sub(r"<[^>]+>", " ", child.text or "")
                    summary = re.sub(r"\s+", " ", summary).strip()[:1200]
                elif ctag in {"pubdate", "published", "updated"} and child.text:
                    try:
                        published = parsedate_to_datetime(child.text.strip()).isoformat()
                    except (TypeError, ValueError, IndexError):
                        published = child.text.strip()[:40]
            if not link:
                continue
            kind = self._resolve_kind(title, link, summary)
            if not self._accept_kind(kind):
                continue
            if not self._keyword_ok(f"{title} {summary} {link}"):
                continue
            topics = literature_topics(f"{title} {summary} {link}")
            rows.append(
                {
                    "title": title or link,
                    "url": link,
                    "kind": kind,
                    "summary": summary,
                    "published_at": published,
                    "topics": topics,
                    "host": _host(link),
                }
            )
            if len(rows) >= max_items:
                break
        return {
            "ok": True,
            "mode": "feed",
            "url": feed_url,
            "count": len(rows),
            "items": rows,
            "excerpt": f"Feed {feed_url}: {len(rows)} items",
        }

    def _from_html(self, page_url: str, max_items: int = 25) -> dict[str, Any]:
        html = self.fetcher.get_text(page_url, timeout=45.0) or ""
        if not html:
            return {
                "ok": False,
                "mode": "html",
                "url": page_url,
                "count": 0,
                "items": [],
                "error": "empty html",
            }
        soup = BeautifulSoup(html, "html.parser")
        excerpt = " ".join(soup.get_text(" ", strip=True).split())[:800]
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            abs_url = _abs(page_url, a["href"])
            if not abs_url or abs_url in seen:
                continue
            text = " ".join(a.stripped_strings) or abs_url
            kind = self._resolve_kind(text, abs_url, "")
            if not self._accept_kind(kind):
                continue
            if not self._keyword_ok(f"{text} {abs_url}"):
                # mixed hubs: require literature classification OR keyword
                if classify_literature_kind(abs_url, text) is None:
                    continue
            topics = literature_topics(f"{text} {abs_url}")
            seen.add(abs_url)
            rows.append(
                {
                    "title": text[:240],
                    "url": abs_url,
                    "kind": kind,
                    "summary": "",
                    "published_at": None,
                    "topics": topics,
                    "host": _host(abs_url),
                }
            )
            if len(rows) >= max_items:
                break
        return {
            "ok": True,
            "mode": "html",
            "url": page_url,
            "count": len(rows),
            "items": rows,
            "excerpt": excerpt,
        }

    def _resolve_kind(self, title: str, url: str, summary: str = "") -> str:
        detected = classify_literature_kind(url, f"{title} {summary}")
        if self.kind != "mixed" and self.kind in LITERATURE_KINDS:
            if detected in {None, "article"} and self.kind in {"article", "whitepaper", "proceedings"}:
                return self.kind
            if detected == self.kind:
                return detected
            if detected and self.kind == "mixed":
                return detected
            return self.kind
        return detected or "article"

    def _accept_kind(self, kind: str) -> bool:
        if self.kind == "mixed":
            return kind in LITERATURE_KINDS
        if self.kind == "article":
            return kind in {"article", "whitepaper", "proceedings"}
        return kind == self.kind or kind in LITERATURE_KINDS

    def _keyword_ok(self, blob: str) -> bool:
        keys = [k.lower() for k in (self.source.link_keywords or _DEFAULT_KEYWORDS) if k]
        low = blob.lower()
        if not keys:
            return True
        return any(k in low for k in keys)
