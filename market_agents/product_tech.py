from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

TECH_KINDS = (
    "cutting_data",
    "handbook",
    "application_guide",
    "iso13399",
    "tech_datasheet",
    "grade_chart",
)

_PARAM_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("vc", re.compile(r"\b(v[_ ]?c|cutting\s*speed|schnittgeschwindigkeit|pr[eę]dko[sś][cć]\s*skrawania)\b", re.I)),
    ("fz", re.compile(r"\b(f[_ ]?z|feed\s*per\s*tooth|zahnvorschub|posuw\s*na\s*ostrze)\b", re.I)),
    ("fn", re.compile(r"\b(f[_ ]?n|feed\s*per\s*rev(?:olution)?|vorschub\s*pro\s*umdrehung|posuw\s*na\s*obr[oó]t)\b", re.I)),
    ("ap", re.compile(r"\b(a[_ ]?p|depth\s*of\s*cut|schnittiefe|g[lł][eę]boko[sś][cć]\s*skrawania)\b", re.I)),
    ("ae", re.compile(r"\b(a[_ ]?e|width\s*of\s*cut|schnittbreite|szeroko[sś][cć]\s*skrawania)\b", re.I)),
    ("n", re.compile(r"\b(rpm|spindle\s*speed|drehzahl|obr\.?\s*/\s*min|obr[oó]t[yó]w)\b", re.I)),
    ("vf", re.compile(r"\b(v[_ ]?f|table\s*feed|vorschubgeschwindigkeit|posuw\s*stolika)\b", re.I)),
    ("kc", re.compile(r"\b(k[_ ]?c1?|specific\s*cutting\s*force|spezifische\s*schnittkraft)\b", re.I)),
]

_MATERIAL_GROUPS = re.compile(
    r"\b(?:ISO\s*)?(?:material\s*group|werkstoffgruppe|grupa\s*materia[lł]owa)?\s*[PMKNSH]\b|"
    r"\b(?:P01|P10|P20|P30|P40|M10|M20|M30|K10|K20|K30)\b",
    re.I,
)
_ISO13399 = re.compile(r"\bISO\s*/?\s*TS?\s*13399\b|\bDIN\s*4000\b|\bGTC\b", re.I)
_GRADE_HINTS = re.compile(
    r"\b(?:grade|sorte|gatunek|coating|beschichtung|pow[lł]oka|CVD|PVD|carbide|HM|HSS|CBN|PKD|PCD|ceramic)\b",
    re.I,
)
_COOLANT_HINTS = re.compile(
    r"\b(?:coolant|cooling|k[uü]hlung|ch[lł]odziwo|MQL|dry\s*machining|trocken|nass|emulsion)\b",
    re.I,
)
_OPERATION_HINTS = re.compile(
    r"\b(?:turning|milling|drilling|boring|threading|reaming|parting|"
    r"toczenie|frezowanie|wiercenie|gwintowanie|drehen|fr[aä]sen|bohren|gewinde)\b",
    re.I,
)
_CHAPTER_HINTS = re.compile(
    r"(?m)^\s*(?:\d+[\.\)]\s+|[A-Z][\d]*[\.\)]\s+|Chapter\s+\d+|Rozdzia[lł]\s+\d+|Kapitel\s+\d+).{8,80}$"
)
_TECH_ASSET_HINTS = re.compile(
    r"(cutting\s*data|schnittwerte|parametr(?:y)?\s*skrawania|"
    r"handbook|technical\s*(?:guide|manual|data|datasheet|reference)|"
    r"application\s*guide|anwendungs|iso[\s_-]?13399|din[\s_-]?4000|"
    r"grade\s*(?:chart|overview)|datenblatt|karta\s*technicz|"
    r"feeds?\s*(?:and|&)\s*speeds?|metal\s*cutting\s*knowledge)",
    re.I,
)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:80] or "product-tech"


