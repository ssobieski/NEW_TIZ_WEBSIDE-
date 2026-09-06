from __future__ import annotations

import io
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from market_agents.collectors.base import BaseCollector
from market_agents.config import CatalogSource
from market_agents.models import MarketItem

# Wzorce URL / tekstu wskazujące na katalogi, PDF, e-shop, publikacje
_ASSET_HINTS = re.compile(
    r"(ecatalog|e-catalog|e_catalog|digital.?catalog|online.?catalog|"
    r"katalog|catalogue|catalog|webshop|e-?shop|shop|store|"
    r"download|publication|handbook|brochure|flyer|pdf|"
    r"flip.?catalog|online.?catalogue)",
    re.I,
)
_PDF_EXT = re.compile(r"\.pdf(\?|#|$)", re.I)
_USER_AGENT = "MarketAgentsLocal/0.3 (+catalog research; polite crawl)"


class CatalogCollector(BaseCollector):
    """
    Monitor e-katalogów, PDF-ów, publikacji i e-shopów konkurencji.

    - URL PDF → meta + opcjonalna ekstrakcja tekstu (pypdf)
    - Strona HTML → discovery linków PDF / catalog / shop
    """

    def __init__(self, source: CatalogSource) -> None:
        self.source = source
        self.name = source.name

    def collect(self, max_items: int = 15) -> list[MarketItem]:
        kind = (self.source.kind or "ecatalog").lower()
        url = self.source.url.strip()
        if not url:
            return []

        if _PDF_EXT.search(url) or kind == "pdf":
            item = self._collect_pdf(url)
            return [item] if item else []

        items = self._collect_hub(url, max_items=max_items)
        # Zawsze dodaj sam hub jako sygnał (nawet bez discovery)
        hub = MarketItem(
            title=f"{self.source.brand or self.source.name}: {kind}",
            url=url,
            source=f"catalog:{self.source.name}",
            summary=(
                f"Źródło katalogowe ({kind}) — {self.source.brand or self.source.name}. "
                f"Odkryto {len(items)} powiązanych assetów."
            ),
            tags=[kind, "catalog_hub", *( [self.source.brand] if self.source.brand else [])],
            relevance_score=0.55,
        )
        # Unikaj duplikatu hubu jeśli już jest w discovery
        if not any(i.url.rstrip("/") == url.rstrip("/") for i in items):
            items.insert(0, hub)
        return items[:max_items]

    def discover_assets(self, max_links: int = 40) -> list[dict[str, Any]]:
        """Publiczne API dla tooli agenta — lista odkrytych assetów."""
        url = self.source.url.strip()
        if not url:
            return []
        if _PDF_EXT.search(url):
            meta = self._pdf_meta(url)
            return [meta] if meta.get("ok") else []
        return self._discover_from_html(url, max_links=max_links)

    def fetch_pdf_text(self, url: str | None = None, max_pages: int = 8, max_chars: int = 12000) -> dict[str, Any]:
        target = (url or self.source.url).strip()
        meta = self._pdf_meta(target)
        if not meta.get("ok"):
            return meta
        text = self._extract_pdf_text(target, max_pages=max_pages, max_chars=max_chars)
        meta["text_excerpt"] = text
        meta["chars"] = len(text)
        return meta

    def _collect_pdf(self, url: str) -> MarketItem | None:
        meta = self._pdf_meta(url)
        if not meta.get("ok"):
            return MarketItem(
                title=f"PDF (niedostępny): {self.source.name}",
                url=url,
                source=f"catalog:{self.source.name}",
                summary=str(meta.get("error") or "PDF fetch failed"),
                tags=["pdf", "error"],
                relevance_score=0.2,
            )
        text = ""
        if self.source.extract_text:
            text = self._extract_pdf_text(
                url,
                max_pages=self.source.max_pdf_pages,
                max_chars=self.source.max_pdf_chars,
            )
        brand = self.source.brand or self.source.name
        title = meta.get("filename") or f"{brand} catalog PDF"
        return MarketItem(
            title=str(title)[:200],
            url=url,
            source=f"catalog:{self.source.name}",
            summary=(
                f"PDF katalog / publikacja — {brand}. "
                f"size={meta.get('content_length')} type={meta.get('content_type')}"
            ),
            content=text,
            tags=["pdf", "catalog", brand],
            relevance_score=0.7,
        )

    def _collect_hub(self, url: str, max_items: int) -> list[MarketItem]:
        assets = self._discover_from_html(url, max_links=max_items * 3)
        items: list[MarketItem] = []
        brand = self.source.brand or self.source.name
        for asset in assets:
            aurl = str(asset.get("url") or "")
            if not aurl:
                continue
            atype = str(asset.get("asset_type") or "link")
            label = str(asset.get("text") or atype)[:180]
            items.append(
                MarketItem(
                    title=f"{brand}: {label}"[:200],
                    url=aurl,
                    source=f"catalog:{self.source.name}",
                    summary=f"Odkryty asset ({atype}) z hubu {url}",
                    tags=[atype, "discovered", brand],
                    relevance_score=0.65 if atype == "pdf" else 0.5,
                )
            )
            if len(items) >= max_items:
                break
        return items

    def _discover_from_html(self, url: str, max_links: int = 40) -> list[dict[str, Any]]:
        headers = {"User-Agent": _USER_AGENT}
        try:
            with httpx.Client(timeout=35, follow_redirects=True, headers=headers) as client:
                resp = client.get(url)
                resp.raise_for_status()
                html = resp.text
        except Exception as exc:  # noqa: BLE001
            return [{"ok": False, "url": url, "error": str(exc)}]

        soup = BeautifulSoup(html, "lxml")
        found: list[dict[str, Any]] = []
        seen: set[str] = set()

        for a in soup.find_all("a", href=True):
            href = str(a.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            abs_url = _absolutize(url, href)
            if abs_url in seen:
                continue
            text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:160]
            asset_type = _classify_asset(abs_url, text)
            if asset_type is None and not self.source.discover_all_links:
                continue
            seen.add(abs_url)
            found.append(
                {
                    "ok": True,
                    "url": abs_url,
                    "text": text,
                    "asset_type": asset_type or "link",
                    "hub": url,
                    "brand": self.source.brand or self.source.name,
                }
            )
            if len(found) >= max_links:
                break

        # Preferuj PDF / catalog / shop
        priority = {"pdf": 0, "ecatalog": 1, "digital_catalogue": 2, "eshop": 3, "publication": 4, "link": 9}
        found.sort(key=lambda x: priority.get(str(x.get("asset_type")), 8))
        return found

    def _pdf_meta(self, url: str) -> dict[str, Any]:
        headers = {"User-Agent": _USER_AGENT}
        try:
            with httpx.Client(timeout=40, follow_redirects=True, headers=headers) as client:
                # Najpierw HEAD; jeśli zablokowane — GET z Range
                try:
                    head = client.head(url)
                    if head.status_code >= 400:
                        raise httpx.HTTPStatusError("head failed", request=head.request, response=head)
                    ctype = head.headers.get("content-type", "")
                    clen = head.headers.get("content-length")
                except Exception:  # noqa: BLE001
                    head = client.get(url, headers={**headers, "Range": "bytes=0-1023"})
                    ctype = head.headers.get("content-type", "")
                    clen = head.headers.get("content-length") or head.headers.get("content-range")
                filename = _filename_from_url(url)
                return {
                    "ok": True,
                    "url": str(head.url),
                    "content_type": ctype,
                    "content_length": clen,
                    "filename": filename,
                    "status_code": head.status_code,
                    "asset_type": "pdf",
                    "brand": self.source.brand or self.source.name,
                }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "url": url, "error": str(exc), "asset_type": "pdf"}

    def _extract_pdf_text(self, url: str, max_pages: int = 8, max_chars: int = 12000) -> str:
        try:
            from pypdf import PdfReader
        except ImportError:
            return "[pypdf niedostępny — pip install pypdf]"

        headers = {"User-Agent": _USER_AGENT}
        try:
            with httpx.Client(timeout=90, follow_redirects=True, headers=headers) as client:
                resp = client.get(url)
                resp.raise_for_status()
                data = resp.content
        except Exception as exc:  # noqa: BLE001
            return f"[błąd pobierania PDF: {exc}]"

        try:
            reader = PdfReader(io.BytesIO(data))
            parts: list[str] = []
            for i, page in enumerate(reader.pages[: max(1, max_pages)]):
                try:
                    parts.append(page.extract_text() or "")
                except Exception:  # noqa: BLE001
                    continue
                if sum(len(p) for p in parts) >= max_chars:
                    break
            text = re.sub(r"\s+", " ", "\n".join(parts)).strip()
            return text[:max_chars]
        except Exception as exc:  # noqa: BLE001
            return f"[błąd ekstrakcji PDF: {exc}]"


def _classify_asset(url: str, text: str) -> str | None:
    blob = f"{url} {text}".lower()
    if _PDF_EXT.search(url) or "application/pdf" in blob:
        return "pdf"
    if re.search(r"e-?shop|webshop|/shop|/store|buy.?online", blob):
        return "eshop"
    if re.search(r"ecatalog|e-catalog|digital.?catalog|online.?catalog|flip.?catalog|katalog.?online", blob):
        return "ecatalog"
    if re.search(r"handbook|publication|brochure|flyer|white.?paper|literature", blob):
        return "publication"
    if re.search(r"catalog|catalogue|katalog|download", blob):
        return "digital_catalogue"
    if _ASSET_HINTS.search(blob):
        return "digital_catalogue"
    return None


def _absolutize(base: str, href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        return "https:" + href
    return urljoin(base, href)


def _filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = path.rstrip("/").split("/")[-1] or "document.pdf"
    return name[:200]
