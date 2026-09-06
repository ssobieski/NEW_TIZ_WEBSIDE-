from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

LITERATURE_KINDS = (
    "book",
    "article",
    "video",
    "proceedings",
    "whitepaper",
)

_BOOK_HINTS = re.compile(
    r"\b(book|ksi[aą][zż]ka|textbook|monograph|handbuch|podr[eę]cznik|"
    r"isbn|springer|elsevier|wiley|crc\s*press)\b",
    re.I,
)
_ARTICLE_HINTS = re.compile(
    r"\b(article|paper|journal|arxiv|doi\.org|sciencedirect|researchgate|"
    r"proceedings?\s+of|conference\s+paper|publikacja|artyku[lł])\b",
    re.I,
)
_VIDEO_HINTS = re.compile(
    r"\b(video|webinar|youtube|youtu\.be|vimeo|playlist|lecture|wyk[lł]ad|"
    r"tutorial|watch\?v=)\b",
    re.I,
)
_PROCEEDINGS_HINTS = re.compile(
    r"\b(proceedings|conference|symposium|cirp|procedia|conf\.)\b",
    re.I,
)
_WHITEPAPER_HINTS = re.compile(
    r"\b(white\s*paper|whitepaper|technical\s*report|tech\s*report)\b",
    re.I,
)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:80] or "literature"


