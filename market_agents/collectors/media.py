from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from market_agents.collectors.base import BaseCollector
from market_agents.config import IndustryMediaSource
from market_agents.firms import extract_candidate_firm_names, normalize_firm_name
from market_agents.models import MarketItem

USER_AGENT = "MarketAgentsLocal/0.3 (+local research bot; polite crawl)"

_KIND_DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "trade_fair": [
        "exhibitor",
        "exhibitors",
        "aussteller",
        "wystawc",
        "stand",
        "booth",
        "hall",
        "fair",
        "messe",
        "targi",
        "show",
    ],
    "magazine": [
        "article",
        "news",
        "report",
        "interview",
        "feature",
        "magazin",
        "czasopism",
        "wydanie",
        "issue",
    ],
    "portal": [
        "news",
        "article",
        "press",
        "product",
        "tool",
        "machining",
        "cutting",
        "katalog",
        "catalog",
    ],
}


class IndustryMediaCollector(BaseCollector):
    """
    Parsing targów branżowych, czasopism i portali WWW.
    Zwraca MarketItem z tagami media kind + opcjonalną listą wystawców.
    """

    def __init__(self, source: IndustryMediaSource) -> None:
        self.source = source
        self.name = f"media:{source.kind}:{source.name}"

    def collect(self, max_items: int = 20) -> list[MarketItem]:
        if not self.source.enabled:
            return []
        hub = self.discover_links(max_links=max(self.source.max_links, max_items))
        items: list[MarketItem] = []
        # hub as first signal
        items.append(
            MarketItem(
                title=f"[{self.source.kind}] {self.source.name}",
                url=self.source.url,
                source=self.name,
                summary=hub.get("excerpt") or f"Hub {self.source.kind}: {self.source.name}",
                content=str(hub.get("excerpt") or "")[:4000],
                tags=[self.source.kind, "industry_media", "hub"],
                analysis={
                    "media_kind": self.source.kind,
                    "media_name": self.source.name,
                    "link_count": hub.get("count", 0),
                    "exhibitors": hub.get("exhibitors") or [],
                },
                relevance_score=0.55,
            )
        )
        for link in (hub.get("links") or [])[: max(0, max_items - 1)]:
            items.append(
                MarketItem(
                    title=str(link.get("title") or link.get("url") or "")[:200],
                    url=str(link.get("url") or ""),
                    source=self.name,
                    summary=str(link.get("title") or "")[:400],
                    tags=[self.source.kind, "industry_media", "discovered_link"],
                    analysis={
                        "media_kind": self.source.kind,
                        "media_name": self.source.name,
                        "from_hub": self.source.url,
                    },
                    relevance_score=0.5,
                )
            )
        return items[:max_items]

    def discover_links(self, max_links: int | None = None) -> dict:
        max_links = int(max_links or self.source.max_links or 40)
        html = self._fetch(self.source.url)
        soup = BeautifulSoup(html, "lxml")
        excerpt = _page_excerpt(soup)
        keywords = [
            *(self.source.link_keywords or []),
            *_KIND_DEFAULT_KEYWORDS.get(self.source.kind, []),
        ]
        keywords = [k.lower() for k in keywords if k]

        links: list[dict[str, str]] = []
        seen: set[str] = set()
        base_host = urlparse(self.source.url).netloc.lower()

        candidates = []
        if self.source.css_selector:
            candidates = soup.select(self.source.css_selector)
        if not candidates:
            candidates = soup.find_all("a", href=True)

        for node in candidates:
            a = node if getattr(node, "name", None) == "a" else node.find("a", href=True)
            if a is None or not a.get("href"):
                continue
            href = _absolutize(self.source.url, a["href"])
            if not href.startswith("http"):
                continue
            if href in seen or href.rstrip("/") == self.source.url.rstrip("/"):
                continue
            title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            blob = f"{title} {href}".lower()
            host = urlparse(href).netloc.lower()
            same_host = host == base_host or host.endswith("." + base_host)
            keyword_hit = any(k in blob for k in keywords) if keywords else True
            # dla portali/czasopism bierz same-host + keyword; dla targów też keyword off-site (listy wystawców)
            if self.source.kind == "trade_fair":
                if not (keyword_hit or same_host):
                    continue
            else:
                if not same_host:
                    continue
                if keywords and not keyword_hit:
                    # nadal akceptuj ścieżki news/article
                    if not re.search(r"/(news|article|press|magazin|blog)/", href.lower()):
                        continue
            if not title or len(title) < 3:
                title = href.rstrip("/").split("/")[-1].replace("-", " ")[:120]
            seen.add(href)
            links.append({"title": title[:200], "url": href})
            if len(links) >= max_links:
                break

        exhibitors: list[str] = []
        if self.source.extract_exhibitors or self.source.kind == "trade_fair":
            exhibitors = extract_exhibitors_from_html(html, limit=60)

        return {
            "ok": True,
            "source": self.source.name,
            "kind": self.source.kind,
            "url": self.source.url,
            "count": len(links),
            "links": links,
            "exhibitors": exhibitors,
            "excerpt": excerpt[:1500],
        }

    def parse_page(self, url: str | None = None, max_chars: int = 12000) -> dict:
        target = (url or self.source.url).strip()
        html = self._fetch(target)
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript", "nav", "footer", "aside", "form"]):
            tag.decompose()
        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            title = title or h1.get_text(strip=True)
        article = soup.find("article") or soup.find("main") or soup.body
        text = ""
        if article:
            parts = [
                p.get_text(" ", strip=True)
                for p in article.find_all(["p", "li", "h1", "h2", "h3", "td"])
                if p.get_text(strip=True)
            ]
            text = re.sub(r"\s+\n", "\n", "\n".join(parts)).strip()
        text = text[:max_chars]
        firms = extract_candidate_firm_names(f"{title}. {text[:2000]}", max_names=15)
        exhibitors = (
            extract_exhibitors_from_html(html, limit=80)
            if self.source.kind == "trade_fair"
            else []
        )
        return {
            "ok": bool(text) and len(text) >= 80,
            "url": target,
            "title": title or target,
            "chars": len(text),
            "excerpt": text[:1500],
            "text": text,
            "candidate_firms": firms,
            "exhibitors": exhibitors,
            "media_kind": self.source.kind,
            "media_name": self.source.name,
        }

    def _fetch(self, url: str) -> str:
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
        with httpx.Client(timeout=35, follow_redirects=True, headers=headers) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text


