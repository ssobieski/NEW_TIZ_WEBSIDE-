"""
Deals + strategia sprzedażowa — pipeline od leadu do zamknięcia.

Deal łączy:
- firmę (prospect / customer)
- kontakty (buying center)
- business case (budżet, tooling, tender)
- kontekst konkurencji (od kogo kupują)
- strategię (approach, value prop, next actions)
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

DealStage = Literal[
    "discover",
    "qualify",
    "map_buying_center",
    "propose",
    "negotiate",
    "won",
    "lost",
    "on_hold",
]

DEAL_STAGES = (
    "discover",
    "qualify",
    "map_buying_center",
    "propose",
    "negotiate",
    "won",
    "lost",
    "on_hold",
)

STAGE_PROBABILITY = {
    "discover": 0.1,
    "qualify": 0.25,
    "map_buying_center": 0.4,
    "propose": 0.55,
    "negotiate": 0.7,
    "won": 1.0,
    "lost": 0.0,
    "on_hold": 0.15,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def deal_id_from_company(company: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (normalize_firm_name(company) or "deal")).strip("-")
    return f"deal-{(slug[:48] or 'x')}-{uuid.uuid4().hex[:6]}"


@dataclass
class Deal:
    id: str
    company: str
    firm_id: str = ""
    title: str = ""
    stage: str = "discover"
    contact_ids: list[str] = field(default_factory=list)
    primary_contact_id: str | None = None
    business_case: dict[str, Any] = field(default_factory=dict)
    competitive_context: dict[str, Any] = field(default_factory=dict)
    strategy: dict[str, Any] = field(default_factory=dict)
    value_eur_mid: float | None = None
    probability: float = 0.1
    opportunity_score: float | None = None
    crm_task_ids: list[str] = field(default_factory=list)
    scores: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    notes: str = ""
    notion_url: str | None = None
    outcome: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=utc_now_iso)
    created_at: str = field(default_factory=utc_now_iso)
    origin: str = "local"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Deal:
        company = str(raw.get("company") or "").strip()
        stage = str(raw.get("stage") or "discover")
        if stage not in DEAL_STAGES:
            stage = "discover"
        value = raw.get("value_eur_mid")
        opp = raw.get("opportunity_score")
        return cls(
            id=str(raw.get("id") or deal_id_from_company(company)),
            company=company,
            firm_id=str(raw.get("firm_id") or firm_id_from_name(company)),
            title=str(raw.get("title") or f"Deal: {company}")[:200],
            stage=stage,
            contact_ids=[str(x) for x in (raw.get("contact_ids") or [])][:30],
            primary_contact_id=raw.get("primary_contact_id"),
            business_case=dict(raw.get("business_case") or {}),
            competitive_context=dict(raw.get("competitive_context") or {}),
            strategy=dict(raw.get("strategy") or {}),
            value_eur_mid=float(value) if value is not None else None,
            probability=float(raw.get("probability") or STAGE_PROBABILITY.get(stage, 0.1)),
            opportunity_score=float(opp) if opp is not None else None,
            crm_task_ids=[str(x) for x in (raw.get("crm_task_ids") or [])][:20],
            scores=dict(raw.get("scores") or {}),
            sources=[str(s) for s in (raw.get("sources") or [])][:20],
            notes=str(raw.get("notes") or "")[:2000],
            notion_url=(str(raw["notion_url"]) if raw.get("notion_url") else None),
            outcome=dict(raw.get("outcome") or {}),
            updated_at=str(raw.get("updated_at") or utc_now_iso()),
            created_at=str(raw.get("created_at") or utc_now_iso()),
            origin=str(raw.get("origin") or "local"),
        )


class DealRegistry:
    def __init__(self, deals: list[Deal] | None = None) -> None:
        self.deals: list[Deal] = list(deals or [])

    @classmethod
    def load(cls, data_dir: Path | str | None = None) -> DealRegistry:
        path = Path(data_dir or "data") / "knowledge" / "deals.json"
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            rows = raw.get("deals") if isinstance(raw, dict) else raw
        except Exception:  # noqa: BLE001
            return cls()
        out: list[Deal] = []
        for r in rows or []:
            if not isinstance(r, dict) or not r.get("company"):
                continue
            try:
                out.append(Deal.from_dict(r))
            except Exception:  # noqa: BLE001
                continue
        return cls(out)

    def save(self, data_dir: Path | str | None = None) -> Path:
        data_dir = Path(data_dir or "data")
        path = data_dir / "knowledge" / "deals.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": utc_now_iso(),
            "count": len(self.deals),
            "deals": [d.to_dict() for d in self.deals],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def get(self, deal_id: str) -> Deal | None:
        for d in self.deals:
            if d.id == deal_id:
                return d
        return None

    def find_open_for_company(self, company: str) -> Deal | None:
        norm = normalize_firm_name(company)
        closed = {"won", "lost"}
        for d in self.deals:
            if normalize_firm_name(d.company) == norm and d.stage not in closed:
                return d
        return None

    def upsert(self, deal: Deal) -> tuple[Deal, bool]:
        existing = self.get(deal.id) or self.find_open_for_company(deal.company)
        if existing is None:
            self.deals.insert(0, deal)
            return deal, True
        # merge
        if deal.title and deal.title != existing.title:
            existing.title = deal.title
        if deal.stage and deal.stage != existing.stage:
            # don't regress from later open stages unless explicit won/lost
            order = {s: i for i, s in enumerate(DEAL_STAGES)}
            if deal.stage in {"won", "lost", "on_hold"} or order.get(deal.stage, 0) >= order.get(
                existing.stage, 0
            ):
                existing.stage = deal.stage
                existing.probability = STAGE_PROBABILITY.get(deal.stage, existing.probability)
        if deal.contact_ids:
            for cid in deal.contact_ids:
                if cid not in existing.contact_ids:
                    existing.contact_ids.append(cid)
            existing.contact_ids = existing.contact_ids[:30]
        if deal.primary_contact_id:
            existing.primary_contact_id = deal.primary_contact_id
        if deal.business_case:
            existing.business_case = {**(existing.business_case or {}), **deal.business_case}
        if deal.competitive_context:
            existing.competitive_context = {
                **(existing.competitive_context or {}),
                **deal.competitive_context,
            }
        if deal.strategy:
            existing.strategy = {**(existing.strategy or {}), **deal.strategy}
        if deal.value_eur_mid is not None:
            prev = existing.value_eur_mid
            if prev is None or float(deal.value_eur_mid) >= float(prev):
                existing.value_eur_mid = deal.value_eur_mid
        if deal.opportunity_score is not None:
            prev = existing.opportunity_score
            if prev is None or float(deal.opportunity_score) >= float(prev):
                existing.opportunity_score = deal.opportunity_score
        for tid in deal.crm_task_ids:
            if tid not in existing.crm_task_ids:
                existing.crm_task_ids.append(tid)
        for src in deal.sources:
            if src not in existing.sources:
                existing.sources.append(src)
        if deal.scores:
            existing.scores = {**(existing.scores or {}), **deal.scores}
        if deal.notion_url and not existing.notion_url:
            existing.notion_url = deal.notion_url
        if deal.outcome:
            existing.outcome = {**(existing.outcome or {}), **deal.outcome}
        if deal.notes:
            if deal.notes[:60] not in (existing.notes or ""):
                existing.notes = ((existing.notes or "") + " | " + deal.notes).strip(" |")[:2000]
        existing.updated_at = utc_now_iso()
        return existing, False

    def list(
        self,
        *,
        stage: str | None = None,
        company: str | None = None,
        q: str | None = None,
        limit: int = 50,
    ) -> list[Deal]:
        rows = self.deals
        if stage:
            rows = [d for d in rows if d.stage == stage]
        if company:
            c = company.lower()
            rows = [d for d in rows if c in d.company.lower()]
        if q:
            ql = q.lower()
            rows = [
                d
                for d in rows
                if ql in d.company.lower() or ql in (d.title or "").lower()
            ]
        return rows[:limit]

    def pipeline(self, *, limit: int = 40) -> list[dict[str, Any]]:
        rows = []
        for d in self.deals:
            if d.stage in {"won", "lost"}:
                continue
            rows.append(
                {
                    "id": d.id,
                    "company": d.company,
                    "stage": d.stage,
                    "title": d.title,
                    "value_eur_mid": d.value_eur_mid,
                    "probability": d.probability,
                    "expected_value_eur": round(
                        float(d.value_eur_mid or 0) * float(d.probability or 0), 2
                    ),
                    "opportunity_score": d.opportunity_score,
                    "contacts": len(d.contact_ids),
                    "primary_contact_id": d.primary_contact_id,
                    "next_action": ((d.strategy or {}).get("next_actions") or [None])[0],
                    "competitors": (d.competitive_context or {}).get("incumbents") or [],
                    "updated_at": d.updated_at,
                }
            )
        rows.sort(
            key=lambda r: (
                float(r.get("opportunity_score") or 0),
                float(r.get("expected_value_eur") or 0),
            ),
            reverse=True,
        )
        return rows[:limit]

    def set_stage(self, deal_id: str, stage: str) -> Deal | None:
        if stage not in DEAL_STAGES:
            return None
        deal = self.get(deal_id)
        if not deal:
            return None
        deal.stage = stage
        deal.probability = STAGE_PROBABILITY.get(stage, deal.probability)
        deal.updated_at = utc_now_iso()
        return deal


def build_deal_strategy(
    *,
    company: str,
    prospect: dict[str, Any] | None = None,
    contacts: list[dict[str, Any]] | None = None,
    competitors_industry: list[str] | None = None,
    our_brand: str = "TIZ",
) -> dict[str, Any]:
    """Deterministyczna strategia dealu (bez LLM) — agent może ją później wzbogacić."""
    prospect = prospect or {}
    contacts = contacts or []
    vertical = str(prospect.get("vertical") or "unknown")
    quality = str(prospect.get("quality_tier") or "unknown")
    budget = prospect.get("budget") or {}
    product_map = prospect.get("product_map") or {}
    buys_from = prospect.get("buys_from") or []
    tooling = prospect.get("tooling_inference") or product_map.get("tooling") or {}

    incumbents: list[str] = []
    for b in buys_from:
        if isinstance(b, dict) and b.get("brand"):
            incumbents.append(str(b["brand"]))
        elif isinstance(b, str):
            incumbents.append(b)
    incumbents = list(dict.fromkeys(incumbents))[:12]

    families = list(product_map.get("product_families") or tooling.get("product_families") or [])[
        :12
    ]
    processes = list(product_map.get("likely_processes") or tooling.get("processes") or [])[:12]
    practical = list(
        product_map.get("practical_tools") or tooling.get("practical_tools") or []
    )[:15]

    # Buying center map
    buying_center = []
    primary = None
    influence_rank = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
    ranked_contacts = sorted(
        contacts,
        key=lambda c: influence_rank.get(str(c.get("influence_on_purchase") or "unknown"), 0),
        reverse=True,
    )
    for c in ranked_contacts[:8]:
        entry = {
            "contact_id": c.get("id"),
            "name": c.get("name"),
            "title": c.get("title"),
            "role": c.get("role"),
            "influence_on_purchase": c.get("influence_on_purchase"),
            "approach": _approach_for_role(str(c.get("role") or "other")),
        }
        buying_center.append(entry)
        if primary is None and c.get("influence_on_purchase") in {"high", "medium"}:
            primary = c

    # Value prop & win themes
    win_themes: list[str] = []
    if practical:
        win_themes.append(
            f"Dopasowanie tooling do procesu: {', '.join(str(x) for x in practical[:4])}"
        )
    if families:
        win_themes.append(f"Rodziny produktów klienta: {', '.join(str(x) for x in families[:4])}")
    if quality in {"premium", "mid"}:
        win_themes.append(f"Park maszyn {quality} — argument jakości/powtarzalności vs cena")
    if incumbents:
        win_themes.append(
            f"Incumbent: {', '.join(incumbents[:3])} — pozycjonuj jako second source / TCO"
        )
    else:
        win_themes.append("Brak jasnego incumbenta — wejdź przez próbkę / trial tooling")

    risks: list[str] = []
    if not contacts:
        risks.append("Brak zmapowanego buying center — najpierw znaleźć decydenta zakupów/produkcji")
    if not practical and not processes:
        risks.append("Słaba mapa procesów — uzupełnić z WWW / filmu / wizyty")
    if not budget.get("mid_eur") and not budget.get("tooling_budget_mid_eur"):
        risks.append("Brak szacunku budżetu — kwalifikacja wartości dealu niepewna")
    if competitors_industry:
        overlap = [c for c in incumbents if any(c.lower() in i.lower() or i.lower() in c.lower() for i in competitors_industry)]
        if overlap:
            risks.append(f"Silna obecność konkurencji katalogowej: {', '.join(overlap[:4])}")

    next_actions: list[str] = []
    if not contacts:
        next_actions.append("Zidentyfikuj Purchasing / Production / Technology na stronie Kontakt / LinkedIn publicznym")
    else:
        target = primary or ranked_contacts[0]
        next_actions.append(
            f"Outreach do {target.get('name')} ({target.get('role')}) — {_approach_for_role(str(target.get('role') or 'other'))}"
        )
    if practical:
        next_actions.append(
            f"Przygotuj ofertę próbki / case study pod: {', '.join(str(x) for x in practical[:3])}"
        )
    if incumbents:
        next_actions.append(
            f"Zbierz win-loss vs {incumbents[0]}: cena, dostępność, support, lead time"
        )
    next_actions.append("Utwórz/aktualizuj zadanie CRM i umów discovery call (15–20 min)")

    outreach = [
        {
            "step": 1,
            "channel": "email" if any(c.get("emails") for c in contacts) else "phone_or_form",
            "goal": "otwarcie — problem procesu / tooling cost",
            "talking_points": win_themes[:2],
        },
        {
            "step": 2,
            "channel": "call",
            "goal": "mapa buying center + incumbent + okno budżetowe",
            "talking_points": [
                "Kto podpisuje zakupy tooling?",
                "Jakie maszyny / materiały dominują?",
                f"Czy {our_brand} może wejść jako second source?",
            ],
        },
        {
            "step": 3,
            "channel": "meeting",
            "goal": "propozycja wartości + próbka",
            "talking_points": practical[:3] or processes[:3] or ["audit tooling"],
        },
    ]

    mid = budget.get("mid_eur") or budget.get("tooling_budget_mid_eur")
    try:
        mid_f = float(mid) if mid is not None else None
    except (TypeError, ValueError):
        mid_f = None

    return {
        "approach": _account_approach(vertical, quality, incumbents),
        "value_proposition": (
            f"{our_brand}: tooling dopasowany do {vertical} / {quality} park — "
            f"fokus na {', '.join(str(x) for x in (practical or families or processes)[:3]) or 'proces skrawania'}."
        ),
        "win_themes": win_themes[:6],
        "risks": risks[:6],
        "buying_center": buying_center,
        "primary_contact_id": (primary or {}).get("id") if primary else None,
        "next_actions": next_actions[:6],
        "outreach_sequence": outreach,
        "suggested_stage": _suggest_stage(contacts, budget, practical or processes),
        "value_eur_mid": mid_f,
        "vertical": vertical,
        "quality_tier": quality,
        "generated_at": utc_now_iso(),
        "generator": "deterministic_v1",
    }


def _approach_for_role(role: str) -> str:
    return {
        "purchasing": "TCO, dostępność, second source, warunki handlowe",
        "production": "wydajność, trwałość narzędzia, przestój wrzeciona",
        "technology": "parametry skrawania, materiał ISO, proces",
        "quality": "powtarzalność, tolerancje, scrap rate",
        "owner_exec": "marża, ryzyko dostaw, partnerstwo strategiczne",
        "engineering": "geometria narzędzia, uchwyty, CAM",
        "sales": "referencje klientów / case study branżowe",
        "other": "discovery — zrozumieć rolę w zakupie",
    }.get(role, "discovery — zrozumieć rolę w zakupie")


def _account_approach(vertical: str, quality: str, incumbents: list[str]) -> str:
    if incumbents:
        return (
            f"Displace/augment: wejdź obok {incumbents[0]} przez próbę na krytycznym procesie "
            f"({vertical}, park {quality})."
        )
    return f"Land & expand: zakwalifikuj {vertical}, wygraj pierwszą linię tooling, potem asortyment."


def _suggest_stage(
    contacts: list[dict[str, Any]],
    budget: dict[str, Any],
    signals: list[Any],
) -> str:
    has_people = bool(contacts)
    has_budget = bool(budget.get("mid_eur") or budget.get("tooling_budget_mid_eur"))
    has_signals = bool(signals)
    if has_people and has_budget and has_signals:
        return "map_buying_center" if len(contacts) >= 2 else "propose"
    if has_signals or has_budget:
        return "qualify"
    return "discover"


def sync_deals_from_prospects(
    data_dir: Path | str,
    *,
    min_opportunity: float = 50.0,
    create_crm: bool = True,
    our_brand: str = "TIZ",
    competitors_industry: list[str] | None = None,
) -> dict[str, Any]:
    """Zbuduj/odśwież deale z prospect scoreboard + contacts + strategia."""
    from market_agents.contacts import ContactRegistry, sync_contacts_from_profiles
    from market_agents.crm_tasks import CrmTaskStore
    from market_agents.firm_profiles import FirmProfileRegistry

    data_dir = Path(data_dir)
    # ensure contacts exist first
    contact_sync = sync_contacts_from_profiles(data_dir)
    contacts_reg = ContactRegistry.load(data_dir)
    profiles = FirmProfileRegistry.load(data_dir)
    deals = DealRegistry.load(data_dir)
    crm = CrmTaskStore.load(data_dir) if create_crm else None

    created = 0
    updated = 0
    for profile in profiles.profiles.values():
        roles = set(profile.roles or [])
        prospect = profile.prospect or {}
        if "prospect" not in roles and not prospect:
            continue
        opp = ((prospect.get("opportunity") or {}) if isinstance(prospect, dict) else {}).get(
            "overall"
        )
        try:
            opp_f = float(opp) if opp is not None else 0.0
        except (TypeError, ValueError):
            opp_f = 0.0
        if opp_f < min_opportunity and not prospect.get("tenders"):
            # still allow if tender signals present inside prospect meta
            continue

        firm_contacts = [c.to_dict() for c in contacts_reg.for_firm(profile.company)]
        strategy = build_deal_strategy(
            company=profile.company,
            prospect=prospect,
            contacts=firm_contacts,
            competitors_industry=competitors_industry,
            our_brand=our_brand,
        )
        stage = strategy.get("suggested_stage") or "qualify"
        budget = prospect.get("budget") or {}
        value = strategy.get("value_eur_mid")
        if value is None:
            value = budget.get("mid_eur") or budget.get("tooling_budget_mid_eur")
        try:
            value_f = float(value) if value is not None else None
        except (TypeError, ValueError):
            value_f = None

        incumbents = []
        for b in prospect.get("buys_from") or []:
            if isinstance(b, dict) and b.get("brand"):
                incumbents.append(str(b["brand"]))
            elif isinstance(b, str):
                incumbents.append(b)

        deal = Deal(
            id=deal_id_from_company(profile.company),
            company=profile.company,
            firm_id=profile.id or firm_id_from_name(profile.company),
            title=f"Tooling opportunity: {profile.company}",
            stage=str(stage),
            contact_ids=[str(c["id"]) for c in firm_contacts if c.get("id")],
            primary_contact_id=strategy.get("primary_contact_id"),
            business_case={
                "vertical": prospect.get("vertical"),
                "quality_tier": prospect.get("quality_tier"),
                "budget": budget,
                "product_families": (prospect.get("product_map") or {}).get("product_families")
                or [],
                "processes": (prospect.get("product_map") or {}).get("likely_processes") or [],
                "practical_tools": (prospect.get("product_map") or {}).get("practical_tools")
                or [],
                "opportunity": prospect.get("opportunity"),
            },
            competitive_context={
                "incumbents": incumbents[:12],
                "industry_competitors": list(competitors_industry or [])[:20],
                "positioning": strategy.get("approach"),
            },
            strategy=strategy,
            value_eur_mid=value_f,
            probability=STAGE_PROBABILITY.get(str(stage), 0.25),
            opportunity_score=opp_f or None,
            sources=["prospect_sync"],
            scores={"opportunity": opp_f},
            origin="prospect",
        )

        if crm is not None:
            task, _ = crm.create(
                company=profile.company,
                title=f"Deal: {profile.company}",
                reason=str((strategy.get("next_actions") or ["follow-up"])[0]),
                opportunity_score=opp_f or None,
                source="deal",
                dedupe_by_company=True,
                meta={
                    "deal": {
                        "stage": stage,
                        "value_eur_mid": value_f,
                        "primary_contact_id": strategy.get("primary_contact_id"),
                    }
                },
            )
            deal.crm_task_ids = [task.id]

        saved, was_created = deals.upsert(deal)
        if was_created:
            created += 1
        else:
            updated += 1
        _ = saved

    path = deals.save(data_dir)
    crm_path = None
    if crm is not None:
        crm_path = str(crm.save(data_dir))
    return {
        "ok": True,
        "created": created,
        "updated": updated,
        "total": len(deals.deals),
        "contacts_sync": contact_sync,
        "path": str(path),
        "crm_path": crm_path,
        "pipeline": deals.pipeline(limit=15),
    }


def get_deal_strategy(data_dir: Path | str, company_or_id: str) -> dict[str, Any]:
    """Pobierz deal + odśwież strategię z aktualnych kontaktów/profilu."""
    from market_agents.contacts import ContactRegistry
    from market_agents.firm_profiles import FirmProfileRegistry

    data_dir = Path(data_dir)
    deals = DealRegistry.load(data_dir)
    deal = deals.get(company_or_id)
    if deal is None:
        for d in deals.deals:
            if company_or_id.lower() in d.company.lower() or d.firm_id == company_or_id:
                deal = d
                break
    profiles = FirmProfileRegistry.load(data_dir)
    profile = None
    for p in profiles.profiles.values():
        if deal and (p.id == deal.firm_id or normalize_firm_name(p.company) == normalize_firm_name(deal.company)):
            profile = p
            break
        if not deal and (
            p.id == company_or_id or company_or_id.lower() in p.company.lower()
        ):
            profile = p
            break
    if deal is None and profile is None:
        return {"ok": False, "error": f"Brak dealu/profilu: {company_or_id}"}

    company = (deal.company if deal else profile.company)  # type: ignore[union-attr]
    contacts = [c.to_dict() for c in ContactRegistry.load(data_dir).for_firm(company)]
    prospect = (profile.prospect if profile else {}) or {}
    strategy = build_deal_strategy(
        company=company,
        prospect=prospect,
        contacts=contacts,
    )
    if deal is None:
        deal = Deal(
            id=deal_id_from_company(company),
            company=company,
            firm_id=profile.id if profile else firm_id_from_name(company),
            title=f"Tooling opportunity: {company}",
            stage=str(strategy.get("suggested_stage") or "discover"),
            contact_ids=[str(c["id"]) for c in contacts if c.get("id")],
            primary_contact_id=strategy.get("primary_contact_id"),
            business_case={"vertical": prospect.get("vertical"), "budget": prospect.get("budget")},
            competitive_context={
                "incumbents": [
                    str(b.get("brand") if isinstance(b, dict) else b)
                    for b in (prospect.get("buys_from") or [])
                ][:12]
            },
            strategy=strategy,
            value_eur_mid=strategy.get("value_eur_mid"),
            probability=STAGE_PROBABILITY.get(str(strategy.get("suggested_stage") or "discover"), 0.1),
            sources=["on_demand"],
        )
        deals.upsert(deal)
    else:
        deal.strategy = strategy
        deal.contact_ids = [str(c["id"]) for c in contacts if c.get("id")] or deal.contact_ids
        deal.primary_contact_id = strategy.get("primary_contact_id") or deal.primary_contact_id
        if strategy.get("value_eur_mid") is not None:
            deal.value_eur_mid = strategy["value_eur_mid"]
        deal.updated_at = utc_now_iso()
        deals.upsert(deal)
    path = deals.save(data_dir)
    return {
        "ok": True,
        "deal": deal.to_dict(),
        "strategy": strategy,
        "contacts": contacts,
        "path": str(path),
    }


def record_deal_outcome(
    data_dir: Path | str,
    *,
    deal_id: str,
    result: str,
    competitor: str = "",
    reason: str = "",
    value_eur: float | None = None,
    lessons: str = "",
) -> dict[str, Any]:
    """Zapisz win/loss i ustaw stage won|lost."""
    result_norm = (result or "").strip().lower()
    if result_norm not in {"won", "lost"}:
        return {"ok": False, "error": "result must be won|lost"}
    reg = DealRegistry.load(data_dir)
    deal = reg.get(deal_id)
    if deal is None:
        # allow company match
        deal = reg.find_open_for_company(deal_id)
        if deal is None:
            for d in reg.deals:
                if deal_id.lower() in d.company.lower():
                    deal = d
                    break
    if deal is None:
        return {"ok": False, "error": f"deal not found: {deal_id}"}

    incumbents = list((deal.competitive_context or {}).get("incumbents") or [])
    if competitor and competitor not in incumbents:
        incumbents.append(competitor)

    deal.stage = result_norm
    deal.probability = STAGE_PROBABILITY.get(result_norm, 0.0)
    deal.outcome = {
        "result": result_norm,
        "competitor": competitor or (incumbents[0] if incumbents else ""),
        "reason": (reason or "")[:800],
        "lessons": (lessons or "")[:1200],
        "value_eur": float(value_eur) if value_eur is not None else deal.value_eur_mid,
        "closed_at": utc_now_iso(),
    }
    if value_eur is not None:
        deal.value_eur_mid = float(value_eur)
    deal.competitive_context = {
        **(deal.competitive_context or {}),
        "incumbents": incumbents[:12],
        "outcome_competitor": competitor or "",
    }
    deal.updated_at = utc_now_iso()
    path = reg.save(data_dir)
    return {"ok": True, "deal": deal.to_dict(), "path": str(path)}


def win_loss_report(data_dir: Path | str, *, limit: int = 40) -> dict[str, Any]:
    """Agregat win/loss vs konkurencja."""
    reg = DealRegistry.load(data_dir)
    closed = [d for d in reg.deals if d.stage in {"won", "lost"} or (d.outcome or {}).get("result")]
    by_competitor: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    won = lost = 0
    for d in closed:
        outcome = d.outcome or {}
        result = str(outcome.get("result") or d.stage)
        if result == "won":
            won += 1
        elif result == "lost":
            lost += 1
        comp = str(outcome.get("competitor") or "").strip() or "unknown"
        bucket = by_competitor.setdefault(comp, {"competitor": comp, "won": 0, "lost": 0, "deals": []})
        if result == "won":
            bucket["won"] += 1
        elif result == "lost":
            bucket["lost"] += 1
        row = {
            "id": d.id,
            "company": d.company,
            "result": result,
            "competitor": comp,
            "reason": outcome.get("reason") or "",
            "value_eur": outcome.get("value_eur") or d.value_eur_mid,
            "closed_at": outcome.get("closed_at") or d.updated_at,
        }
        bucket["deals"].append(row["company"])
        rows.append(row)
    rows.sort(key=lambda r: str(r.get("closed_at") or ""), reverse=True)
    competitors = sorted(
        by_competitor.values(),
        key=lambda b: (b["lost"] + b["won"], b["lost"]),
        reverse=True,
    )
    for b in competitors:
        b["deals"] = b["deals"][:8]
        total = b["won"] + b["lost"]
        b["win_rate"] = round(b["won"] / total, 2) if total else None
    return {
        "ok": True,
        "won": won,
        "lost": lost,
        "win_rate": round(won / (won + lost), 2) if (won + lost) else None,
        "by_competitor": competitors[:20],
        "recent": rows[:limit],
    }


def push_deals_to_notion(
    config: Any,
    *,
    limit: int = 20,
    open_only: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Push dealów bez notion_url do Notion (dzieci deals_parent_page)."""
    from market_agents.collectors.notion import NotionCollector

    notion_cfg = getattr(getattr(config, "sources", None), "notion", None)
    if notion_cfg is None or not getattr(notion_cfg, "enabled", False):
        return {"ok": False, "error": "Notion disabled — włącz sources.notion.enabled"}
    parent = getattr(notion_cfg, "deals_parent_page", None)
    if not parent:
        return {"ok": False, "error": "Brak sources.notion.deals_parent_page w config"}

    data_dir = getattr(config, "data_path", Path("data"))
    reg = DealRegistry.load(data_dir)
    rows = [d for d in reg.deals if not d.notion_url]
    if open_only:
        rows = [d for d in rows if d.stage not in {"won", "lost"}]
    rows = rows[:limit]

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "would_push": len(rows),
            "deals": [{"id": d.id, "company": d.company, "stage": d.stage} for d in rows],
        }

    token_fn = getattr(config, "notion_token", None)
    token = token_fn() if callable(token_fn) else None
    if not token:
        return {"ok": False, "error": f"Brak tokenu Notion ({notion_cfg.token_env})"}

    collector = NotionCollector(notion_cfg, token)
    pushed = 0
    errors: list[str] = []
    for d in rows:
        try:
            strat = d.strategy or {}
            next_actions = strat.get("next_actions") or []
            body = [
                f"Company: {d.company}",
                f"Stage: {d.stage}",
                f"Opportunity: {d.opportunity_score}",
                f"Value EUR mid: {d.value_eur_mid}",
                f"Probability: {d.probability}",
                f"Incumbents: {', '.join((d.competitive_context or {}).get('incumbents') or [])}",
                f"Value prop: {strat.get('value_proposition') or '—'}",
                f"Next: {(next_actions[0] if next_actions else '—')}",
                f"Win themes: {'; '.join(strat.get('win_themes') or [])}",
                f"Local deal id: {d.id}",
            ]
            created = collector.create_child_page(
                parent_page_id_or_url=str(parent),
                title=d.title or f"Deal: {d.company}",
                body_lines=body,
            )
            d.notion_url = created.get("url")
            d.scores = dict(d.scores or {})
            d.scores["notion_id"] = created.get("id")
            d.updated_at = utc_now_iso()
            reg.save(data_dir)
            pushed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{d.id}:{exc}"[:160])
    path = reg.save(data_dir)
    return {
        "ok": pushed > 0 or not errors,
        "pushed": pushed,
        "errors": errors[:10],
        "path": str(path),
        "with_notion": sum(1 for d in reg.deals if d.notion_url),
    }


