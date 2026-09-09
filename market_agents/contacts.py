"""
Baza kontaktów (osoby) powiązana z firmami: prospecty, konkurencja, dostawcy.

Oddziela osoby od FirmProfile (który trzyma tylko firmowe e-maile/telefony).
Kontakt ma:
- tożsamość + kanały
- linki do firm (works_at / buys_for / influences / competitor_rep …)
- lekki profil sprzedażowy (wpływ na zakup, preferencje, notatki)
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from market_agents.firm_profiles import firm_id_from_name
from market_agents.firms import normalize_firm_name

ContactRole = Literal[
    "purchasing",
    "production",
    "technology",
    "quality",
    "owner_exec",
    "engineering",
    "sales",
    "other",
]

FirmLinkRelation = Literal[
    "works_at",
    "buys_for",
    "influences",
    "competitor_rep",
    "partner_rep",
    "former",
    "unknown",
]

Influence = Literal["high", "medium", "low", "unknown"]

CONTACT_ROLES = (
    "purchasing",
    "production",
    "technology",
    "quality",
    "owner_exec",
    "engineering",
    "sales",
    "other",
)

FIRM_LINK_RELATIONS = (
    "works_at",
    "buys_for",
    "influences",
    "competitor_rep",
    "partner_rep",
    "former",
    "unknown",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def contact_id_from_name(name: str, company: str = "") -> str:
    base = f"{normalize_firm_name(name) or 'unknown'}-{normalize_firm_name(company) or 'x'}"
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return (slug[:72] or "unknown") + "-" + uuid.uuid4().hex[:6]


def _norm_email(value: str) -> str:
    return (value or "").strip().lower()


def _clean_phone(raw: str) -> str:
    s = re.sub(r"[^\d+]", "", raw or "")
    if s.startswith("00"):
        s = "+" + s[2:]
    return s[:24]


@dataclass
class Contact:
    id: str
    name: str
    title: str = ""
    role: str = "other"
    emails: list[dict[str, Any]] = field(default_factory=list)
    phones: list[dict[str, Any]] = field(default_factory=list)
    linkedin_url: str = ""
    firm_links: list[dict[str, Any]] = field(default_factory=list)
    influence_on_purchase: str = "unknown"
    profile: dict[str, Any] = field(default_factory=dict)
    competitors_mentioned: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    updated_at: str = field(default_factory=utc_now_iso)
    origin: str = "local"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Contact:
        name = str(raw.get("name") or "").strip()
        company_hint = ""
        links = list(raw.get("firm_links") or [])
        if links and isinstance(links[0], dict):
            company_hint = str(links[0].get("company") or "")
        cid = str(raw.get("id") or contact_id_from_name(name, company_hint))
        role = str(raw.get("role") or "other")
        if role not in CONTACT_ROLES:
            role = "other"
        influence = str(raw.get("influence_on_purchase") or "unknown")
        if influence not in {"high", "medium", "low", "unknown"}:
            influence = "unknown"
        return cls(
            id=cid,
            name=name,
            title=str(raw.get("title") or "")[:120],
            role=role,
            emails=list(raw.get("emails") or []),
            phones=list(raw.get("phones") or []),
            linkedin_url=str(raw.get("linkedin_url") or "")[:300],
            firm_links=[l for l in links if isinstance(l, dict)],
            influence_on_purchase=influence,
            profile=dict(raw.get("profile") or {}),
            competitors_mentioned=[str(c) for c in (raw.get("competitors_mentioned") or [])][:20],
            sources=[str(s) for s in (raw.get("sources") or [])][:20],
            verification=dict(raw.get("verification") or {}),
            notes=str(raw.get("notes") or "")[:2000],
            updated_at=str(raw.get("updated_at") or utc_now_iso()),
            origin=str(raw.get("origin") or "local"),
        )

    def primary_company(self) -> str:
        for link in self.firm_links:
            if link.get("is_primary"):
                return str(link.get("company") or "")
        if self.firm_links:
            return str(self.firm_links[0].get("company") or "")
        return ""

    def primary_firm_id(self) -> str:
        for link in self.firm_links:
            if link.get("is_primary"):
                return str(link.get("firm_id") or "")
        if self.firm_links:
            return str(self.firm_links[0].get("firm_id") or "")
        return ""


def make_firm_link(
    *,
    company: str,
    relation: str = "works_at",
    influence: str = "unknown",
    is_primary: bool = True,
    evidence: str = "",
    firm_id: str | None = None,
) -> dict[str, Any]:
    company = (company or "").strip()
    if relation not in FIRM_LINK_RELATIONS:
        relation = "unknown"
    if influence not in {"high", "medium", "low", "unknown"}:
        influence = "unknown"
    return {
        "firm_id": firm_id or firm_id_from_name(company),
        "company": company,
        "relation": relation,
        "influence_on_purchase": influence,
        "is_primary": bool(is_primary),
        "evidence": (evidence or "")[:400],
    }


class ContactRegistry:
    def __init__(self, contacts: list[Contact] | None = None) -> None:
        self.contacts: list[Contact] = list(contacts or [])

    @classmethod
    def load(cls, data_dir: Path | str | None = None) -> ContactRegistry:
        path = Path(data_dir or "data") / "knowledge" / "contacts.json"
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            rows = raw.get("contacts") if isinstance(raw, dict) else raw
        except Exception:  # noqa: BLE001
            return cls()
        out: list[Contact] = []
        for r in rows or []:
            if not isinstance(r, dict) or not r.get("name"):
                continue
            try:
                out.append(Contact.from_dict(r))
            except Exception:  # noqa: BLE001
                continue
        return cls(out)

    def save(self, data_dir: Path | str | None = None) -> Path:
        data_dir = Path(data_dir or "data")
        path = data_dir / "knowledge" / "contacts.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": utc_now_iso(),
            "count": len(self.contacts),
            "contacts": [c.to_dict() for c in self.contacts],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def get(self, contact_id: str) -> Contact | None:
        for c in self.contacts:
            if c.id == contact_id:
                return c
        return None

    def find_by_name_company(self, name: str, company: str = "") -> Contact | None:
        n = (normalize_firm_name(name) or "").strip()
        c_norm = normalize_firm_name(company) if company else ""
        if not n:
            return None
        for contact in self.contacts:
            if normalize_firm_name(contact.name) != n:
                continue
            if not c_norm:
                return contact
            for link in contact.firm_links:
                if normalize_firm_name(str(link.get("company") or "")) == c_norm:
                    return contact
        return None

    def upsert(self, contact: Contact, *, merge: bool = True) -> tuple[Contact, bool]:
        """Zwraca (contact, created)."""
        existing = self.get(contact.id) or self.find_by_name_company(
            contact.name, contact.primary_company()
        )
        if existing is None:
            self.contacts.insert(0, contact)
            return contact, True
        if not merge:
            idx = self.contacts.index(existing)
            contact.id = existing.id
            contact.updated_at = utc_now_iso()
            self.contacts[idx] = contact
            return contact, False
        # merge fields
        if contact.title and not existing.title:
            existing.title = contact.title
        elif contact.title and len(contact.title) > len(existing.title or ""):
            existing.title = contact.title
        if contact.role != "other":
            existing.role = contact.role
        if contact.influence_on_purchase != "unknown":
            rank = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
            if rank.get(contact.influence_on_purchase, 0) >= rank.get(
                existing.influence_on_purchase, 0
            ):
                existing.influence_on_purchase = contact.influence_on_purchase
        existing.emails = _merge_attrs(existing.emails, contact.emails)
        existing.phones = _merge_attrs(existing.phones, contact.phones)
        if contact.linkedin_url and not existing.linkedin_url:
            existing.linkedin_url = contact.linkedin_url
        existing.firm_links = _merge_firm_links(existing.firm_links, contact.firm_links)
        for brand in contact.competitors_mentioned:
            if brand not in existing.competitors_mentioned:
                existing.competitors_mentioned.append(brand)
        existing.competitors_mentioned = existing.competitors_mentioned[:20]
        for src in contact.sources:
            if src not in existing.sources:
                existing.sources.append(src)
        existing.sources = existing.sources[:20]
        if contact.notes:
            if contact.notes[:60] not in (existing.notes or ""):
                existing.notes = ((existing.notes or "") + " | " + contact.notes).strip(" |")[:2000]
        if contact.profile:
            existing.profile = {**(existing.profile or {}), **contact.profile}
        existing.updated_at = utc_now_iso()
        return existing, False

    def list(
        self,
        *,
        q: str | None = None,
        company: str | None = None,
        role: str | None = None,
        relation: str | None = None,
        limit: int = 50,
    ) -> list[Contact]:
        rows = self.contacts
        if q:
            ql = q.lower()
            rows = [
                c
                for c in rows
                if ql in c.name.lower()
                or ql in (c.title or "").lower()
                or ql in c.primary_company().lower()
            ]
        if company:
            cn = normalize_firm_name(company) or company.lower()
            rows = [
                c
                for c in rows
                if any(
                    (normalize_firm_name(str(l.get("company") or "")) or "") == cn
                    or cn in str(l.get("company") or "").lower()
                    for l in c.firm_links
                )
            ]
        if role:
            rows = [c for c in rows if c.role == role]
        if relation:
            rows = [
                c for c in rows if any(l.get("relation") == relation for l in c.firm_links)
            ]
        return rows[:limit]

    def for_firm(self, company_or_id: str) -> list[Contact]:
        key = (company_or_id or "").strip()
        if not key:
            return []
        norm = normalize_firm_name(key) or key.lower()
        fid = firm_id_from_name(key)
        out: list[Contact] = []
        for c in self.contacts:
            for link in c.firm_links:
                if str(link.get("firm_id") or "") == fid:
                    out.append(c)
                    break
                if (normalize_firm_name(str(link.get("company") or "")) or "") == norm:
                    out.append(c)
                    break
        return out

    def scoreboard(self, *, limit: int = 40) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        influence_rank = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
        for c in self.contacts:
            rows.append(
                {
                    "id": c.id,
                    "name": c.name,
                    "title": c.title,
                    "role": c.role,
                    "company": c.primary_company(),
                    "firm_id": c.primary_firm_id(),
                    "influence_on_purchase": c.influence_on_purchase,
                    "emails": len(c.emails),
                    "phones": len(c.phones),
                    "firm_links": len(c.firm_links),
                    "competitors_mentioned": c.competitors_mentioned[:5],
                    "reachability": _reachability_score(c),
                }
            )
        rows.sort(
            key=lambda r: (
                influence_rank.get(str(r["influence_on_purchase"]), 0),
                int(r["reachability"]),
                int(r["emails"]) + int(r["phones"]),
            ),
            reverse=True,
        )
        return rows[:limit]


def _reachability_score(c: Contact) -> int:
    score = 0
    if c.emails:
        score += 40
    if c.phones:
        score += 30
    if c.linkedin_url:
        score += 15
    if c.name:
        score += 10
    if c.influence_on_purchase == "high":
        score += 20
    elif c.influence_on_purchase == "medium":
        score += 10
    return min(100, score)


def _merge_attrs(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out = list(existing or [])
    seen = {
        str(a.get("value") or "").lower().replace(" ", "")
        for a in out
        if isinstance(a, dict)
    }
    for a in incoming or []:
        if not isinstance(a, dict):
            continue
        val = str(a.get("value") or "").strip()
        if not val:
            continue
        key = val.lower().replace(" ", "")
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out[:20]


def _merge_firm_links(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out = list(existing or [])
    keys = {
        (
            normalize_firm_name(str(l.get("company") or "")),
            str(l.get("relation") or "works_at"),
        )
        for l in out
        if isinstance(l, dict)
    }
    for link in incoming or []:
        if not isinstance(link, dict):
            continue
        company = str(link.get("company") or "").strip()
        if not company:
            continue
        rel = str(link.get("relation") or "works_at")
        key = (normalize_firm_name(company), rel)
        if key in keys:
            continue
        keys.add(key)
        out.append(link)
    # ensure one primary
    if out and not any(l.get("is_primary") for l in out):
        out[0]["is_primary"] = True
    return out[:12]


def contact_from_stakeholder(
    stakeholder: dict[str, Any],
    *,
    company: str,
    competitors: list[str] | None = None,
    source: str = "prospect_stakeholder",
) -> Contact | None:
    name = stakeholder.get("name")
    if not name or not str(name).strip():
        return None
    name = str(name).strip()
    role = str(stakeholder.get("role") or "other")
    if role not in CONTACT_ROLES:
        # map prospect roles
        role_map = {
            "purchasing": "purchasing",
            "production": "production",
            "technology": "technology",
            "quality": "quality",
            "owner": "owner_exec",
            "executive": "owner_exec",
            "engineering": "engineering",
            "sales": "sales",
        }
        role = role_map.get(role, "other")
    influence = str(stakeholder.get("influence_on_purchase") or "unknown")
    company = (company or "").strip()
    relation: str = "works_at"
    if influence in {"high", "medium"} and role in {
        "purchasing",
        "production",
        "technology",
        "owner_exec",
    }:
        relation = "buys_for" if role == "purchasing" else "influences"
    emails: list[dict[str, Any]] = []
    phones: list[dict[str, Any]] = []
    if stakeholder.get("email"):
        emails.append(
            {
                "value": _norm_email(str(stakeholder["email"])),
                "verified": False,
                "status": "heuristic",
                "source": source,
            }
        )
    if stakeholder.get("phone"):
        phones.append(
            {
                "value": _clean_phone(str(stakeholder["phone"])),
                "verified": False,
                "status": "heuristic",
                "source": source,
            }
        )
    return Contact(
        id=contact_id_from_name(name, company),
        name=name,
        title=str(stakeholder.get("title") or "")[:120],
        role=role,
        emails=emails,
        phones=phones,
        linkedin_url=str(stakeholder.get("linkedin_url") or "")[:300],
        firm_links=[
            make_firm_link(
                company=company,
                relation=relation,
                influence=influence,
                is_primary=True,
                evidence=str(stakeholder.get("source") or source),
            )
        ],
        influence_on_purchase=influence if influence in {"high", "medium", "low", "unknown"} else "unknown",
        competitors_mentioned=list(competitors or [])[:10],
        sources=[source],
        verification={"status": "heuristic", "method": source},
        origin="prospect",
    )


def sync_contacts_from_profiles(data_dir: Path | str) -> dict[str, Any]:
    """Wyciągnij named stakeholders z firm_profiles (prospect) do contacts.json."""
    from market_agents.firm_profiles import FirmProfileRegistry

    data_dir = Path(data_dir)
    profiles = FirmProfileRegistry.load(data_dir)
    registry = ContactRegistry.load(data_dir)
    created = 0
    updated = 0
    skipped = 0
    for profile in profiles.profiles.values():
        prospect = profile.prospect or {}
        stakeholders = prospect.get("stakeholders") or []
        buys = prospect.get("buys_from") or []
        competitors = []
        for b in buys:
            if isinstance(b, dict) and b.get("brand"):
                competitors.append(str(b["brand"]))
            elif isinstance(b, str):
                competitors.append(b)
        for sh in stakeholders:
            if not isinstance(sh, dict):
                skipped += 1
                continue
            contact = contact_from_stakeholder(
                sh,
                company=profile.company,
                competitors=competitors,
                source="prospect_stakeholder",
            )
            if contact is None:
                skipped += 1
                continue
            _c, was_created = registry.upsert(contact)
            if was_created:
                created += 1
            else:
                updated += 1
    path = registry.save(data_dir)
    return {
        "ok": True,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "total": len(registry.contacts),
        "path": str(path),
    }


def upsert_manual_contact(
    data_dir: Path | str,
    *,
    name: str,
    company: str,
    title: str = "",
    role: str = "other",
    email: str = "",
    phone: str = "",
    influence: str = "unknown",
    relation: str = "works_at",
    notes: str = "",
) -> dict[str, Any]:
    registry = ContactRegistry.load(data_dir)
    emails = []
    phones = []
    if email:
        emails.append(
            {
                "value": _norm_email(email),
                "verified": False,
                "status": "manual",
                "source": "manual",
            }
        )
    if phone:
        phones.append(
            {
                "value": _clean_phone(phone),
                "verified": False,
                "status": "manual",
                "source": "manual",
            }
        )
    contact = Contact(
        id=contact_id_from_name(name, company),
        name=name.strip(),
        title=title[:120],
        role=role if role in CONTACT_ROLES else "other",
        emails=emails,
        phones=phones,
        firm_links=[
            make_firm_link(
                company=company,
                relation=relation if relation in FIRM_LINK_RELATIONS else "works_at",
                influence=influence,
                is_primary=True,
                evidence="manual",
            )
        ],
        influence_on_purchase=influence if influence in {"high", "medium", "low", "unknown"} else "unknown",
        sources=["manual"],
        verification={"status": "manual"},
        notes=notes[:2000],
        origin="manual",
    )
    saved, created = registry.upsert(contact)
    path = registry.save(data_dir)
    return {
        "ok": True,
        "created": created,
        "contact": saved.to_dict(),
        "path": str(path),
    }
