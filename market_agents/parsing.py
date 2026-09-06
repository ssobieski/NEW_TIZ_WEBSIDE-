from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from market_agents.parse_rules import SiteParseRulesStore, host_from_url
from market_agents.polite_http import AdaptivePoliteFetcher, DEFAULT_USER_AGENT

try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None  # type: ignore


USER_AGENT = DEFAULT_USER_AGENT


@dataclass
class ParsedDocument:
    url: str
    title: str = ""
    text: str = ""
    author: str | None = None
    published_at: str | None = None
    language: str | None = None
    links: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    parse_method: str = "none"
    ok: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def excerpt(self) -> str:
        return (self.text or "")[:1200]


class IntelligentParser:
    """
    Inteligentny parsing stron — agent może go rozwijać przez SiteParseRulesStore:
    1) reguła hosta (preferred_method / css_selector)
    2) trafilatura
    3) fallback BeautifulSoup
    """

    def __init__(
        self,
        timeout: int = 30,
        rules: SiteParseRulesStore | None = None,
        learn: bool = True,
        fetcher: AdaptivePoliteFetcher | None = None,
    ) -> None:
        self.timeout = timeout
        self.rules = rules
        self.learn = learn
        self.fetcher = fetcher

    def fetch_html(self, url: str) -> str:
        headers = {"Accept": "text/html,application/xhtml+xml"}
        if self.fetcher is not None:
            return self.fetcher.get_text(
                url, headers=headers, timeout=float(self.timeout)
            )
        # fallback (testy / ad-hoc) — nadal przez shared polite fetcher
        return AdaptivePoliteFetcher.shared().get_text(
            url, headers=headers, timeout=float(self.timeout)
        )

    def parse_url(
        self,
        url: str,
        *,
        force_method: str | None = None,
        css_selector: str | None = None,
    ) -> ParsedDocument:
        try:
            html = self.fetch_html(url)
            return self.parse_html(
                url, html, force_method=force_method, css_selector=css_selector
            )
        except Exception as exc:  # noqa: BLE001
            return ParsedDocument(url=url, ok=False, error=str(exc))

    def parse_html(
        self,
        url: str,
        html: str,
        *,
        force_method: str | None = None,
        css_selector: str | None = None,
    ) -> ParsedDocument:
        rule = self.rules.get(url) if self.rules else None
        method = (force_method or (rule.preferred_method if rule else "auto") or "auto")
        method = method.strip().lower()
        selector = (
            css_selector
            if css_selector is not None
            else (rule.css_selector if rule else "")
        ) or ""
        drop = list(rule.drop_selectors) if rule else []
        min_chars = int(rule.min_chars) if rule else 120

        def _finish(doc: ParsedDocument) -> ParsedDocument:
            doc.links = self._extract_links(url, html)
            doc.meta = {
                **(doc.meta or {}),
                "rule_host": host_from_url(url),
                "requested_method": method,
                "applied_selector": selector or None,
            }
            if self.learn and self.rules is not None and doc.ok and len(doc.text) >= 120:
                self._auto_learn(url, doc)
            return doc

        if method == "css" and selector:
            css_doc = self._css(
                url, html, selector, drop_selectors=drop, min_chars=min_chars
            )
            if css_doc.ok:
                return _finish(css_doc)

        if method == "trafilatura":
            doc = self._trafilatura(url, html)
            if doc.ok and len(doc.text) >= max(min_chars, 120):
                return _finish(doc)
        elif method == "bs4":
            doc = self._beautifulsoup(
                url, html, drop_selectors=drop, min_chars=min_chars
            )
            if doc.ok:
                return _finish(doc)

        if method == "auto" and selector:
            css_doc = self._css(
                url, html, selector, drop_selectors=drop, min_chars=min_chars
            )
            if css_doc.ok:
                return _finish(css_doc)

        doc = self._trafilatura(url, html)
        if doc.ok and len(doc.text) >= max(280, min_chars):
            return _finish(doc)

        fallback = self._beautifulsoup(
            url, html, drop_selectors=drop, min_chars=min_chars
        )
        if not fallback.ok and doc.ok:
            return _finish(doc)
        return _finish(fallback)

    def _auto_learn(self, url: str, doc: ParsedDocument) -> None:
        if self.rules is None:
            return
        host = host_from_url(url)
        if not host:
            return
        existing = self.rules.get(host)
        if existing and existing.origin == "manual" and existing.css_selector:
            self.rules.rate(
                url,
                quality_0_to_1=0.8,
                parse_method=None,
                notes="auto-ok",
            )
            return
        method = doc.parse_method if doc.parse_method in {"trafilatura", "bs4", "css"} else None
        self.rules.rate(
            url,
            quality_0_to_1=0.85 if len(doc.text) >= 280 else 0.65,
            parse_method=method,
            notes="auto-learn from successful parse",
        )

    def _css(
        self,
        url: str,
        html: str,
        selector: str,
        *,
        drop_selectors: list[str] | None = None,
        min_chars: int = 120,
    ) -> ParsedDocument:
        try:
            soup = BeautifulSoup(html, "lxml")
            for sel in drop_selectors or []:
                for tag in soup.select(sel):
                    tag.decompose()
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            nodes = soup.select(selector)
            if not nodes:
                return ParsedDocument(
                    url=url,
                    ok=False,
                    error=f"css selector empty: {selector}",
                    parse_method="css",
                )
            chunks = [n.get_text(" ", strip=True) for n in nodes if n.get_text(strip=True)]
            text = _normalize_whitespace("\n".join(chunks))
            text = _drop_short_noise(text)
            title = _title_from_html(html) or url
            return ParsedDocument(
                url=url,
                title=title,
                text=text,
                parse_method="css",
                ok=len(text) >= min_chars,
                meta={"chars": len(text), "selector": selector, "nodes": len(nodes)},
            )
        except Exception as exc:  # noqa: BLE001
            return ParsedDocument(url=url, ok=False, error=str(exc), parse_method="css")

    def _trafilatura(self, url: str, html: str) -> ParsedDocument:
        if trafilatura is None:
            return ParsedDocument(url=url, ok=False, error="trafilatura not installed")
        try:
            extracted = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                favor_precision=True,
                url=url,
                output_format="txt",
            )
            meta = None
            if hasattr(trafilatura, "extract_metadata"):
                meta = trafilatura.extract_metadata(html)
            title = ""
            author = None
            published = None
            language = None
            if meta is not None:
                title = getattr(meta, "title", None) or ""
                author = getattr(meta, "author", None)
                published = getattr(meta, "date", None)
                language = getattr(meta, "language", None)
            text = _normalize_whitespace(extracted or "")
            if not title:
                title = _title_from_html(html)
            ok = bool(text) and len(text) >= 120
            return ParsedDocument(
                url=url,
                title=title or url,
                text=text,
                author=author,
                published_at=published,
                language=language,
                parse_method="trafilatura",
                ok=ok,
                meta={"chars": len(text)},
            )
        except Exception as exc:  # noqa: BLE001
            return ParsedDocument(
                url=url, ok=False, error=str(exc), parse_method="trafilatura"
            )

    def _beautifulsoup(
        self,
        url: str,
        html: str,
        *,
        drop_selectors: list[str] | None = None,
        min_chars: int = 120,
    ) -> ParsedDocument:
        soup = BeautifulSoup(html, "lxml")
        for sel in drop_selectors or []:
            for tag in soup.select(sel):
                tag.decompose()
        for tag in soup(["script", "style", "noscript", "nav", "footer", "aside", "form"]):
            tag.decompose()
        title = _title_from_html(html) or (
            soup.title.get_text(strip=True) if soup.title else url
        )
        article = soup.find("article") or soup.find("main") or soup.body
        if article is None:
            return ParsedDocument(
                url=url, title=title, ok=False, error="no body", parse_method="bs4"
            )
        paragraphs = [
            p.get_text(" ", strip=True)
            for p in article.find_all(["p", "li", "h1", "h2", "h3"])
            if p.get_text(strip=True)
        ]
        text = _normalize_whitespace("\n".join(paragraphs))
        text = _drop_short_noise(text)
        return ParsedDocument(
            url=url,
            title=title,
            text=text,
            parse_method="bs4",
            ok=len(text) >= min_chars,
            meta={"chars": len(text), "paragraphs": len(paragraphs)},
        )

    def _extract_links(self, base_url: str, html: str, limit: int = 30) -> list[str]:
        soup = BeautifulSoup(html, "lxml")
        out: list[str] = []
        seen: set[str] = set()
        base_host = urlparse(base_url).netloc
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"])
            if not href.startswith("http"):
                continue
            if href in seen:
                continue
            path = urlparse(href).path.lower()
            if urlparse(href).netloc == base_host or any(
                x in path for x in ("/news", "/artykul", "/blog", "/press", "/aktualn")
            ):
                seen.add(href)
                out.append(href)
            if len(out) >= limit:
                break
        return out


def _title_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return str(og["content"]).strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else ""


def _normalize_whitespace(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _drop_short_noise(text: str) -> str:
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if len(s) < 25 and not s.endswith("."):
            continue
        lines.append(s)
    return "\n".join(lines)
