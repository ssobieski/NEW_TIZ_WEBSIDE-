from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PARSE_METHODS = ("trafilatura", "bs4", "css", "auto")


@dataclass
class SiteParseRule:
    """Nauczona / ręczna reguła parsowania dla hosta."""

    host: str
    preferred_method: str = "auto"  # trafilatura | bs4 | css | auto
    css_selector: str = ""
    drop_selectors: list[str] = field(default_factory=list)
    min_chars: int = 120
    success_count: int = 0
    fail_count: int = 0
    last_ok_url: str = ""
    notes: str = ""
    origin: str = "agent"  # agent | manual | auto

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SiteParseRule":
        method = str(raw.get("preferred_method") or "auto").strip().lower()
        if method not in PARSE_METHODS:
            method = "auto"
        drops = raw.get("drop_selectors") or []
        if not isinstance(drops, list):
            drops = []
        return cls(
            host=str(raw.get("host") or "").strip().lower(),
            preferred_method=method,
            css_selector=str(raw.get("css_selector") or "").strip(),
            drop_selectors=[str(x).strip() for x in drops if str(x).strip()],
            min_chars=int(raw.get("min_chars") or 120),
            success_count=int(raw.get("success_count") or 0),
            fail_count=int(raw.get("fail_count") or 0),
            last_ok_url=str(raw.get("last_ok_url") or ""),
            notes=str(raw.get("notes") or "")[:500],
            origin=str(raw.get("origin") or "agent"),
        )


def host_from_url(url: str) -> str:
    host = (urlparse(url).netloc or "").lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


class SiteParseRulesStore:
    """
    Trwała pamięć strategii parsowania per host.
    Agent rozwija parser: po słabym/dobrym parse zapisuje metodę / CSS.
    """

    def __init__(self, rules: dict[str, SiteParseRule] | None = None) -> None:
        self.rules: dict[str, SiteParseRule] = dict(rules or {})

    @classmethod
    def load(
        cls,
        data_dir: Path | str | None = None,
        path: Path | str | None = None,
    ) -> "SiteParseRulesStore":
        data_dir = Path(data_dir or "data")
        store_path = Path(path) if path else data_dir / "knowledge" / "site_parse_rules.json"
        if not store_path.exists():
            return cls()
        try:
            payload = json.loads(store_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return cls()
        rows = payload.get("rules", payload if isinstance(payload, list) else [])
        rules: dict[str, SiteParseRule] = {}
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                rule = SiteParseRule.from_dict(row)
                if rule.host:
                    rules[rule.host] = rule
        elif isinstance(rows, dict):
            for host, row in rows.items():
                if isinstance(row, dict):
                    row = {**row, "host": row.get("host") or host}
                    rule = SiteParseRule.from_dict(row)
                    if rule.host:
                        rules[rule.host] = rule
        return cls(rules)

    def save(self, data_dir: Path | str | None = None) -> Path:
        data_dir = Path(data_dir or "data")
        out = data_dir / "knowledge" / "site_parse_rules.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "count": len(self.rules),
            "methods": list(PARSE_METHODS),
            "rules": [r.to_dict() for r in sorted(self.rules.values(), key=lambda x: x.host)],
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    def get(self, host_or_url: str) -> SiteParseRule | None:
        host = host_from_url(host_or_url) if "://" in host_or_url else host_or_url.lower().strip()
        if host.startswith("www."):
            host = host[4:]
        if host in self.rules:
            return self.rules[host]
        parts = host.split(".")
        if len(parts) > 2:
            parent = ".".join(parts[-2:])
            return self.rules.get(parent)
        return None

    def list_rules(self, query: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        rows = [r.to_dict() for r in self.rules.values()]
        if query:
            q = query.lower().strip()
            rows = [
                r
                for r in rows
                if q in r["host"]
                or q in (r.get("notes") or "").lower()
                or q in (r.get("css_selector") or "").lower()
            ]
        rows.sort(key=lambda r: (-int(r.get("success_count") or 0), r.get("host") or ""))
        return rows[:limit]

    def upsert(
        self,
        host_or_url: str,
        *,
        preferred_method: str = "auto",
        css_selector: str = "",
        drop_selectors: list[str] | None = None,
        min_chars: int = 120,
        notes: str = "",
        origin: str = "agent",
        last_ok_url: str = "",
    ) -> dict[str, Any]:
        host = host_from_url(host_or_url) if "://" in host_or_url else host_or_url.lower().strip()
        if host.startswith("www."):
            host = host[4:]
        if not host:
            raise ValueError("host wymagany (URL lub nazwa hosta)")
        method = (preferred_method or "auto").strip().lower()
        if method not in PARSE_METHODS:
            raise ValueError(f"Nieznana metoda={method}. Dozwolone: {', '.join(PARSE_METHODS)}")
        existing = self.rules.get(host)
        rule = SiteParseRule(
            host=host,
            preferred_method=method,
            css_selector=css_selector if css_selector else (existing.css_selector if existing else ""),
            drop_selectors=list(drop_selectors)
            if drop_selectors is not None
            else (list(existing.drop_selectors) if existing else []),
            min_chars=int(min_chars),
            success_count=existing.success_count if existing else 0,
            fail_count=existing.fail_count if existing else 0,
            last_ok_url=last_ok_url or (existing.last_ok_url if existing else ""),
            notes=notes or (existing.notes if existing else ""),
            origin=origin,
        )
        self.rules[host] = rule
        return {"ok": True, "upserted": True, "rule": rule.to_dict()}

    def rate(
        self,
        url: str,
        *,
        quality_0_to_1: float,
        parse_method: str | None = None,
        css_selector: str | None = None,
        notes: str = "",
        success_threshold: float = 0.55,
    ) -> dict[str, Any]:
        """Zapisz feedback jakości parse → wzmocnij lub osłab regułę hosta."""
        host = host_from_url(url)
        if not host:
            return {"ok": False, "error": "nie udało się wyciągnąć hosta z URL"}
        rule = self.rules.get(host) or SiteParseRule(host=host, origin="auto")
        score = max(0.0, min(1.0, float(quality_0_to_1)))
        if parse_method and parse_method in PARSE_METHODS and parse_method != "auto":
            rule.preferred_method = parse_method
        if css_selector is not None and str(css_selector).strip():
            rule.css_selector = str(css_selector).strip()
            if rule.preferred_method == "auto":
                rule.preferred_method = "css"
        if notes:
            rule.notes = (rule.notes + " | " + notes).strip(" |")[:500]
        if score >= success_threshold:
            rule.success_count += 1
            rule.last_ok_url = url
        else:
            rule.fail_count += 1
        self.rules[host] = rule
        return {
            "ok": True,
            "rated": True,
            "quality": score,
            "rule": rule.to_dict(),
        }
