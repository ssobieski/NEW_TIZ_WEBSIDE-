from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None  # type: ignore


USER_AGENT = (
    "MarketAgentsLocal/0.2 (+local intelligence agent; respectful research crawler)"
)


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
    Inteligentny parsing stron:
    1) trafilatura (article extraction)
    2) fallback BeautifulSoup + heurystyki
    3) czyszczenie boilerplate / nawigacji
    """

    def __init__(self, timeout: int = 30) -> None:
        self.timeout = timeout

    def fetch_html(self, url: str) -> str:
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=headers) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text

    def parse_url(self, url: str) -> ParsedDocument:
        try:
            html = self.fetch_html(url)
            return self.parse_html(url, html)
        except Exception as exc:  # noqa: BLE001
            return ParsedDocument(url=url, ok=False, error=str(exc))

    def parse_html(self, url: str, html: str) -> ParsedDocument:
        doc = self._trafilatura(url, html)
        if doc.ok and len(doc.text) >= 280:
            doc.links = self._extract_links(url, html)
            return doc
        fallback = self._beautifulsoup(url, html)
        if not fallback.ok and doc.ok:
            doc.links = self._extract_links(url, html)
            return doc
        fallback.links = self._extract_links(url, html)
        return fallback

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

    def _beautifulsoup(self, url: str, html: str) -> ParsedDocument:
        soup = BeautifulSoup(html, "lxml")
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
            ok=len(text) >= 120,
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
