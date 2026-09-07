from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import feedparser
from bs4 import BeautifulSoup

from market_agents.collectors.base import BaseCollector
from market_agents.config import SocialMediaSource
from market_agents.firms import extract_candidate_firm_names
from market_agents.models import MarketItem
from market_agents.polite_http import AdaptivePoliteFetcher

SOCIAL_PLATFORMS = (
    "youtube",
    "linkedin",
    "twitter",
    "x",
    "facebook",
    "instagram",
    "tiktok",
    "other",
)

_DEFAULT_KEYWORDS = [
    "catalog",
    "catalogue",
    "katalog",
    "cutting",
    "tooling",
    "carbide",
    "insert",
    "milling",
    "drilling",
    "turning",
    "launch",
    "new product",
    "handbook",
    "application",
    "EMO",
    "AMB",
    "IMTS",
    "ISO 13399",
    "preis",
    "cennik",
    "price list",
]

_POST_PATH = re.compile(
    r"/(watch|shorts|reel|reels|p|posts?|status|videos?|activity|pulse)/|"
    r"[?&]v=|/@[\\w.-]+/video/",
    re.I,
)


def detect_platform(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "linkedin.com" in host:
        return "linkedin"
    if "twitter.com" in host or host == "x.com" or host.endswith(".x.com"):
        return "x"
    if "facebook.com" in host or "fb.com" in host:
        return "facebook"
    if "instagram.com" in host:
        return "instagram"
    if "tiktok.com" in host:
        return "tiktok"
    return "other"


def youtube_feed_url(channel_id: str | None = None, feed_url: str | None = None, page_url: str = "") -> str | None:
    if feed_url:
        return feed_url.strip()
    if channel_id:
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id.strip()}"
    # ?channel_id= in URL
    qs = parse_qs(urlparse(page_url).query)
    if qs.get("channel_id"):
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={qs['channel_id'][0]}"
    return None


