from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:80] or "pricelist"


def _host(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


@dataclass
class AvailablePricelist:
    """Zarejestrowany / odkryty cennik konkurencji."""

    id: str
    title: str
    url: str
    brand: str | None = None
    source: str | None = None
    asset_type: str = "pricelist"
    access: str = "unknown"  # public | login | request | unknown
    currency: str | None = None
    year: str | None = None
    notes: str = ""
    found_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AvailablePricelist":
        return cls(
            id=str(raw.get("id") or _slug(str(raw.get("url") or "pricelist"))),
            title=str(raw.get("title") or raw.get("url") or "cennik")[:240],
            url=str(raw.get("url") or "").strip(),
            brand=(str(raw["brand"]).strip() if raw.get("brand") else None),
            source=(str(raw["source"]).strip() if raw.get("source") else None),
            asset_type=str(raw.get("asset_type") or "pricelist"),
            access=str(raw.get("access") or "unknown"),
            currency=(str(raw["currency"]).strip() if raw.get("currency") else None),
            year=(str(raw["year"]).strip() if raw.get("year") else None),
            notes=str(raw.get("notes") or "")[:1000],
            found_at=float(raw.get("found_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
            confirmed=bool(raw.get("confirmed")),
        )


class PricelistRegistry:
    """Lokalny rejestr dostępnych cenników (data/knowledge/available_pricelists.json)."""

    FILENAME = "available_pricelists.json"

    def __init__(self, items: dict[str, AvailablePricelist] | None = None) -> None:
        self.items: dict[str, AvailablePricelist] = dict(items or {})

    @classmethod
    def path_for(cls, data_dir: Path | str) -> Path:
        return Path(data_dir) / "knowledge" / cls.FILENAME

    @classmethod
    def load(cls, data_dir: Path | str) -> "PricelistRegistry":
        path = cls.path_for(data_dir)
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return cls()
        rows = raw.get("items") if isinstance(raw, dict) else raw
        items: dict[str, AvailablePricelist] = {}
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict) or not row.get("url"):
                    continue
                item = AvailablePricelist.from_dict(row)
                items[item.id] = item
        return cls(items)

    def save(self, data_dir: Path | str) -> Path:
        path = self.path_for(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "count": len(self.items),
            "items": [
                i.to_dict()
                for i in sorted(self.items.values(), key=lambda x: x.brand or x.title)
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def list(
        self,
        brand: str | None = None,
        access: str | None = None,
        q: str | None = None,
        limit: int = 100,
    ) -> list[AvailablePricelist]:
        brand_l = (brand or "").strip().lower()
        access_l = (access or "").strip().lower()
        q_l = (q or "").strip().lower()
        out: list[AvailablePricelist] = []
        for item in sorted(self.items.values(), key=lambda x: (x.brand or "", x.title)):
            if brand_l and brand_l not in (item.brand or "").lower() and brand_l not in item.title.lower():
                continue
            if access_l and item.access.lower() != access_l:
                continue
            if q_l:
                blob = f"{item.title} {item.url} {item.brand or ''} {item.notes}".lower()
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
        access: str = "unknown",
        currency: str | None = None,
        year: str | None = None,
        notes: str = "",
        confirmed: bool = False,
        asset_type: str = "pricelist",
    ) -> AvailablePricelist:
        url = (url or "").strip()
        if not url:
            raise ValueError("url jest wymagany")
        host = _host(url)
        path_name = Path(urlparse(url).path).name or host
        pid = _slug(f"{brand or host}-{path_name}")
        existing = None
        for item in self.items.values():
            if item.url.rstrip("/") == url.rstrip("/"):
                existing = item
                break
            if item.id == pid:
                existing = item
                break
        now = time.time()
        if existing is None:
            item = AvailablePricelist(
                id=pid,
                title=(title or path_name or url)[:240],
                url=url,
                brand=brand,
                source=source,
                asset_type=asset_type or "pricelist",
                access=access or "unknown",
                currency=currency,
                year=year,
                notes=notes[:1000],
                found_at=now,
                updated_at=now,
                confirmed=confirmed,
            )
            self.items[item.id] = item
            return item
        if title:
            existing.title = title[:240]
        if brand:
            existing.brand = brand
        if source:
            existing.source = source
        if access and access != "unknown":
            existing.access = access
        if currency:
            existing.currency = currency
        if year:
            existing.year = year
        if notes:
            existing.notes = notes[:1000]
        if confirmed:
            existing.confirmed = True
        existing.asset_type = asset_type or existing.asset_type
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
    ) -> list[AvailablePricelist]:
        saved: list[AvailablePricelist] = []
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            atype = str(asset.get("asset_type") or "").lower()
            url = str(asset.get("url") or "").strip()
            text = str(asset.get("text") or "")
            if not url:
                continue
            if atype != "pricelist" and "pricelist" not in atype:
                continue
            item = self.upsert(
                url=url,
                title=text or None,
                brand=brand or (str(asset.get("brand")).strip() if asset.get("brand") else None),
                source=source or (str(asset.get("hub") or asset.get("source_name") or "").strip() or None),
                access="public",
                notes="auto from discover",
            )
            saved.append(item)
        if auto_save_dir is not None and saved:
            self.save(auto_save_dir)
        return saved