def _host(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    return host[4:] if host.startswith("www.") else host


def classify_tech_kind(url: str, text: str = "") -> str | None:
    """Zwróć rodzaj dokumentu technicznego albo None."""
    blob = f"{url} {text}".lower()
    if re.search(r"iso[\s_-]?13399|din[\s_-]?4000|\bgtc\b|tool\s*data\s*file", blob):
        return "iso13399"
    if re.search(
        r"cutting\s*data|cutting\s*parameter|schnittwerte|"
        r"parametr(?:y)?\s*skrawania|recommended\s*cutting|feeds?\s*(?:and|&)\s*speeds?",
        blob,
    ):
        return "cutting_data"
    if re.search(
        r"application\s*guide|anwendungs(?:hinweis|guide)|przewodnik\s*zastosowa|"
        r"troubleshooting|wear\s*guide|tool\s*deterioration",
        blob,
    ):
        return "application_guide"
    if re.search(r"grade\s*(?:chart|overview|comparison)|sorten[uü]bersicht|por[oó]wnanie\s*gatunk", blob):
        return "grade_chart"
    if re.search(
        r"handbook|technical\s*(?:guide|manual|book|reference)|metal\s*cutting\s*(?:knowledge|training)|"
        r"technische[sr]?\s*(?:handbuch|leitfaden|informationen)|"
        r"poradnik\s*obr[oó]bki|user'?s?\s*guide",
        blob,
    ):
        return "handbook"
    if re.search(
        r"technical\s*(?:data|datasheet|info|information)|datenblatt|karta\s*technicz|"
        r"product\s*data\s*sheet|spec\s*sheet",
        blob,
    ):
        return "tech_datasheet"
    if _TECH_ASSET_HINTS.search(blob):
        return "tech_datasheet"
    return None


def extract_tech_schema(text: str, *, max_chapters: int = 20) -> dict[str, Any]:
    """
    Wyodrębnij SCHEMAT pól technicznych z tekstu handbooka / cutting data.

    Polityka TIZ: studiujemy układ rozdziałów i pól (vc/fz/ap/ae, grupy materiałowe,
    chłodzenie). Nie kopiujemy tabel wartości do CutData.
    """
    raw = text or ""
    fields_present = [name for name, pat in _PARAM_PATTERNS if pat.search(raw)]
    material_hits = sorted({m.group(0).upper() for m in _MATERIAL_GROUPS.finditer(raw)})[:40]
    operations = sorted({m.group(0).lower() for m in _OPERATION_HINTS.finditer(raw)})[:30]
    chapters: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if _CHAPTER_HINTS.match(line) and len(line) < 120:
            chapters.append(line)
            if len(chapters) >= max_chapters:
                break
    if not chapters:
        for line in raw.splitlines():
            low = line.strip()
            if 12 <= len(low) <= 90 and re.search(
                r"turning|milling|drilling|toczenie|frezowanie|wiercenie|"
                r"cutting data|technical|grade|coating|coolant",
                low,
                re.I,
            ):
                chapters.append(low)
                if len(chapters) >= max_chapters:
                    break
    return {
        "ok": True,
        "purpose": "schema_only — układ pól do nauki kalkulatora TIZ; nie kopiować tabel vc/fz do CutData",
        "fields_present": fields_present,
        "field_count": len(fields_present),
        "material_groups_mentioned": material_hits,
        "operations_mentioned": operations,
        "has_iso13399": bool(_ISO13399.search(raw)),
        "has_grade_info": bool(_GRADE_HINTS.search(raw)),
        "has_coolant_info": bool(_COOLANT_HINTS.search(raw)),
        "chapter_headings": chapters,
        "chars": len(raw),
        "richness_0_to_1": round(
            min(
                1.0,
                (len(fields_present) / 8) * 0.5
                + (0.2 if material_hits else 0)
                + (0.15 if chapters else 0)
                + (0.1 if _ISO13399.search(raw) else 0)
                + (0.05 if _COOLANT_HINTS.search(raw) else 0),
            ),
            3,
        ),
    }


@dataclass
class ProductTechDoc:
    """Źródło informacji technicznej o produktach konkurencji."""

    id: str
    title: str
    url: str
    brand: str | None = None
    source: str | None = None
    kind: str = "tech_datasheet"
    access: str = "unknown"
    year: str | None = None
    notes: str = ""
    schema_summary: dict[str, Any] = field(default_factory=dict)
    found_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProductTechDoc":
        kind = str(raw.get("kind") or "tech_datasheet")
        if kind not in TECH_KINDS:
            kind = "tech_datasheet"
        return cls(
            id=str(raw.get("id") or _slug(str(raw.get("url") or "product-tech"))),
            title=str(raw.get("title") or raw.get("url") or "tech")[:240],
            url=str(raw.get("url") or "").strip(),
            brand=(str(raw["brand"]).strip() if raw.get("brand") else None),
            source=(str(raw["source"]).strip() if raw.get("source") else None),
            kind=kind,
            access=str(raw.get("access") or "unknown"),
            year=(str(raw["year"]).strip() if raw.get("year") else None),
            notes=str(raw.get("notes") or "")[:1000],
            schema_summary=dict(raw.get("schema_summary") or {}),
            found_at=float(raw.get("found_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
            confirmed=bool(raw.get("confirmed")),
        )


class ProductTechRegistry:
    """Rejestr źródeł tech produktów (data/knowledge/product_tech.json)."""

    FILENAME = "product_tech.json"

    def __init__(self, items: dict[str, ProductTechDoc] | None = None) -> None:
        self.items: dict[str, ProductTechDoc] = dict(items or {})

    @classmethod
    def path_for(cls, data_dir: Path | str) -> Path:
        return Path(data_dir) / "knowledge" / cls.FILENAME

    @classmethod
    def load(cls, data_dir: Path | str) -> "ProductTechRegistry":
        path = cls.path_for(data_dir)
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return cls()
        rows = raw.get("items") if isinstance(raw, dict) else raw
        items: dict[str, ProductTechDoc] = {}
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("url"):
                    doc = ProductTechDoc.from_dict(row)
                    items[doc.id] = doc
        return cls(items)

    def save(self, data_dir: Path | str) -> Path:
        path = self.path_for(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "count": len(self.items),
            "policy": (
                "Schema/layout only for TIZ calculator research. "
                "Do not copy competitor vc/fz tables into CutData."
            ),
            "items": [
                i.to_dict()
                for i in sorted(self.items.values(), key=lambda x: (x.brand or "", x.title))
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def list(
        self,
        brand: str | None = None,
        kind: str | None = None,
        access: str | None = None,
        q: str | None = None,
        limit: int = 100,
    ) -> list[ProductTechDoc]:
        brand_l = (brand or "").strip().lower()
        kind_l = (kind or "").strip().lower()
        access_l = (access or "").strip().lower()
        q_l = (q or "").strip().lower()
        out: list[ProductTechDoc] = []
        for item in sorted(self.items.values(), key=lambda x: (x.brand or "", x.title)):
            if brand_l and brand_l not in (item.brand or "").lower() and brand_l not in item.title.lower():
                continue
            if kind_l and item.kind.lower() != kind_l:
                continue
            if access_l and item.access.lower() != access_l:
                continue
            if q_l:
                blob = f"{item.title} {item.url} {item.brand or ''} {item.notes} {item.kind}".lower()
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
        brand: str | None = None,
        source: str | None = None,
        kind: str = "tech_datasheet",
        access: str = "unknown",
        year: str | None = None,
        notes: str = "",
        schema_summary: dict[str, Any] | None = None,
        confirmed: bool = False,
    ) -> ProductTechDoc:
        url = (url or "").strip()
        if not url:
            raise ValueError("url jest wymagany")
        if kind not in TECH_KINDS:
            kind = classify_tech_kind(url, title or "") or "tech_datasheet"
        host = _host(url)
        path_name = Path(urlparse(url).path).name or host
        pid = _slug(f"{brand or host}-{path_name}")
        existing = None
        for item in self.items.values():
            if item.url.rstrip("/") == url.rstrip("/") or item.id == pid:
                existing = item
                break
        now = time.time()
        if existing is None:
            doc = ProductTechDoc(
                id=pid,
                title=(title or path_name or url)[:240],
                url=url,
                brand=brand,
                source=source,
                kind=kind,
                access=access or "unknown",
                year=year,
                notes=notes[:1000],
                schema_summary=dict(schema_summary or {}),
                found_at=now,
                updated_at=now,
                confirmed=confirmed,
            )
            self.items[doc.id] = doc
            return doc
        if title:
            existing.title = title[:240]
        if brand:
            existing.brand = brand
        if source:
            existing.source = source
        if kind:
            existing.kind = kind
        if access and access != "unknown":
            existing.access = access
        if year:
            existing.year = year
        if notes:
            existing.notes = notes[:1000]
        if schema_summary:
            existing.schema_summary = dict(schema_summary)
        if confirmed:
            existing.confirmed = True
        existing.updated_at = now
        self.items[existing.id] = existing
        return existing

    def ingest_discovered(
        self,
        assets: list[dict[str, Any]],
        *,
        brand: str | None = None,
        source: str | None = None,
        auto_save_dir: Path | str | None = None,
    ) -> list[ProductTechDoc]:
        saved: list[ProductTechDoc] = []
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            atype = str(asset.get("asset_type") or "").lower()
            url = str(asset.get("url") or "").strip()
            text = str(asset.get("text") or "")
            if not url:
                continue
            kind = atype if atype in TECH_KINDS else classify_tech_kind(url, text)
            if kind is None:
                continue
            doc = self.upsert(
                url=url,
                title=text or None,
                brand=brand or (str(asset.get("brand")).strip() if asset.get("brand") else None),
                source=source
                or (str(asset.get("hub") or asset.get("source_name") or "").strip() or None),
                kind=kind,
                access="public",
                notes="auto from discover",
            )
            saved.append(doc)
        if auto_save_dir is not None and saved:
            self.save(auto_save_dir)
        return saved