def extract_exhibitors_from_html(html: str, limit: int = 60) -> list[str]:
    """Heurystyka: listy wystawców z nagłówków/linków/tabel."""
    soup = BeautifulSoup(html, "lxml")
    names: list[str] = []
    seen: set[str] = set()

    # 1) jawne sekcje wystawców
    for node in soup.find_all(["a", "li", "td", "h2", "h3", "h4"]):
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if not text or len(text) < 2 or len(text) > 80:
            continue
        parent_blob = ""
        parent = node.parent
        if parent is not None:
            parent_blob = " ".join(parent.get("class", []) if hasattr(parent, "get") else [])
            parent_blob += " " + str(parent.get("id") or "")
        blob = f"{text} {parent_blob}".lower()
        if not re.search(r"exhibit|ausstell|wystaw|booth|stand|hall", blob):
            # i tak bierz Title Case / ALLCAPS marki w listach
            if not re.match(r"^[A-Z][\w&./+-]*(?:\s+[A-Z][\w&./+-]*){0,4}$", text):
                continue
        # odrzuć oczywiste UI
        if text.lower() in {
            "exhibitors",
            "exhibitor list",
            "aussteller",
            "wystawcy",
            "home",
            "news",
            "contact",
            "login",
            "register",
            "search",
        }:
            continue
        key = normalize_firm_name(text)
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(text)
        if len(names) >= limit:
            break

    # 2) fallback NER-heurystyka z całego tekstu strony
    if len(names) < 5:
        plain = soup.get_text(" ", strip=True)[:8000]
        for cand in extract_candidate_firm_names(plain, max_names=limit):
            key = normalize_firm_name(cand)
            if key and key not in seen:
                seen.add(key)
                names.append(cand)
            if len(names) >= limit:
                break
    return names[:limit]


def _page_excerpt(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    article = soup.find("article") or soup.find("main") or soup.body
    if not article:
        return ""
    text = article.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _absolutize(base: str, href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        return "https:" + href
    return urljoin(base, href)
