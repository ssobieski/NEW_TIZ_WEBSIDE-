"""
Firm profiling — konkurenci / dostawcy / dystrybucja / klienci.

Buduje karty profilowe z:
- tożsamość (nazwa, adresy, telefony, e-mail, WWW) + weryfikacja
- sygnały finansowe z treści publicznych (bez płatnych baz)
- PR / social (wzmianki + prosty sentiment)
- katalogi / cenniki / ulotki / e-shop
- sieć: dystrybutorzy, dealerzy, klienci, dostawcy, OEM
- tabela ocen (scorecard)
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import urlparse

from market_agents.firms import KnownFirmsIndex, normalize_firm_name

FirmRole = Literal[
    "competitor",
    "supplier",
    "distributor",
    "dealer",
    "customer",
    "prospect",
    "partner",
    "unknown",
]

VERIFICATION_STATUSES = ("unverified", "heuristic", "cross_checked", "manual")

SCORE_DIMENSIONS = (
    "completeness",
    "identity_trust",
    "market_visibility",
    "distribution_reach",
    "digital_commerce",
    "pr_presence",
    "financial_transparency",
    "overall",
)

_EMAIL_RE = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
)
_PHONE_RE = re.compile(
    r"(?:\+|00)?\d[\d\s\-()./]{7,}\d"
)
_POSTAL_RE = re.compile(
    r"\b\d{2}-\d{3}\b|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b|\b\d{4,5}\b"
)
_FIN_RE = re.compile(
    r"(?i)(?:revenue|turnover|sprzedaż|obrót|umsatz|jahresumsatz|"
    r"net\s+sales|sales\s+of)\s*(?:of\s*)?"
    r"(?:approx\.?\s*|ok\.\s*|около\s*)?"
    r"((?:[€$£]|EUR|USD|PLN|SEK|CHF)?\s?\d[\d\s.,]*\s*"
    r"(?:mrd|mld|bn|billion|mln|million|m\.?|tys\.?)?"
    r"(?:\s*(?:EUR|USD|PLN|SEK|CHF))?)",
)
_NEG_WORDS = {
    "scandal",
    "lawsuit",
    "fraud",
    "layoff",
    "bankrupt",
    "recall",
    "fine",
    "kryzys",
    "pozew",
    "afera",
    "zwolnienia",
}
_POS_WORDS = {
    "award",
    "growth",
    "expansion",
    "innovation",
    "partnership",
    "launch",
    "nagroda",
    "wzrost",
    "ekspansja",
    "innowacja",
    "partnerstwo",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def firm_id_from_name(name: str) -> str:
    norm = normalize_firm_name(name) or "unknown"
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
    return slug[:80] or "unknown"


def _clean_phone(raw: str) -> str:
    s = re.sub(r"[^\d+]", "", raw or "")
    if s.startswith("00"):
        s = "+" + s[2:]
    return s[:24]


def _looks_like_phone(raw: str) -> bool:
    digits = re.sub(r"\D", "", raw or "")
    return 8 <= len(digits) <= 15


def _domain(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:  # noqa: BLE001
        return ""


@dataclass
class ContactAttr:
    value: str
    verified: bool = False
    status: str = "heuristic"
    source: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AddressAttr:
    raw: str
    city: str = ""
    country: str = ""
    postal: str = ""
    verified: bool = False
    status: str = "heuristic"
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FirmProfile:
    id: str
    company: str
    roles: list[str] = field(default_factory=lambda: ["unknown"])
    country: str = ""
    legal_name: str = ""
    aliases: list[str] = field(default_factory=list)
    addresses: list[dict[str, Any]] = field(default_factory=list)
    phones: list[dict[str, Any]] = field(default_factory=list)
    emails: list[dict[str, Any]] = field(default_factory=list)
    websites: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    financial: dict[str, Any] = field(default_factory=dict)
    public_relations: dict[str, Any] = field(default_factory=dict)
    assets: dict[str, Any] = field(default_factory=dict)
    network: dict[str, Any] = field(default_factory=dict)
    scores: dict[str, Any] = field(default_factory=dict)
    prospect: dict[str, Any] = field(default_factory=dict)
    product_focus: str = ""
    notes: str = ""
    sources: list[str] = field(default_factory=list)
    updated_at: str = ""
    origin: str = "local"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FirmProfile:
        company = str(raw.get("company") or raw.get("name") or "").strip()
        fid = str(raw.get("id") or firm_id_from_name(company))
        roles = raw.get("roles") or raw.get("role") or ["unknown"]
        if isinstance(roles, str):
            roles = [roles]
        return cls(
            id=fid,
            company=company,
            roles=[str(r) for r in roles],
            country=str(raw.get("country") or ""),
            legal_name=str(raw.get("legal_name") or ""),
            aliases=[str(a) for a in (raw.get("aliases") or [])],
            addresses=list(raw.get("addresses") or []),
            phones=list(raw.get("phones") or []),
            emails=list(raw.get("emails") or []),
            websites=list(raw.get("websites") or []),
            verification=dict(raw.get("verification") or {}),
            financial=dict(raw.get("financial") or {}),
            public_relations=dict(raw.get("public_relations") or {}),
            assets=dict(raw.get("assets") or {}),
            network=dict(raw.get("network") or {}),
            scores=dict(raw.get("scores") or {}),
            prospect=dict(raw.get("prospect") or {}),
            product_focus=str(raw.get("product_focus") or ""),
            notes=str(raw.get("notes") or raw.get("presentation") or "")[:4000],
            sources=[str(s) for s in (raw.get("sources") or [])],
            updated_at=str(raw.get("updated_at") or ""),
            origin=str(raw.get("origin") or "local"),
        )


def extract_contacts_from_html(
    html: str,
    *,
    base_url: str = "",
    company: str = "",
) -> dict[str, Any]:
    """Heurystyczna ekstrakcja kontaktów z publicznego HTML (contact/about)."""
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html or "")
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain)

    emails: list[ContactAttr] = []
    seen_e: set[str] = set()
    for m in _EMAIL_RE.findall(plain):
        em = m.lower()
        if em in seen_e or em.endswith((".png", ".jpg", ".gif")):
            continue
        if any(x in em for x in ("example.com", "sentry.io", "wixpress", "schema.org")):
            continue
        seen_e.add(em)
        emails.append(ContactAttr(value=em, status="heuristic", source=base_url or "html"))

    phones: list[ContactAttr] = []
    seen_p: set[str] = set()
    for m in _PHONE_RE.findall(plain):
        if not _looks_like_phone(m):
            continue
        cleaned = _clean_phone(m)
        if cleaned in seen_p or len(re.sub(r"\D", "", cleaned)) < 8:
            continue
        seen_p.add(cleaned)
        phones.append(
            ContactAttr(value=cleaned, status="heuristic", source=base_url or "html", note=m.strip()[:40])
        )

    addresses: list[AddressAttr] = []
    # schema.org PostalAddress — bardzo prosto
    for m in re.finditer(
        r'(?is)itemtype=["\'][^"\']*PostalAddress["\'][^>]*>(.*?)</(?:div|span|section|address)',
        html or "",
    ):
        chunk = re.sub(r"<[^>]+>", " ", m.group(1))
        chunk = re.sub(r"\s+", " ", chunk).strip()
        if 12 <= len(chunk) <= 240:
            postal = ""
            pm = _POSTAL_RE.search(chunk)
            if pm:
                postal = pm.group(0)
            addresses.append(
                AddressAttr(raw=chunk[:240], postal=postal, status="heuristic", source="schema.org")
            )

    # linie z „Street/ul./Str.”
    for m in re.finditer(
        r"(?i)((?:ul\.|ulica|str\.|street|avenue|weg|straße|strasse)[^.]{8,120})",
        plain,
    ):
        raw = m.group(1).strip()
        if len(addresses) >= 5:
            break
        if any(a.raw.lower() == raw.lower() for a in addresses):
            continue
        addresses.append(AddressAttr(raw=raw[:240], status="heuristic", source=base_url or "html"))

    websites: list[dict[str, Any]] = []
    if base_url:
        websites.append(
            {
                "url": base_url,
                "primary": True,
                "verified": True,
                "status": "cross_checked",
                "source": "fetch",
            }
        )

    financial_signals: list[dict[str, Any]] = []
    for m in _FIN_RE.finditer(plain[:50000]):
        financial_signals.append(
            {
                "metric": "public_revenue_mention",
                "value_text": m.group(0).strip()[:160],
                "source_url": base_url,
                "confidence": 0.35,
                "extracted_at": utc_now_iso(),
            }
        )
        if len(financial_signals) >= 5:
            break

    legal_name = ""
    if company:
        # © Company Name
        cm = re.search(
            rf"(?i)(?:©|copyright)\s*\d{{0,4}}\s*([^.]{{3,80}})",
            plain[:20000],
        )
        if cm:
            legal_name = cm.group(1).strip()[:120]

    return {
        "emails": [e.to_dict() for e in emails[:12]],
        "phones": [p.to_dict() for p in phones[:12]],
        "addresses": [a.to_dict() for a in addresses[:8]],
        "websites": websites,
        "financial_signals": financial_signals,
        "legal_name": legal_name,
        "excerpt": plain[:500],
    }


def score_sentiment(texts: Iterable[str]) -> dict[str, Any]:
    pos = neg = 0
    n = 0
    for t in texts:
        low = (t or "").lower()
        if not low.strip():
            continue
        n += 1
        pos += sum(1 for w in _POS_WORDS if w in low)
        neg += sum(1 for w in _NEG_WORDS if w in low)
    if n == 0:
        return {"label": "unknown", "score": 0.0, "sample_size": 0}
    raw = (pos - neg) / max(1, pos + neg)
    if pos == 0 and neg == 0:
        label = "neutral"
        score = 0.0
    elif raw > 0.25:
        label = "positive"
        score = min(1.0, raw)
    elif raw < -0.25:
        label = "negative"
        score = max(-1.0, raw)
    else:
        label = "mixed" if (pos and neg) else "neutral"
        score = raw
    return {"label": label, "score": round(score, 3), "sample_size": n, "pos_hits": pos, "neg_hits": neg}


def compute_scorecard(profile: FirmProfile) -> dict[str, Any]:
    """Tabela ocen 0–100."""
    id_fields = 0
    id_fields += 1 if profile.company else 0
    id_fields += 1 if profile.country else 0
    id_fields += 1 if profile.websites else 0
    id_fields += 1 if profile.emails else 0
    id_fields += 1 if profile.phones else 0
    id_fields += 1 if profile.addresses else 0
    completeness = round(100 * id_fields / 6)

    verified = 0
    total_attrs = 0
    for bucket in (profile.emails, profile.phones, profile.addresses, profile.websites):
        for row in bucket:
            total_attrs += 1
            if row.get("verified") or row.get("status") in {"cross_checked", "manual"}:
                verified += 1
    identity_trust = round(100 * verified / total_attrs) if total_attrs else (40 if profile.websites else 10)

    assets = profile.assets or {}
    cat_n = len(assets.get("catalogs") or [])
    price_n = len(assets.get("pricelists") or [])
    leaf_n = len(assets.get("leaflets") or [])
    shop_n = len(assets.get("eshops") or [])
    market_visibility = min(100, cat_n * 15 + leaf_n * 10 + (20 if profile.product_focus else 0) + 10)
    digital_commerce = min(100, shop_n * 40 + price_n * 25 + (15 if cat_n else 0))

    net = profile.network or {}
    dist_n = len(net.get("distributors") or []) + len(net.get("dealers") or [])
    cust_n = len(net.get("customers") or [])
    supp_n = len(net.get("suppliers") or [])
    distribution_reach = min(100, dist_n * 20 + cust_n * 15 + supp_n * 10)

    pr = profile.public_relations or {}
    mentions = pr.get("mentions") or []
    sent = pr.get("sentiment") or {}
    pr_presence = min(100, len(mentions) * 12 + (20 if sent.get("sample_size") else 0))
    if sent.get("label") == "negative":
        pr_presence = max(0, pr_presence - 15)

    fin = profile.financial or {}
    signals = fin.get("signals") or []
    financial_transparency = min(100, len(signals) * 25 + (10 if fin.get("notes") else 0))

    overall = round(
        0.15 * completeness
        + 0.15 * identity_trust
        + 0.15 * market_visibility
        + 0.15 * distribution_reach
        + 0.15 * digital_commerce
        + 0.15 * pr_presence
        + 0.10 * financial_transparency
    )

    table = [
        {"dimension": "completeness", "score": completeness, "label": "Kompletność danych tożsamości"},
        {"dimension": "identity_trust", "score": identity_trust, "label": "Weryfikacja atrybutów"},
        {"dimension": "market_visibility", "score": market_visibility, "label": "Widoczność (katalogi/ulotki)"},
        {"dimension": "distribution_reach", "score": distribution_reach, "label": "Sieć dystrybucji / klienci"},
        {"dimension": "digital_commerce", "score": digital_commerce, "label": "E-shop / cenniki"},
        {"dimension": "pr_presence", "score": pr_presence, "label": "PR / social"},
        {"dimension": "financial_transparency", "score": financial_transparency, "label": "Sygnały finansowe (publiczne)"},
        {"dimension": "overall", "score": overall, "label": "Ocena łączna"},
    ]
    return {
        "completeness": completeness,
        "identity_trust": identity_trust,
        "market_visibility": market_visibility,
        "distribution_reach": distribution_reach,
        "digital_commerce": digital_commerce,
        "pr_presence": pr_presence,
        "financial_transparency": financial_transparency,
        "overall": overall,
        "table": table,
        "scored_at": utc_now_iso(),
    }


def verify_identity(profile: FirmProfile) -> dict[str, Any]:
    """Cross-check: e-mail/WWW domain vs site_url; phone length; address postal."""
    checks: list[dict[str, Any]] = []
    primary_web = ""
    for w in profile.websites:
        if w.get("primary") or not primary_web:
            primary_web = str(w.get("url") or "")
    dom = _domain(primary_web)

    for em in profile.emails:
        edom = str(em.get("value") or "").split("@")[-1].lower()
        ok = bool(dom) and (edom == dom or edom.endswith("." + dom) or dom.endswith("." + edom))
        em["verified"] = ok
        em["status"] = "cross_checked" if ok else em.get("status") or "heuristic"
        checks.append({"attr": "email", "value": em.get("value"), "ok": ok, "rule": "email_domain_matches_website"})

    for ph in profile.phones:
        ok = _looks_like_phone(str(ph.get("value") or ""))
        ph["verified"] = ok
        ph["status"] = "cross_checked" if ok else "heuristic"
        checks.append({"attr": "phone", "value": ph.get("value"), "ok": ok, "rule": "phone_digit_length"})

    for ad in profile.addresses:
        raw = str(ad.get("raw") or "")
        ok = len(raw) >= 12 and bool(_POSTAL_RE.search(raw) or re.search(r"(?i)ul\.|street|str\.", raw))
        ad["verified"] = ok
        ad["status"] = "cross_checked" if ok else "heuristic"
        checks.append({"attr": "address", "value": raw[:80], "ok": ok, "rule": "address_structure"})

    for w in profile.websites:
        url = str(w.get("url") or "")
        ok = urlparse(url).scheme in {"http", "https"} and bool(urlparse(url).hostname)
        w["verified"] = ok
        w["status"] = "cross_checked" if ok else "unverified"
        checks.append({"attr": "website", "value": url, "ok": ok, "rule": "url_scheme_host"})

    ok_n = sum(1 for c in checks if c["ok"])
    score = round(ok_n / len(checks), 3) if checks else 0.0
    return {
        "score": score,
        "checks": checks[:40],
        "updated_at": utc_now_iso(),
        "primary_domain": dom,
    }


class FirmProfileRegistry:
    """Rejestr profili firm (JSON)."""

    def __init__(self, profiles: list[FirmProfile] | None = None) -> None:
        self.profiles: dict[str, FirmProfile] = {}
        for p in profiles or []:
            self.profiles[p.id] = p

    @classmethod
    def load(cls, data_dir: Path | str | None = None) -> FirmProfileRegistry:
        data_dir = Path(data_dir or "data")
        path = data_dir / "knowledge" / "firm_profiles.json"
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return cls()
        rows = raw.get("profiles") if isinstance(raw, dict) else raw
        profiles = [FirmProfile.from_dict(r) for r in (rows or []) if isinstance(r, dict)]
        return cls(profiles)

    def save(self, data_dir: Path | str | None = None) -> Path:
        data_dir = Path(data_dir or "data")
        path = data_dir / "knowledge" / "firm_profiles.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": utc_now_iso(),
            "count": len(self.profiles),
            "score_dimensions": list(SCORE_DIMENSIONS),
            "profiles": [p.to_dict() for p in sorted(self.profiles.values(), key=lambda x: x.company.lower())],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def upsert(self, profile: FirmProfile) -> FirmProfile:
        existing = self.profiles.get(profile.id)
        if existing:
            profile = self._merge(existing, profile)
        profile.verification = verify_identity(profile)
        profile.scores = compute_scorecard(profile)
        profile.updated_at = utc_now_iso()
        self.profiles[profile.id] = profile
        return profile

    @staticmethod
    def _merge(old: FirmProfile, new: FirmProfile) -> FirmProfile:
        def merge_list(a: list[dict[str, Any]], b: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
            seen: set[str] = set()
            out: list[dict[str, Any]] = []
            for row in list(a or []) + list(b or []):
                k = str(row.get(key) or row.get("raw") or row.get("url") or "").lower().strip()
                if not k or k in seen:
                    continue
                seen.add(k)
                out.append(row)
            return out

        def merge_signals(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[dict[str, Any]]:
            seen: set[str] = set()
            out: list[dict[str, Any]] = []
            for row in list(a or []) + list(b or []):
                k = f"{row.get('metric')}|{row.get('value_text')}"
                if k in seen:
                    continue
                seen.add(k)
                out.append(row)
            return out[:20]

        roles = list(dict.fromkeys(list(old.roles) + list(new.roles)))
        merged_assets = {**(old.assets or {}), **(new.assets or {})}
        for bucket in ("catalogs", "pricelists", "leaflets", "eshops", "downloads"):
            merged_assets[bucket] = merge_list(
                (old.assets or {}).get(bucket, []),
                (new.assets or {}).get(bucket, []),
                "url",
            )
        return FirmProfile(
            id=old.id,
            company=new.company or old.company,
            roles=roles or ["unknown"],
            country=new.country or old.country,
            legal_name=new.legal_name or old.legal_name,
            aliases=list(dict.fromkeys(old.aliases + new.aliases)),
            addresses=merge_list(old.addresses, new.addresses, "raw"),
            phones=merge_list(old.phones, new.phones, "value"),
            emails=merge_list(old.emails, new.emails, "value"),
            websites=merge_list(old.websites, new.websites, "url"),
            verification=new.verification or old.verification,
            financial={
                "signals": merge_signals(
                    (old.financial or {}).get("signals", []),
                    (new.financial or {}).get("signals", []),
                ),
                "notes": (new.financial or {}).get("notes") or (old.financial or {}).get("notes") or "",
                "tooling_budget": (new.financial or {}).get("tooling_budget")
                or (old.financial or {}).get("tooling_budget"),
            },
            public_relations=new.public_relations or old.public_relations,
            assets=merged_assets,
            network={
                k: list(
                    dict.fromkeys(
                        list((old.network or {}).get(k) or [])
                        + list((new.network or {}).get(k) or [])
                    )
                )
                for k in (
                    "distributors",
                    "dealers",
                    "customers",
                    "suppliers",
                    "oem_group",
                    "competitors",
                    "partners",
                )
            },
            scores=new.scores or old.scores,
            prospect=new.prospect or old.prospect,
            product_focus=new.product_focus or old.product_focus,
            notes=(new.notes or old.notes)[:4000],
            sources=list(dict.fromkeys(old.sources + new.sources)),
            updated_at=utc_now_iso(),
            origin=new.origin or old.origin,
        )

    def get(self, company_or_id: str) -> FirmProfile | None:
        key = (company_or_id or "").strip()
        if not key:
            return None
        if key in self.profiles:
            return self.profiles[key]
        fid = firm_id_from_name(key)
        if fid in self.profiles:
            return self.profiles[fid]
        norm = normalize_firm_name(key)
        for p in self.profiles.values():
            if normalize_firm_name(p.company) == norm:
                return p
            if norm in {normalize_firm_name(a) for a in p.aliases}:
                return p
        return None

    def list(
        self,
        *,
        role: str | None = None,
        q: str | None = None,
        sort: str = "overall",
        limit: int = 50,
    ) -> list[FirmProfile]:
        rows = list(self.profiles.values())
        if role:
            rows = [p for p in rows if role in (p.roles or [])]
        if q:
            ql = q.lower()
            rows = [
                p
                for p in rows
                if ql in p.company.lower()
                or ql in (p.country or "").lower()
                or ql in (p.product_focus or "").lower()
            ]
        rev = True
        if sort == "company":
            rows.sort(key=lambda p: p.company.lower())
            rev = False
        else:
            rows.sort(key=lambda p: float((p.scores or {}).get(sort) or (p.scores or {}).get("overall") or 0), reverse=True)
        return rows[:limit] if not rev or sort != "company" else rows[:limit]

    def scoreboard(self, *, limit: int = 30) -> list[dict[str, Any]]:
        rows = []
        for p in self.list(sort="overall", limit=limit):
            sc = p.scores or {}
            rows.append(
                {
                    "company": p.company,
                    "id": p.id,
                    "roles": p.roles,
                    "country": p.country,
                    "overall": sc.get("overall", 0),
                    "completeness": sc.get("completeness", 0),
                    "identity_trust": sc.get("identity_trust", 0),
                    "distribution_reach": sc.get("distribution_reach", 0),
                    "digital_commerce": sc.get("digital_commerce", 0),
                    "pr_presence": sc.get("pr_presence", 0),
                    "financial_transparency": sc.get("financial_transparency", 0),
                }
            )
        return rows

    def apply_enrichment(self, profile: FirmProfile, extracted: dict[str, Any]) -> FirmProfile:
        profile.emails = list(profile.emails) + list(extracted.get("emails") or [])
        profile.phones = list(profile.phones) + list(extracted.get("phones") or [])
        profile.addresses = list(profile.addresses) + list(extracted.get("addresses") or [])
        profile.websites = list(profile.websites) + list(extracted.get("websites") or [])
        if extracted.get("legal_name") and not profile.legal_name:
            profile.legal_name = str(extracted["legal_name"])
        signals = list((profile.financial or {}).get("signals") or [])
        signals.extend(extracted.get("financial_signals") or [])
        profile.financial = {
            "signals": signals[:20],
            "notes": (profile.financial or {}).get("notes") or "Public web mentions only",
        }
        if "html" not in profile.sources:
            profile.sources.append("html_enrichment")
        return self.upsert(profile)

    def build_from_ecosystem(
        self,
        data_dir: Path | str,
        *,
        competitors: list[str] | None = None,
        default_role: str = "competitor",
    ) -> dict[str, Any]:
        """
        Złóż / zaktualizuj profile z known_firms + relations + pricelists + literature + social hints.
        """
        data_dir = Path(data_dir)
        known = KnownFirmsIndex.load(data_dir, competitors=competitors)
        created = updated = 0

        # 1) baza z known_firms
        for firm in known.firms:
            company = str(firm.get("company") or "").strip()
            if not company:
                continue
            fid = firm_id_from_name(company)
            before = fid in self.profiles
            site = str(firm.get("site_url") or "").strip()
            roles = [default_role]
            # heurystyka roli z product_focus / note
            focus = str(firm.get("product_focus") or "").lower()
            if "distribut" in focus or "dealer" in focus:
                roles = ["distributor"]
            profile = FirmProfile(
                id=fid,
                company=company,
                roles=roles,
                country=str(firm.get("country") or ""),
                websites=[{"url": site, "primary": True, "verified": bool(site), "status": "heuristic", "source": "known_firms"}]
                if site
                else [],
                product_focus=str(firm.get("product_focus") or ""),
                notes=str(firm.get("presentation") or firm.get("note") or "")[:2000],
                assets={
                    "catalogs": [],
                    "pricelists": [],
                    "leaflets": [],
                    "eshops": [],
                    "downloads": [firm["downloads"]] if firm.get("downloads") else [],
                },
                sources=["known_firms"],
                origin="ecosystem_build",
            )
            self.upsert(profile)
            if before:
                updated += 1
            else:
                created += 1

        # 2) sieć z firm_relations
        try:
            from market_agents.firm_relations import FirmRelationsGraph

            graph = FirmRelationsGraph.load(data_dir)
        except Exception:  # noqa: BLE001
            graph = None
        if graph:
            for edge in graph.edges:
                src, tgt, rtype = edge["source"], edge["target"], edge["relation_type"]
                self._ensure_stub(src, role="unknown")
                self._ensure_stub(tgt, role="unknown")
                sp = self.get(src)
                tp = self.get(tgt)
                if not sp or not tp:
                    continue
                net_s = dict(sp.network or {})
                net_t = dict(tp.network or {})
                if rtype in {"distributor_of", "dealer_of"}:
                    # src distributes tgt's products
                    key = "distributors" if rtype == "distributor_of" else "dealers"
                    net_t.setdefault(key, [])
                    if src not in net_t[key]:
                        net_t[key].append(src)
                    if "distributor" not in sp.roles and "dealer" not in sp.roles:
                        sp.roles = list(dict.fromkeys(sp.roles + ["distributor" if rtype == "distributor_of" else "dealer"]))
                elif rtype == "customer_of":
                    # src is customer of tgt
                    net_t.setdefault("customers", [])
                    if src not in net_t["customers"]:
                        net_t["customers"].append(src)
                    net_s.setdefault("suppliers", [])
                    if tgt not in net_s["suppliers"]:
                        net_s["suppliers"].append(tgt)
                    if "customer" not in sp.roles:
                        sp.roles = list(dict.fromkeys(sp.roles + ["customer"]))
                    if "supplier" not in tp.roles:
                        tp.roles = list(dict.fromkeys(tp.roles + ["supplier"]))
                elif rtype == "supplier_of":
                    # src supplies tgt
                    net_t.setdefault("suppliers", [])
                    if src not in net_t["suppliers"]:
                        net_t["suppliers"].append(src)
                    net_s.setdefault("customers", [])
                    if tgt not in net_s["customers"]:
                        net_s["customers"].append(tgt)
                    if "supplier" not in sp.roles:
                        sp.roles = list(dict.fromkeys(sp.roles + ["supplier"]))
                elif rtype == "oem_group":
                    net_s.setdefault("oem_group", [])
                    net_t.setdefault("oem_group", [])
                    if tgt not in net_s["oem_group"]:
                        net_s["oem_group"].append(tgt)
                    if src not in net_t["oem_group"]:
                        net_t["oem_group"].append(src)
                elif rtype == "partner_of":
                    net_s.setdefault("partners", [])
                    net_t.setdefault("partners", [])
                    if tgt not in net_s["partners"]:
                        net_s["partners"].append(tgt)
                    if src not in net_t["partners"]:
                        net_t["partners"].append(src)
                elif rtype == "competes_with":
                    net_s.setdefault("competitors", [])
                    net_t.setdefault("competitors", [])
                    if tgt not in net_s["competitors"]:
                        net_s["competitors"].append(tgt)
                    if src not in net_t["competitors"]:
                        net_t["competitors"].append(src)
                    for prof in (sp, tp):
                        if "competitor" not in prof.roles:
                            prof.roles = list(dict.fromkeys(prof.roles + ["competitor"]))
                elif rtype == "brand_of":
                    if tgt not in sp.aliases and tgt != sp.company:
                        pass
                    if src not in tp.aliases and src != tp.company:
                        tp.aliases = list(dict.fromkeys(tp.aliases + [src]))
                sp.network = net_s
                tp.network = net_t
                if "firm_relations" not in sp.sources:
                    sp.sources.append("firm_relations")
                if "firm_relations" not in tp.sources:
                    tp.sources.append("firm_relations")
                self.upsert(sp)
                self.upsert(tp)

        # 3) cenniki
        try:
            from market_agents.pricelists import PricelistRegistry

            prices = PricelistRegistry.load(data_dir)
            for item in prices.items.values():
                brand = str(getattr(item, "brand", "") or "").strip()
                if not brand:
                    continue
                p = self.get(brand) or self._ensure_stub(brand, role="competitor")
                assets = dict(p.assets or {})
                assets.setdefault("pricelists", [])
                entry = {
                    "url": getattr(item, "url", None),
                    "title": getattr(item, "title", None),
                    "access": getattr(item, "access", None),
                    "source": "pricelists",
                }
                if entry["url"] and not any(x.get("url") == entry["url"] for x in assets["pricelists"]):
                    assets["pricelists"].append(entry)
                p.assets = assets
                if "pricelists" not in p.sources:
                    p.sources.append("pricelists")
                self.upsert(p)
        except Exception:  # noqa: BLE001
            pass

        # 4) literature / product_tech as leaflets/catalogs hints
        for fname, bucket in (
            ("literature.json", "leaflets"),
            ("product_tech.json", "catalogs"),
        ):
            path = data_dir / "knowledge" / fname
            if not path.is_file():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if isinstance(raw, list):
                items = raw
            elif isinstance(raw, dict):
                items = (
                    raw.get("items")
                    or raw.get("entries")
                    or raw.get("literature")
                    or raw.get("documents")
                    or []
                )
            else:
                items = []
            for row in items if isinstance(items, list) else []:
                if not isinstance(row, dict):
                    continue
                brand = str(row.get("brand") or row.get("company") or row.get("publisher") or "").strip()
                if not brand:
                    continue
                p = self.get(brand) or self._ensure_stub(brand, role="competitor")
                assets = dict(p.assets or {})
                assets.setdefault(bucket, [])
                entry = {"url": row.get("url"), "title": row.get("title") or row.get("name"), "source": fname}
                if entry.get("url") and not any(x.get("url") == entry["url"] for x in assets[bucket]):
                    assets[bucket].append(entry)
                # eshop hint
                kind = str(row.get("kind") or row.get("asset_type") or "").lower()
                if kind in {"eshop", "e-shop", "webshop"}:
                    assets.setdefault("eshops", [])
                    if entry.get("url") and not any(x.get("url") == entry["url"] for x in assets["eshops"]):
                        assets["eshops"].append(entry)
                p.assets = assets
                self.upsert(p)

        # 5) social mentions from agent memory / optional file
        social_path = data_dir / "knowledge" / "social_mentions.json"
        if social_path.is_file():
            try:
                mentions_raw = json.loads(social_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                mentions_raw = []
            by_firm: dict[str, list[dict[str, Any]]] = {}
            for m in mentions_raw if isinstance(mentions_raw, list) else mentions_raw.get("mentions") or []:
                if not isinstance(m, dict):
                    continue
                company = str(m.get("company") or m.get("brand") or "").strip()
                if not company:
                    continue
                by_firm.setdefault(company, []).append(m)
            for company, mentions in by_firm.items():
                p = self.get(company) or self._ensure_stub(company, role="competitor")
                texts = [str(x.get("snippet") or x.get("title") or "") for x in mentions]
                p.public_relations = {
                    "summary": f"{len(mentions)} publicznych wzmianek social",
                    "themes": [],
                    "sentiment": score_sentiment(texts),
                    "mentions": mentions[:30],
                }
                if "social" not in p.sources:
                    p.sources.append("social")
                self.upsert(p)

        path = self.save(data_dir)
        return {
            "ok": True,
            "path": str(path),
            "count": len(self.profiles),
            "created_or_seen": created + updated,
            "scoreboard_top": self.scoreboard(limit=10),
        }

    def _ensure_stub(self, company: str, *, role: str = "unknown") -> FirmProfile:
        existing = self.get(company)
        if existing:
            return existing
        p = FirmProfile(
            id=firm_id_from_name(company),
            company=company,
            roles=[role],
            sources=["stub"],
            origin="ecosystem_build",
        )
        return self.upsert(p)


def profile_from_known_row(firm: dict[str, Any], *, role: str = "competitor") -> FirmProfile:
    company = str(firm.get("company") or "").strip()
    site = str(firm.get("site_url") or "").strip()
    return FirmProfile(
        id=firm_id_from_name(company),
        company=company,
        roles=[role],
        country=str(firm.get("country") or ""),
        websites=[{"url": site, "primary": True, "verified": bool(site), "status": "heuristic", "source": "known_firms"}]
        if site
        else [],
        product_focus=str(firm.get("product_focus") or ""),
        notes=str(firm.get("presentation") or "")[:2000],
        sources=["known_firms"],
    )
