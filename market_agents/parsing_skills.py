from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from market_agents.parse_rules import PARSE_METHODS

SKILL_CATEGORIES = (
    "article",
    "exhibitor_list",
    "catalog_hub",
    "pdf",
    "firm_extract",
    "relation_extract",
    "media_hub",
    "generic",
)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:80] or "skill"


def _host(url_or_host: str) -> str:
    raw = (url_or_host or "").strip().lower()
    if "://" in raw:
        host = (urlparse(raw).netloc or "").lower()
    else:
        host = raw
    if host.startswith("www."):
        host = host[4:]
    return host


def _str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def _merge_unique(base: list[str], extra: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in list(base or []) + list(extra or []):
        key = item.strip()
        if not key:
            continue
        low = key.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(key)
    return out


@dataclass
class ParsingSkill:
    """
    Współdzielona umiejętność parsowania — przepis wielokrotnego użytku
    (targi, artykuły, katalogi, firmy…). Wszystkie agenty czytają i ulepszają
    tę samą bazę w data/knowledge/parsing_skills.json.
    """

    id: str
    name: str
    category: str = "generic"
    description: str = ""
    preferred_method: str = "auto"
    css_selectors: list[str] = field(default_factory=list)
    drop_selectors: list[str] = field(default_factory=list)
    link_keywords: list[str] = field(default_factory=list)
    extract_patterns: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    applies_to_hosts: list[str] = field(default_factory=list)
    applies_to_kinds: list[str] = field(default_factory=list)
    url_contains: list[str] = field(default_factory=list)
    version: int = 1
    success_count: int = 0
    fail_count: int = 0
    quality_sum: float = 0.0
    taught_by: str = "agent"
    shared: bool = True
    parent_skill_id: str | None = None
    notes: str = ""
    examples: list[str] = field(default_factory=list)

    @property
    def avg_quality(self) -> float:
        n = self.success_count + self.fail_count
        if n <= 0:
            return 0.0
        return round(self.quality_sum / n, 3)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["avg_quality"] = self.avg_quality
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ParsingSkill":
        method = str(raw.get("preferred_method") or "auto").strip().lower()
        if method not in PARSE_METHODS:
            method = "auto"
        category = str(raw.get("category") or "generic").strip().lower()
        if category not in SKILL_CATEGORIES:
            category = "generic"
        skill_id = str(raw.get("id") or "").strip() or _slug(str(raw.get("name") or "skill"))
        return cls(
            id=skill_id,
            name=str(raw.get("name") or skill_id),
            category=category,
            description=str(raw.get("description") or "")[:800],
            preferred_method=method,
            css_selectors=_str_list(raw.get("css_selectors")),
            drop_selectors=_str_list(raw.get("drop_selectors")),
            link_keywords=_str_list(raw.get("link_keywords")),
            extract_patterns=_str_list(raw.get("extract_patterns")),
            hints=_str_list(raw.get("hints")),
            applies_to_hosts=[_host(h) for h in _str_list(raw.get("applies_to_hosts")) if _host(h)],
            applies_to_kinds=_str_list(raw.get("applies_to_kinds")),
            url_contains=_str_list(raw.get("url_contains")),
            version=max(1, int(raw.get("version") or 1)),
            success_count=int(raw.get("success_count") or 0),
            fail_count=int(raw.get("fail_count") or 0),
            quality_sum=float(raw.get("quality_sum") or 0.0),
            taught_by=str(raw.get("taught_by") or "agent")[:80],
            shared=bool(raw.get("shared", True)),
            parent_skill_id=str(raw.get("parent_skill_id") or "") or None,
            notes=str(raw.get("notes") or "")[:500],
            examples=_str_list(raw.get("examples"))[:12],
        )


def default_seed_skills() -> list[ParsingSkill]:
    """Startowe umiejętności TIZ — wspólne dla wszystkich agentów."""
    return [
        ParsingSkill(
            id="fair-exhibitor-list",
            name="Lista wystawców targów",
            category="exhibitor_list",
            description="Parsowanie list wystawców EMO/AMB/IMTS i podobnych.",
            preferred_method="bs4",
            css_selectors=[
                ".exhibitor",
                ".aussteller",
                "table.exhibitors",
                "ul.exhibitors li",
                "a[href*='exhibitor']",
            ],
            link_keywords=["exhibitor", "aussteller", "wystawc", "booth", "stand", "hall"],
            extract_patterns=[r"\b[A-Z][\w&./+-]+(?:\s+[A-Z][\w&./+-]+){0,4}\b"],
            hints=[
                "Najpierw discover_media_links z kind=trade_fair",
                "Potem extract_fair_exhibitors + check_firm_known",
                "Nowe nazwy → discover_new_firms / extract_market_intel signal_type=new_firm",
            ],
            applies_to_kinds=["trade_fair"],
            url_contains=["exhibitor", "aussteller", "wystawc"],
            taught_by="seed",
            examples=["https://emo-hannover.com/", "https://www.imts.com/"],
        ),
        ParsingSkill(
            id="magazine-article",
            name="Artykuł czasopisma branżowego",
            category="article",
            description="Treść artykułów CTE / MMS / MM Maschinenmarkt.",
            preferred_method="trafilatura",
            css_selectors=["article", "main .content", ".article-body", ".story-body"],
            drop_selectors=["nav", "footer", ".related", ".newsletter", ".paywall"],
            link_keywords=["article", "news", "feature", "report", "interview"],
            hints=[
                "Użyj parse_media_page lub fetch_and_parse",
                "Po tekście: extract firm candidates + discover_relations",
                "Słaby wynik → improve_parsing_skill z nowym css_selector",
            ],
            applies_to_kinds=["magazine", "portal"],
            url_contains=["/news/", "/article/", "/artikel/"],
            taught_by="seed",
        ),
        ParsingSkill(
            id="catalog-hub-assets",
            name="Hub e-katalogu / downloads",
            category="catalog_hub",
            description="Odkrywanie PDF i digital catalogue na stronach producentów.",
            preferred_method="bs4",
            link_keywords=["catalog", "catalogue", "download", "pdf", "ecatalog", "brochure"],
            hints=[
                "list_catalog_sources → discover_catalog_assets → fetch_pdf_text",
                "Szukaj linków .pdf oraz 'digital catalogue' / 'e-catalog'",
            ],
            applies_to_kinds=["ecatalog", "digital_catalogue", "publication"],
            url_contains=["download", "catalog", "catalogue"],
            taught_by="seed",
        ),
        ParsingSkill(
            id="firm-name-extract",
            name="Ekstrakcja nazw firm z tekstu",
            category="firm_extract",
            description="Heurystyki nazw producentów narzędzi skrawających.",
            preferred_method="auto",
            extract_patterns=[
                r"\b([A-Z][\w&./+-]+(?:\s+[A-Z][\w&./+-]+){0,4})\s+"
                r"(?:GmbH|AG|Inc|Ltd|LLC|Co\.,?\s*Ltd\.?)\b",
                r"\b(?:new brand|startup|exhibitor|manufacturer)\s+"
                r"([A-Z][\w&./+-]+(?:\s+[A-Z][\w&./+-]+){0,3})\b",
            ],
            hints=[
                "Po ekstrakcji zawsze check_firm_known",
                "Nie zgaduj profilu znanej marki — get_firm_presentation z Notion",
            ],
            applies_to_kinds=["trade_fair", "magazine", "portal", "news"],
            taught_by="seed",
        ),
        ParsingSkill(
            id="relation-phrases",
            name="Frazy powiązań firm",
            category="relation_extract",
            description="Dystrybutor / dealer / brand / OEM group z tekstów.",
            preferred_method="auto",
            extract_patterns=[
                r"(?i)([\w .&+-]+)\s+(?:is\s+)?(?:a\s+)?distributor\s+of\s+([\w .&+-]+)",
                r"(?i)([\w .&+-]+)\s+(?:authorized\s+)?dealer\s+(?:for|of)\s+([\w .&+-]+)",
                r"(?i)([\w .&+-]+)\s+(?:brand|private label)\s+of\s+([\w .&+-]+)",
            ],
            hints=[
                "discover_relations(parse_candidates=true) → add_relation po weryfikacji",
                "Typy: distributor_of, dealer_of, brand_of, subsidiary_of, oem_group",
            ],
            applies_to_kinds=["magazine", "portal", "catalog", "news"],
            taught_by="seed",
        ),
    ]


class ParsingSkillsStore:
    """
    Wspólna, trwała baza umiejętności parsowania.
    Collector / ParsingAgent / Analyst korzystają z tej samej wiedzy i mogą ją ulepszać.
    """

    def __init__(self, skills: dict[str, ParsingSkill] | None = None) -> None:
        self.skills: dict[str, ParsingSkill] = dict(skills or {})

    @classmethod
    def load(
        cls,
        data_dir: Path | str | None = None,
        path: Path | str | None = None,
        *,
        with_seeds: bool = True,
    ) -> "ParsingSkillsStore":
        data_dir = Path(data_dir or "data")
        store_path = Path(path) if path else data_dir / "knowledge" / "parsing_skills.json"
        store = cls()
        if with_seeds:
            for skill in default_seed_skills():
                store.skills[skill.id] = skill
        if not store_path.exists():
            return store
        try:
            payload = json.loads(store_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return store
        rows = payload.get("skills", payload if isinstance(payload, list) else [])
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                skill = ParsingSkill.from_dict(row)
                store.skills[skill.id] = skill
        elif isinstance(rows, dict):
            for sid, row in rows.items():
                if isinstance(row, dict):
                    row = {**row, "id": row.get("id") or sid}
                    skill = ParsingSkill.from_dict(row)
                    store.skills[skill.id] = skill
        return store

    def save(self, data_dir: Path | str | None = None) -> Path:
        data_dir = Path(data_dir or "data")
        out = data_dir / "knowledge" / "parsing_skills.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        shared = [s for s in self.skills.values() if s.shared]
        payload = {
            "count": len(shared),
            "categories": list(SKILL_CATEGORIES),
            "skills": [s.to_dict() for s in sorted(shared, key=lambda x: (x.category, x.id))],
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    def get(self, skill_id: str) -> ParsingSkill | None:
        key = (skill_id or "").strip()
        return self.skills.get(key) or self.skills.get(key.lower())

    def list_skills(
        self,
        query: str | None = None,
        category: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = [s.to_dict() for s in self.skills.values() if s.shared]
        if category:
            cat = category.strip().lower()
            rows = [r for r in rows if r.get("category") == cat]
        if query:
            q = query.lower().strip()
            rows = [
                r
                for r in rows
                if q in (r.get("id") or "")
                or q in (r.get("name") or "").lower()
                or q in (r.get("description") or "").lower()
                or q in (r.get("notes") or "").lower()
                or any(q in h.lower() for h in (r.get("hints") or []))
            ]
        rows.sort(
            key=lambda r: (
                -float(r.get("avg_quality") or 0),
                -int(r.get("success_count") or 0),
                r.get("id") or "",
            )
        )
        return rows[:limit]

    def match(
        self,
        url: str | None = None,
        kind: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        host = _host(url or "")
        url_l = (url or "").lower()
        kind_l = (kind or "").strip().lower()
        scored: list[tuple[float, ParsingSkill]] = []
        for skill in self.skills.values():
            if not skill.shared:
                continue
            score = 0.0
            if kind_l and kind_l in [k.lower() for k in skill.applies_to_kinds]:
                score += 3.0
            if host and host in skill.applies_to_hosts:
                score += 4.0
            elif host and any(host.endswith(h) for h in skill.applies_to_hosts):
                score += 2.5
            for frag in skill.url_contains:
                if frag.lower() in url_l:
                    score += 1.5
            score += 0.1 * skill.avg_quality
            score += 0.02 * skill.success_count
            if score > 0:
                scored.append((score, skill))
        scored.sort(key=lambda x: (-x[0], x[1].id))
        return [
            {**skill.to_dict(), "match_score": round(score, 3)}
            for score, skill in scored[:limit]
        ]

    def learn(
        self,
        *,
        name: str,
        category: str = "generic",
        description: str = "",
        preferred_method: str = "auto",
        css_selectors: list[str] | None = None,
        drop_selectors: list[str] | None = None,
        link_keywords: list[str] | None = None,
        extract_patterns: list[str] | None = None,
        hints: list[str] | None = None,
        applies_to_hosts: list[str] | None = None,
        applies_to_kinds: list[str] | None = None,
        url_contains: list[str] | None = None,
        taught_by: str = "agent",
        skill_id: str | None = None,
        notes: str = "",
        examples: list[str] | None = None,
        merge: bool = True,
    ) -> dict[str, Any]:
        sid = (skill_id or _slug(name)).strip().lower()
        existing = self.skills.get(sid)
        method = (preferred_method or "auto").strip().lower()
        if method not in PARSE_METHODS:
            raise ValueError(f"Nieznana metoda={method}. Dozwolone: {', '.join(PARSE_METHODS)}")
        cat = (category or "generic").strip().lower()
        if cat not in SKILL_CATEGORIES:
            raise ValueError(f"Nieznana kategoria={cat}. Dozwolone: {', '.join(SKILL_CATEGORIES)}")

        if existing and merge:
            skill = ParsingSkill(
                id=sid,
                name=name or existing.name,
                category=cat or existing.category,
                description=description or existing.description,
                preferred_method=method if method != "auto" else existing.preferred_method,
                css_selectors=_merge_unique(existing.css_selectors, css_selectors),
                drop_selectors=_merge_unique(existing.drop_selectors, drop_selectors),
                link_keywords=_merge_unique(existing.link_keywords, link_keywords),
                extract_patterns=_merge_unique(existing.extract_patterns, extract_patterns),
                hints=_merge_unique(existing.hints, hints),
                applies_to_hosts=_merge_unique(
                    existing.applies_to_hosts,
                    [_host(h) for h in (applies_to_hosts or []) if _host(h)],
                ),
                applies_to_kinds=_merge_unique(existing.applies_to_kinds, applies_to_kinds),
                url_contains=_merge_unique(existing.url_contains, url_contains),
                version=existing.version,
                success_count=existing.success_count,
                fail_count=existing.fail_count,
                quality_sum=existing.quality_sum,
                taught_by=taught_by or existing.taught_by,
                shared=True,
                parent_skill_id=existing.parent_skill_id,
                notes=notes or existing.notes,
                examples=_merge_unique(existing.examples, examples)[:12],
            )
            action = "merged"
        else:
            skill = ParsingSkill(
                id=sid,
                name=name,
                category=cat,
                description=description,
                preferred_method=method,
                css_selectors=list(css_selectors or []),
                drop_selectors=list(drop_selectors or []),
                link_keywords=list(link_keywords or []),
                extract_patterns=list(extract_patterns or []),
                hints=list(hints or []),
                applies_to_hosts=[_host(h) for h in (applies_to_hosts or []) if _host(h)],
                applies_to_kinds=list(applies_to_kinds or []),
                url_contains=list(url_contains or []),
                taught_by=taught_by,
                shared=True,
                notes=notes,
                examples=list(examples or [])[:12],
                parent_skill_id=existing.id if existing else None,
                version=(existing.version + 1) if existing else 1,
                success_count=existing.success_count if existing else 0,
                fail_count=existing.fail_count if existing else 0,
                quality_sum=existing.quality_sum if existing else 0.0,
            )
            action = "created" if not existing else "replaced"

        self.skills[sid] = skill
        return {"ok": True, "action": action, "skill": skill.to_dict()}

    def improve(
        self,
        skill_id: str,
        *,
        preferred_method: str | None = None,
        css_selectors: list[str] | None = None,
        drop_selectors: list[str] | None = None,
        link_keywords: list[str] | None = None,
        extract_patterns: list[str] | None = None,
        hints: list[str] | None = None,
        applies_to_hosts: list[str] | None = None,
        applies_to_kinds: list[str] | None = None,
        url_contains: list[str] | None = None,
        notes: str = "",
        taught_by: str = "agent",
        bump_version: bool = True,
    ) -> dict[str, Any]:
        skill = self.get(skill_id)
        if skill is None:
            return {"ok": False, "error": f"unknown skill: {skill_id}"}
        if preferred_method:
            method = preferred_method.strip().lower()
            if method not in PARSE_METHODS:
                return {"ok": False, "error": f"Nieznana metoda={method}"}
            skill.preferred_method = method
        if css_selectors is not None:
            skill.css_selectors = _merge_unique(skill.css_selectors, css_selectors)
        if drop_selectors is not None:
            skill.drop_selectors = _merge_unique(skill.drop_selectors, drop_selectors)
        if link_keywords is not None:
            skill.link_keywords = _merge_unique(skill.link_keywords, link_keywords)
        if extract_patterns is not None:
            skill.extract_patterns = _merge_unique(skill.extract_patterns, extract_patterns)
        if hints is not None:
            skill.hints = _merge_unique(skill.hints, hints)
        if applies_to_hosts is not None:
            skill.applies_to_hosts = _merge_unique(
                skill.applies_to_hosts,
                [_host(h) for h in applies_to_hosts if _host(h)],
            )
        if applies_to_kinds is not None:
            skill.applies_to_kinds = _merge_unique(skill.applies_to_kinds, applies_to_kinds)
        if url_contains is not None:
            skill.url_contains = _merge_unique(skill.url_contains, url_contains)
        if notes:
            skill.notes = (skill.notes + " | " + notes).strip(" |")[:500]
        skill.taught_by = taught_by or skill.taught_by
        skill.shared = True
        if bump_version:
            skill.version += 1
        self.skills[skill.id] = skill
        return {"ok": True, "improved": True, "skill": skill.to_dict()}

    def rate(
        self,
        skill_id: str,
        *,
        quality_0_to_1: float,
        notes: str = "",
        success_threshold: float = 0.55,
        taught_by: str = "agent",
    ) -> dict[str, Any]:
        skill = self.get(skill_id)
        if skill is None:
            return {"ok": False, "error": f"unknown skill: {skill_id}"}
        score = max(0.0, min(1.0, float(quality_0_to_1)))
        skill.quality_sum += score
        if score >= success_threshold:
            skill.success_count += 1
        else:
            skill.fail_count += 1
        if notes:
            skill.notes = (skill.notes + " | " + notes).strip(" |")[:500]
        skill.taught_by = taught_by or skill.taught_by
        self.skills[skill.id] = skill
        return {"ok": True, "rated": True, "quality": score, "skill": skill.to_dict()}

    def promote_host_rule(
        self,
        host_or_url: str,
        *,
        preferred_method: str = "auto",
        css_selector: str = "",
        notes: str = "",
        taught_by: str = "agent",
        category: str = "generic",
    ) -> dict[str, Any]:
        """Z reguły hosta zrób współdzieloną umiejętność (promocja lokalnego sukcesu)."""
        host = _host(host_or_url)
        if not host:
            return {"ok": False, "error": "host wymagany"}
        return self.learn(
            skill_id=_slug(f"host-{host}"),
            name=f"Host skill: {host}",
            category=category if category in SKILL_CATEGORIES else "generic",
            description=f"Umiejętność wypromowana z udanego parse hosta {host}",
            preferred_method=preferred_method,
            css_selectors=[css_selector] if css_selector else None,
            applies_to_hosts=[host],
            taught_by=taught_by,
            notes=notes,
            merge=True,
        )