class SocialMediaCollector(BaseCollector):
    """
    Parsing publicznych profili / kanałów social (bez logowania).

    - YouTube: preferuj oficjalny RSS (channel_id / feed_url)
    - Inne platformy: polite HTML publicznych stron firmowych + odkrywanie postów/linków
    """

    def __init__(
        self,
        source: SocialMediaSource,
        fetcher: AdaptivePoliteFetcher | None = None,
        lookback_hours: int = 720,
    ) -> None:
        self.source = source
        self.platform = (source.platform or detect_platform(source.url) or "other").lower()
        if self.platform == "twitter":
            self.platform = "x"
        self.name = f"social:{self.platform}:{source.name}"
        self.fetcher = fetcher or AdaptivePoliteFetcher.shared()
        self.lookback_hours = lookback_hours

    def collect(self, max_items: int | None = None) -> list[MarketItem]:
        if not self.source.enabled:
            return []
        limit = int(max_items or self.source.max_items or 20)
        posts = self.discover_posts(max_items=limit)
        items: list[MarketItem] = []
        # hub signal
        items.append(
            MarketItem(
                title=f"[social:{self.platform}] {self.source.brand or self.source.name}",
                url=self.source.url,
                source=self.name,
                summary=posts.get("excerpt") or f"Social hub {self.platform}: {self.source.name}",
                content=str(posts.get("excerpt") or "")[:3000],
                tags=[self.platform, "social_media", "hub"],
                analysis={
                    "social_platform": self.platform,
                    "social_name": self.source.name,
                    "brand": self.source.brand,
                    "post_count": posts.get("count", 0),
                    "mode": posts.get("mode"),
                },
                relevance_score=0.5,
            )
        )
        for post in (posts.get("posts") or [])[: max(0, limit - 1)]:
            items.append(
                MarketItem(
                    title=str(post.get("title") or "")[:200],
                    url=str(post.get("url") or ""),
                    source=self.name,
                    published_at=post.get("published_at"),
                    summary=str(post.get("summary") or post.get("title") or "")[:500],
                    content=str(post.get("summary") or "")[:4000],
                    tags=[self.platform, "social_media", "post"],
                    analysis={
                        "social_platform": self.platform,
                        "social_name": self.source.name,
                        "brand": self.source.brand or post.get("brand"),
                        "from_hub": self.source.url,
                        "signal_hints": post.get("signal_hints") or [],
                    },
                    relevance_score=0.55 if post.get("signal_hints") else 0.45,
                )
            )
        return items[:limit]

    def discover_posts(self, max_items: int | None = None) -> dict[str, Any]:
        limit = int(max_items or self.source.max_items or 20)
        feed = youtube_feed_url(self.source.channel_id, self.source.feed_url, self.source.url)
        if self.platform == "youtube" and feed:
            return self._from_feed(feed, limit=limit)
        if self.source.feed_url:
            return self._from_feed(self.source.feed_url, limit=limit)
        return self._from_html(self.source.url, limit=limit)

    def parse_post(self, url: str | None = None, max_chars: int = 8000) -> dict[str, Any]:
        target = (url or self.source.url).strip()
        try:
            html = self.fetcher.get_text(target, timeout=35)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "url": target, "error": str(exc), "social_platform": self.platform}

        soup = BeautifulSoup(html, "lxml")
        title = _meta(soup, "og:title") or _meta(soup, "twitter:title")
        if not title and soup.title and soup.title.string:
            title = soup.title.string.strip()
        desc = _meta(soup, "og:description") or _meta(soup, "twitter:description") or ""
        for tag in soup(["script", "style", "noscript", "nav", "footer", "form"]):
            tag.decompose()
        body = soup.find("article") or soup.find("main") or soup.body
        text = ""
        if body:
            parts = [
                p.get_text(" ", strip=True)
                for p in body.find_all(["p", "li", "h1", "h2", "h3"])
                if p.get_text(strip=True)
            ]
            text = re.sub(r"\s+", " ", "\n".join(parts)).strip()
        if not text and desc:
            text = desc
        text = text[:max_chars]
        blob = f"{title} {desc} {text}"
        return {
            "ok": bool(text) and len(text) >= 40,
            "url": target,
            "title": (title or target)[:200],
            "chars": len(text),
            "excerpt": text[:1200],
            "text": text,
            "candidate_firms": extract_candidate_firm_names(blob[:2500], max_names=12),
            "signal_hints": _signal_hints(blob),
            "social_platform": self.platform,
            "social_name": self.source.name,
            "brand": self.source.brand,
            "access_note": (
                "public HTML only — wiele platform social wymaga logowania; "
                "dla YouTube preferuj channel_id/RSS"
            ),
        }

    def _from_feed(self, feed_url: str, limit: int) -> dict[str, Any]:
        feed = feedparser.parse(feed_url)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)
        posts: list[dict[str, Any]] = []
        for entry in feed.entries[: limit * 2]:
            published = _entry_date(entry)
            if published and published < cutoff:
                continue
            title = (getattr(entry, "title", "") or "").strip()
            link = (getattr(entry, "link", "") or "").strip()
            if not title or not link:
                continue
            summary = getattr(entry, "summary", None) or getattr(entry, "description", None) or ""
            summary = _strip_html(str(summary))
            hints = _signal_hints(f"{title} {summary}")
            posts.append(
                {
                    "title": title[:200],
                    "url": link,
                    "summary": summary[:800],
                    "published_at": published.isoformat() if published else None,
                    "signal_hints": hints,
                    "brand": self.source.brand,
                }
            )
            if len(posts) >= limit:
                break
        return {
            "ok": True,
            "mode": "rss",
            "source": self.source.name,
            "platform": self.platform,
            "url": self.source.url,
            "feed_url": feed_url,
            "count": len(posts),
            "posts": posts,
            "excerpt": f"RSS {self.platform}: {len(posts)} postów z {feed_url}",
        }

    def _from_html(self, url: str, limit: int) -> dict[str, Any]:
        try:
            html = self.fetcher.get_text(url, timeout=35)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "mode": "html",
                "source": self.source.name,
                "platform": self.platform,
                "url": url,
                "count": 0,
                "posts": [],
                "error": str(exc),
                "excerpt": f"Nie udało się pobrać publicznego profilu ({exc})",
            }

        soup = BeautifulSoup(html, "lxml")
        excerpt = _meta(soup, "og:description") or _meta(soup, "description") or ""
        if not excerpt:
            body = soup.find("main") or soup.body
            if body:
                excerpt = re.sub(r"\s+", " ", body.get_text(" ", strip=True))[:800]

        keywords = [
            *(self.source.link_keywords or []),
            *_DEFAULT_KEYWORDS,
        ]
        keywords = [k.lower() for k in keywords if k]

        posts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = _absolutize(url, str(a.get("href") or ""))
            if not href.startswith("http") or href in seen:
                continue
            title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            blob = f"{title} {href}".lower()
            is_post = bool(_POST_PATH.search(href))
            keyword_hit = any(k in blob for k in keywords)
            same_platform = detect_platform(href) == self.platform or detect_platform(href) == "other"
            if not (is_post or keyword_hit):
                continue
            if self.platform != "other" and not same_platform and not keyword_hit:
                continue
            if not title or len(title) < 3:
                title = href.rstrip("/").split("/")[-1].replace("-", " ")[:120]
            seen.add(href)
            hints = _signal_hints(f"{title} {href}")
            posts.append(
                {
                    "title": title[:200],
                    "url": href,
                    "summary": title[:400],
                    "published_at": None,
                    "signal_hints": hints,
                    "brand": self.source.brand,
                }
            )
            if len(posts) >= limit:
                break

        # og:url as fallback post if nothing found
        if not posts:
            og = _meta(soup, "og:title")
            if og:
                posts.append(
                    {
                        "title": og[:200],
                        "url": url,
                        "summary": excerpt[:400],
                        "published_at": None,
                        "signal_hints": _signal_hints(f"{og} {excerpt}"),
                        "brand": self.source.brand,
                    }
                )

        return {
            "ok": True,
            "mode": "html",
            "source": self.source.name,
            "platform": self.platform,
            "url": url,
            "count": len(posts),
            "posts": posts,
            "excerpt": excerpt[:1500],
            "access_note": (
                "Public HTML only. LinkedIn/X/Instagram często wymagają logowania — "
                "wtedy zostaje hub URL + meta; YouTube: ustaw channel_id dla RSS."
            ),
        }


def _signal_hints(text: str) -> list[str]:
    blob = (text or "").lower()
    hints: list[str] = []
    mapping = {
        "new_product": r"\b(new product|launch|unveils|introduces|nowość|premiera|neuheit)\b",
        "catalog": r"\b(catalog|catalogue|katalog|ecatalog|digital catalogue)\b",
        "cutting_data": r"\b(cutting data|feeds? and speeds?|schnittwerte|parametr)\b",
        "trade_fair": r"\b(emo|amb|imts|exhibitor|messe|targi)\b",
        "pricing": r"\b(price list|pricelist|preisliste|cennik)\b",
        "tech": r"\b(handbook|application guide|iso\s*13399|technical)\b",
    }
    for name, pat in mapping.items():
        if re.search(pat, blob, re.I):
            hints.append(name)
    return hints


def _meta(soup: BeautifulSoup, key: str) -> str:
    tag = soup.find("meta", property=key) or soup.find("meta", attrs={"name": key})
    if tag and tag.get("content"):
        return str(tag["content"]).strip()
    return ""


def _strip_html(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def _absolutize(base: str, href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        return "https:" + href
    return urljoin(base, href)


def _entry_date(entry: object) -> datetime | None:
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