def enrich_deal_strategy_with_llm(
    data_dir: Path | str,
    company_or_id: str,
    *,
    llm: Any | None = None,
    our_brand: str = "TIZ",
) -> dict[str, Any]:
    """
    Wzbogaca deterministyczną strategię tekstem LLM (pitch, objection handling).
    Bez LLM / przy błędzie — zwraca bazową strategię bez zmian krytycznych.
    """
    base = get_deal_strategy(data_dir, company_or_id)
    if not base.get("ok"):
        return base
    strategy = dict(base.get("strategy") or {})
    deal = base.get("deal") or {}
    contacts = base.get("contacts") or []

    if llm is None:
        strategy["llm_enrichment"] = {
            "status": "skipped",
            "reason": "no_llm",
        }
        return {**base, "strategy": strategy, "llm_enriched": False}

    system = (
        f"Jesteś doradcą sprzedaży narzędzi skrawających dla marki {our_brand}. "
        "Odpowiadasz po polsku, konkretnie, bez ozdobników. "
        "Zwróć JSON z polami: pitch (string), objection_handling (lista string), "
        "discovery_questions (lista string), email_opener (string)."
    )
    user = json.dumps(
        {
            "company": deal.get("company"),
            "stage": deal.get("stage"),
            "business_case": deal.get("business_case"),
            "competitive_context": deal.get("competitive_context"),
            "strategy": {
                "value_proposition": strategy.get("value_proposition"),
                "win_themes": strategy.get("win_themes"),
                "risks": strategy.get("risks"),
                "next_actions": strategy.get("next_actions"),
            },
            "buying_center": [
                {
                    "name": c.get("name"),
                    "role": c.get("role"),
                    "influence_on_purchase": c.get("influence_on_purchase"),
                }
                for c in contacts[:6]
            ],
        },
        ensure_ascii=False,
    )
    try:
        raw = llm.chat(system, user)
        parsed = _parse_llm_json(raw)
        strategy["llm_enrichment"] = {
            "status": "ok",
            "pitch": str(parsed.get("pitch") or "")[:1200],
            "objection_handling": [
                str(x)[:300] for x in (parsed.get("objection_handling") or [])[:8]
            ],
            "discovery_questions": [
                str(x)[:300] for x in (parsed.get("discovery_questions") or [])[:8]
            ],
            "email_opener": str(parsed.get("email_opener") or "")[:800],
            "generated_at": utc_now_iso(),
        }
        # fold pitch into value prop appendix when useful
        if strategy["llm_enrichment"]["pitch"]:
            strategy["value_proposition_llm"] = strategy["llm_enrichment"]["pitch"]
        llm_ok = True
    except Exception as exc:  # noqa: BLE001
        strategy["llm_enrichment"] = {
            "status": "error",
            "reason": str(exc)[:200],
        }
        llm_ok = False

    # persist
    deals = DealRegistry.load(data_dir)
    d = deals.get(str(deal.get("id") or ""))
    if d is None and deal.get("company"):
        d = deals.find_open_for_company(str(deal["company"]))
    if d is not None:
        d.strategy = strategy
        d.updated_at = utc_now_iso()
        deals.upsert(d)
        path = deals.save(data_dir)
    else:
        path = None
    return {
        **base,
        "ok": True,
        "strategy": strategy,
        "llm_enriched": llm_ok,
        "path": str(path) if path else base.get("path"),
    }


def _parse_llm_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    # strip markdown fences
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}