def _host(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    return host[4:] if host.startswith("www.") else host


def classify_literature_kind(url: str, text: str = "") -> str | None:
    blob = f"{url} {text}".lower()
    if _VIDEO_HINTS.search(blob) or "youtube.com" in blob or "youtu.be" in blob:
        return "video"
    if _BOOK_HINTS.search(blob):
        return "book"
    if _PROCEEDINGS_HINTS.search(blob):
        return "proceedings"
    if _WHITEPAPER_HINTS.search(blob):
        return "whitepaper"
    if _ARTICLE_HINTS.search(blob):
        return "article"
    return None


def literature_topics(text: str) -> list[str]:
    blob = (text or "").lower()
    topics = []
    mapping = {
        "cutting_dynamics": r"dynamik|vibration|chatter|stability",
        "tool_design": r"tool\s*design|geometr|insert|end\s*mill|drill",
        "parameters": r"cutting\s*param|vc\b|fz\b|feeds?\s*and\s*speeds?",
        "cam": r"\bcam\b|toolpath|nc\s*code",
        "coolant": r"coolant|ch[lł]odziw|mql|emulsion",
        "surface": r"surface\s*integrity|chropowato|roughness",
        "ai": r"\bai\b|machine\s*learning|neural|reinforcement",
        "iso13399": r"iso\s*13399|din\s*4000",
    }
    for name, pat in mapping.items():
        if re.search(pat, blob, re.I):
            topics.append(name)
    return topics


@dataclass
class LiteratureItem:
    """Książka / artykuł / wideo / materiały konferencyjne."""

    id: str
    title: str
    url: str
    kind: str = "article"
    authors: str | None = None
    year: str | None = None
    source: str | None = None
    brand: str | None = None
    topics: list[str] = field(default_factory=list)
    access: str = "unknown"  # public | paywall | login | unknown
    notes: str = ""
    found_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LiteratureItem":
        kind = str(raw.get("kind") or "article")
        if kind not in LITERATURE_KINDS:
            kind = "article"
        return cls(
            id=str(raw.get("id") or _slug(str(raw.get("url") or "literature"))),
            title=str(raw.get("title") or raw.get("url") or "item")[:300],
            url=str(raw.get("url") or "").strip(),
            kind=kind,
            authors=(str(raw["authors"]).strip() if raw.get("authors") else None),
            year=(str(raw["year"]).strip() if raw.get("year") else None),
            source=(str(raw["source"]).strip() if raw.get("source") else None),
            brand=(str(raw["brand"]).strip() if raw.get("brand") else None),
            topics=list(raw.get("topics") or []),
            access=str(raw.get("access") or "unknown"),
            notes=str(raw.get("notes") or "")[:1000],
            found_at=float(raw.get("found_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
            confirmed=bool(raw.get("confirmed")),
        )


class LiteratureRegistry:
    """Rejestr literatury (data/knowledge/literature.json)."""

    FILENAME = "literature.json"

    def __init__(self, items: dict[str, LiteratureItem] | None = None) -> None:
        self.items: dict[str, LiteratureItem] = dict(items or {})

    @classmethod
    def path_for(cls, data_dir: Path | str) -> Path:
        return Path(data_dir) / "knowledge" / cls.FILENAME

    @classmethod
    def load(cls, data_dir: Path | str) -> "LiteratureRegistry":
        path = cls.path_for(data_dir)
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return cls()
        rows = raw.get("items") if isinstance(raw, dict) else raw
        items: dict[str, LiteratureItem] = {}
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("url"):
                    item = LiteratureItem.from_dict(row)
                    items[item.id] = item
        return cls(items)

    def save(self, data_dir: Path | str) -> Path:
        path = self.path_for(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "count": len(self.items),
            "kinds": list(LITERATURE_KINDS),
            "items": [
                i.to_dict()
                for i in sorted(self.items.values(), key=lambda x: (x.kind, x.title))
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def list(
        self,
        kind: str | None = None,
        q: str | None = None,
        topic: str | None = None,
        limit: int = 100,
    ) -> list[LiteratureItem]:
        kind_l = (kind or "").strip().lower()
        q_l = (q or "").strip().lower()
        topic_l = (topic or "").strip().lower()
        out: list[LiteratureItem] = []
        for item in sorted(self.items.values(), key=lambda x: (x.kind, x.title)):
            if kind_l and item.kind != kind_l:
                continue
            if topic_l and topic_l not in [t.lower() for t in item.topics]:
                continue
            if q_l:
                blob = f"{item.title} {item.url} {item.authors or ''} {item.notes} {item.kind}".lower()
                if q_l not in blob:
                    continue
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def upsert(
        self,
        *,
        url: str,
        title: str | None = None,
        kind: str | None = None,
        authors: str | None = None,
        year: str | None = None,
        source: str | None = None,
        brand: str | None = None,
        topics: list[str] | None = None,
        access: str = "unknown",
        notes: str = "",
        confirmed: bool = False,
    ) -> LiteratureItem:
        url = (url or "").strip()
        if not url:
            raise ValueError("url jest wymagany")
        resolved_kind = kind if kind in LITERATURE_KINDS else (
            classify_literature_kind(url, title or "") or "article"
        )
        host = _host(url)
        path_name = Path(urlparse(url).path).name or host
        pid = _slug(f"{resolved_kind}-{path_name}")
        existing = None
        for item in self.items.values():
            if item.url.rstrip("/") == url.rstrip("/") or item.id == pid:
                existing = item
                break
        now = time.time()
        if existing is None:
            item = LiteratureItem(
                id=pid,
                title=(title or path_name or url)[:300],
                url=url,
                kind=resolved_kind,
                authors=authors,
                year=year,
                source=source,
                brand=brand,
                topics=list(topics or []),
                access=access or "unknown",
                notes=notes[:1000],
                found_at=now,
                updated_at=now,
                confirmed=confirmed,
            )
            self.items[item.id] = item
            return item
        if title:
            existing.title = title[:300]
        if resolved_kind:
            existing.kind = resolved_kind
        if authors:
            existing.authors = authors
        if year:
            existing.year = year
        if source:
            existing.source = source
        if brand:
            existing.brand = brand
        if topics:
            existing.topics = list(dict.fromkeys([*existing.topics, *topics]))
        if access and access != "unknown":
            existing.access = access
        if notes:
            existing.notes = notes[:1000]
        if confirmed:
            existing.confirmed = True
        existing.updated_at = now
        self.items[existing.id] = existing
        return existing

    def ingest_discovered(
        self,
        assets: list[dict[str, Any]],
        *,
        source: str | None = None,
        auto_save_dir: Path | str | None = None,
    ) -> list[LiteratureItem]:
        saved: list[LiteratureItem] = []
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            url = str(asset.get("url") or "").strip()
            title = str(asset.get("title") or asset.get("text") or "")
            atype = str(asset.get("kind") or asset.get("asset_type") or "").lower()
            if not url:
                continue
            kind = atype if atype in LITERATURE_KINDS else classify_literature_kind(url, title)
            if kind is None:
                continue
            item = self.upsert(
                url=url,
                title=title or None,
                kind=kind,
                source=source or (str(asset.get("hub") or "").strip() or None),
                topics=literature_topics(f"{title} {asset.get('summary') or ''}"),
                access="public",
                notes="auto from discover",
            )
            saved.append(item)
        if auto_save_dir is not None and saved:
            self.save(auto_save_dir)
        return saved
