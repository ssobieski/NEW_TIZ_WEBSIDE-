"""
Potencjalni klienci (prospects) — intelligence zakupowy dla TIZ.

Zakłada sprzedaż narzędzi/usług skrawania do firm produkcyjnych.
Buduje:
- ocenę dopasowania (opportunity scorecard)
- szacunek budżetu na narzędzia (4–10% kosztów produkcji wg branży)
- od kogo kupuje (marki narzędzi / dystrybutorzy na stronie)
- poziom jakości parku maszynowego
- mapę produktów klienta → procesy / rodziny narzędzi / materiały
- stakeholders wpływających na zakup (zakupy, produkcja, technologia)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from market_agents.firm_profiles import (
    FirmProfile,
    FirmProfileRegistry,
    extract_contacts_from_html,
    firm_id_from_name,
    utc_now_iso,
)
from market_agents.firms import normalize_firm_name

# Udział wydatków na narzędzia produkcyjne / tooling w kosztach (założenie branżowe)
DEFAULT_VERTICALS: dict[str, dict[str, Any]] = {
    "automotive": {
        "label": "Motoryzacja / automotive",
        "tooling_pct_min": 0.04,
        "tooling_pct_max": 0.08,
        "notes": "Wysoki wolumen, silna presja kosztowa; typowo 4–8% kosztów produkcji na tooling",
    },
    "aerospace": {
        "label": "Lotnictwo / aerospace",
        "tooling_pct_min": 0.06,
        "tooling_pct_max": 0.10,
        "notes": "Trudne materiały (ISO S), droższe narzędzia; 6–10%",
    },
    "medical": {
        "label": "Medycyna / implanty",
        "tooling_pct_min": 0.05,
        "tooling_pct_max": 0.10,
        "notes": "Precyzja + trudne stopy; 5–10%",
    },
    "mold_die": {
        "label": "Formy / matryce / mold & die",
        "tooling_pct_min": 0.05,
        "tooling_pct_max": 0.09,
        "notes": "HSM, węgliki, elektrody; 5–9%",
    },
    "energy": {
        "label": "Energetyka / oil & gas",
        "tooling_pct_min": 0.05,
        "tooling_pct_max": 0.09,
        "notes": "Duże detale, trudne stopy; 5–9%",
    },
    "general_machining": {
        "label": "Obróbka ogólna / job shop",
        "tooling_pct_min": 0.04,
        "tooling_pct_max": 0.07,
        "notes": "Zróżnicowany mix; typowo 4–7%",
    },
    "electronics": {
        "label": "Elektronika / precyzja",
        "tooling_pct_min": 0.04,
        "tooling_pct_max": 0.08,
        "notes": "Mikro-narzędzia, wysoka rotacja; 4–8%",
    },
    "unknown": {
        "label": "Nieznana / ogólna produkcja",
        "tooling_pct_min": 0.04,
        "tooling_pct_max": 0.10,
        "notes": "Domyślny widełki 4–10% kosztów produkcji na narzędzia",
    },
}

_VERTICAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "automotive": (
        "automotive",
        "motoryzac",
        "oem",
        "tier 1",
        "tier-1",
        "powertrain",
        "silnik",
        "chassis",
        "ev battery",
    ),
    "aerospace": (
        "aerospace",
        "lotnict",
        "aircraft",
        "aviacja",
        "turbine",
        "silnik lotniczy",
        "landing gear",
    ),
    "medical": (
        "medical",
        "implant",
        "surgical",
        "ortoped",
        "dental",
        "medyczn",
    ),
    "mold_die": (
        "mold",
        "mould",
        "die",
        "formy wtrysk",
        "matryc",
        "narzędziownia",
        "tool & die",
    ),
    "energy": (
        "oil & gas",
        "offshore",
        "wind turbine",
        "energetyk",
        "powergen",
        "nuclear",
    ),
    "electronics": (
        "electronics",
        "semiconductor",
        "pcb",
        "connector",
        "elektronik",
    ),
    "general_machining": (
        "cnc",
        "machining",
        "skrawanie",
        "toczenie",
        "frezowanie",
        "job shop",
        "obróbka",
    ),
}

# Park maszynowy → poziom jakości / budżetu
MACHINE_TIERS: dict[str, str] = {
    # premium
    "dmg mori": "premium",
    "dmg": "premium",
    "mazak": "premium",
    "okuma": "premium",
    "makino": "premium",
    "hermle": "premium",
    "grohmann": "premium",
    "gf machining": "premium",
    "agiecharmilles": "premium",
    "mikron": "premium",
    "index": "premium",
    "trautwein": "premium",
    "studer": "premium",
    # mid
    "haas": "mid",
    "doosan": "mid",
    "dn solutions": "mid",
    "hyundai wia": "mid",
    "brother": "mid",
    "hardinge": "mid",
    "emco": "mid",
    "spinner": "mid",
    "goodway": "mid",
    "takisawa": "mid",
    "kitamura": "mid",
    # economy / entry
    "syil": "economy",
    "tormach": "economy",
    "optimum": "economy",
    "weiler": "mid",
}

TOOL_BRANDS = (
    "sandvik coromant",
    "sandvik",
    "seco",
    "walter",
    "kennametal",
    "iscar",
    "mapal",
    "guhring",
    "guehring",
    "osg",
    "mitsubishi",
    "tungaloy",
    "kyocera",
    "ceratizit",
    "dormer",
    "pramet",
    "fraisa",
    "emuge",
    "franken",
    "hoffmann",
    "garant",
    "holex",
    "big daishowa",
    "haimer",
    "korloy",
    "yg-1",
    "nachi",
    "sumitomo",
    "ingersoll",
    "widia",
    "horn",
    "paul horn",
)

PROCESS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "turning": ("toczenie", "turning", "lathe", "tokarka", "swiss", "longitudinal"),
    "milling": ("frezowanie", "milling", "machining center", "centrum obróbcze", "5-axis", "5 axis"),
    "drilling": ("wiercenie", "drilling", "deep hole", "głębokie otwory"),
    "threading": ("gwintowanie", "tapping", "threading", "gwint"),
    "grinding": ("szlifowanie", "grinding", "hone"),
    "edm": ("edm", "drążenie", "elektroeroz", "wire edm"),
    "hsm": ("hsm", "high speed machining", "wysokowydajne"),
}

MATERIAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "iso-p": ("stal", "steel", "carbon steel", "alloy steel"),
    "iso-m": ("stal nierdzew", "stainless", "inox", "austenit"),
    "iso-k": ("żeliwo", "cast iron", "ggi", "gjs"),
    "iso-n": ("aluminium", "aluminum", "brass", "copper", "miedź", "mosiądz"),
    "iso-s": ("tytan", "titanium", "inconel", "hastelloy", "nickel", "superalloy", "hrsa"),
    "iso-h": ("hartowan", "hardened", "hrc", "tool steel"),
}

TOOL_FAMILY_FROM_PROCESS: dict[str, list[str]] = {
    "turning": ["turning_inserts", "grooving", "parting"],
    "milling": ["end_mills", "face_mills", "indexable_milling"],
    "drilling": ["solid_carbide_drills", "indexable_drills", "deep_hole"],
    "threading": ["taps", "thread_mills", "thread_turning"],
    "grinding": ["grinding_wheels"],
    "edm": ["edm_electrodes"],
    "hsm": ["solid_carbide_end_mills", "hsm_toolholders"],
}

STAKEHOLDER_PATTERNS: list[tuple[str, str, str]] = [
    # (regex, role, influence)
    (r"(?i)\b(?:purchasing|zakup|procurement|buyer)\s+(?:manager|director|kierownik|dyrektor)\b", "purchasing", "high"),
    (r"(?i)\b(?:kierownik|dyrektor)\s+(?:zakup|zaopatrzenia)\b", "purchasing", "high"),
    (r"(?i)\b(?:production|produkcj)\s+(?:manager|director|kierownik|dyrektor)\b", "production", "high"),
    (r"(?i)\b(?:kierownik|dyrektor)\s+produkcji\b", "production", "high"),
    (r"(?i)\b(?:technical|technolog(?:ia|iczny)?|process)\s+(?:manager|director|kierownik|engineer|inżynier)\b", "engineering", "high"),
    (r"(?i)\b(?:chief|główny)\s+(?:technolog|engineer|inżynier)\b", "engineering", "high"),
    (r"(?i)\b(?:plant|zakład(?:u)?)\s+manager\b", "operations", "high"),
    (r"(?i)\b(?:ceo|prezes|owner|właściciel|managing director|dyrektor zarządzający)\b", "executive", "high"),
    (r"(?i)\b(?:cfo|dyrektor finansowy)\b", "finance", "medium"),
    (r"(?i)\b(?:quality|jakość|qa)\s+(?:manager|kierownik)\b", "quality", "medium"),
    (r"(?i)\b(?:maintenance|utrzymanie ruchu)\s+(?:manager|kierownik)\b", "maintenance", "medium"),
    (r"(?i)\b(?:cnc\s+)?(?:programmer|programista)\b", "engineering", "medium"),
    (r"(?i)\b(?:tool(?:ing)?\s+crib|magazyn narzędzi)\b", "tooling", "medium"),
]

_EMPLOYEE_COUNT_RE = re.compile(
    r"(?i)(?:employ(?:ee)?s?|pracownik(?:ów|i)?|beschäftigte|mitarbeiter)\D{0,20}(\d[\d\s.,]{0,12})"
)
_REVENUE_NUM_RE = re.compile(
    r"(?i)(?:revenue|turnover|sprzedaż|obrót|umsatz|net\s+sales)"
    r".{0,40}?"
    r"((?:[€$£]|EUR|USD|PLN|SEK|CHF)?\s?\d[\d\s.,]*)\s*"
    r"(mrd|mld|bn|billion|mln|million|m\.?|tys\.?|k)?"
    r"(?:\s*(EUR|USD|PLN|SEK|CHF))?"
)


def load_vertical_assumptions(path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    path = Path(path or "config/prospect_assumptions.json")
    data = dict(DEFAULT_VERTICALS)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            verts = raw.get("verticals") if isinstance(raw, dict) else None
            if isinstance(verts, dict):
                for k, v in verts.items():
                    if isinstance(v, dict):
                        data[str(k)] = {**data.get(str(k), {}), **v}
        except Exception:  # noqa: BLE001
            pass
    return data


def detect_vertical(text: str) -> tuple[str, float]:
    low = (text or "").lower()
    best = ("unknown", 0.0)
    for vert, kws in _VERTICAL_KEYWORDS.items():
        hits = sum(1 for kw in kws if kw in low)
        if hits <= 0:
            continue
        score = hits / max(3, len(kws) * 0.35)
        if score > best[1]:
            best = (vert, min(1.0, score))
    if best[0] == "unknown" and any(k in low for k in ("cnc", "machining", "skraw", "produkcj")):
        return "general_machining", 0.4
    return best


def _parse_money_to_eur(num_raw: str, unit: str | None, currency: str | None) -> float | None:
    try:
        n = float(re.sub(r"[^\d.]", "", (num_raw or "").replace(",", ".").replace(" ", "")) or "0")
    except ValueError:
        return None
    if n <= 0:
        return None
    u = (unit or "").lower()
    if u in {"mrd", "mld", "bn", "billion"}:
        n *= 1_000_000_000
    elif u in {"mln", "million", "m", "m."}:
        n *= 1_000_000
    elif u in {"tys", "tys.", "k"}:
        n *= 1_000
    # crude FX — only for order-of-magnitude budget
    cur = (currency or "").upper()
    if "PLN" in (num_raw or "").upper() or cur == "PLN":
        n *= 0.23
    elif "$" in (num_raw or "") or cur == "USD":
        n *= 0.92
    elif "SEK" in (num_raw or "").upper() or cur == "SEK":
        n *= 0.087
    elif "CHF" in (num_raw or "").upper() or cur == "CHF":
        n *= 1.05
    return n


def extract_revenue_estimate(text: str) -> dict[str, Any] | None:
    m = _REVENUE_NUM_RE.search(text or "")
    if not m:
        return None
    amount = _parse_money_to_eur(m.group(1), m.group(2), m.group(3))
    if not amount:
        return None
    return {
        "value_eur_approx": round(amount, 0),
        "value_text": m.group(0).strip()[:160],
        "confidence": 0.35,
        "source": "public_text",
    }


def estimate_tooling_budget(
    *,
    vertical: str,
    revenue_eur: float | None = None,
    production_cost_ratio: float = 0.65,
    assumptions: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Budżet narzędzi ≈ (przychód × udział kosztów produkcji) × % tooling.

    Domyślnie koszty produkcji ~65% przychodu (założenie), tooling 4–10% tych kosztów.
    """
    verts = assumptions or DEFAULT_VERTICALS
    meta = verts.get(vertical) or verts["unknown"]
    pct_min = float(meta.get("tooling_pct_min", 0.04))
    pct_max = float(meta.get("tooling_pct_max", 0.10))
    out: dict[str, Any] = {
        "vertical": vertical,
        "vertical_label": meta.get("label"),
        "tooling_pct_of_production_cost": {"min": pct_min, "max": pct_max},
        "production_cost_ratio_of_revenue": production_cost_ratio,
        "assumption_notes": meta.get("notes"),
        "method": "industry_heuristic_4_to_10_pct",
        "currency": "EUR",
        "estimated_at": utc_now_iso(),
    }
    if revenue_eur and revenue_eur > 0:
        prod_cost = revenue_eur * production_cost_ratio
        out["revenue_eur_approx"] = revenue_eur
        out["production_cost_eur_approx"] = round(prod_cost, 0)
        out["annual_tooling_budget_eur"] = {
            "min": round(prod_cost * pct_min, 0),
            "max": round(prod_cost * pct_max, 0),
            "mid": round(prod_cost * (pct_min + pct_max) / 2, 0),
        }
    else:
        out["annual_tooling_budget_eur"] = None
        out["hint"] = "Brak przychodu publicznego — podaj revenue lub użyj enrich z about/page"
    return out


def detect_equipment(text: str) -> list[dict[str, Any]]:
    low = (text or "").lower()
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for brand, tier in sorted(MACHINE_TIERS.items(), key=lambda x: -len(x[0])):
        if brand in low and brand not in seen:
            # uniknij duplikatu dmg vs dmg mori
            if any(brand in s or s in brand for s in seen):
                continue
            seen.add(brand)
            found.append(
                {
                    "brand": brand.title() if brand != "dmg mori" else "DMG MORI",
                    "quality_tier": tier,
                    "category": "machine_tool",
                    "evidence": "website_mention",
                }
            )
    return found[:20]


def detect_tool_suppliers(text: str) -> list[dict[str, Any]]:
    low = (text or "").lower()
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for brand in TOOL_BRANDS:
        if brand in low:
            key = normalize_firm_name(brand)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "brand": brand.title(),
                    "relation": "buys_tools_from",
                    "evidence": "website_mention",
                    "confidence": 0.55,
                }
            )
    return found[:25]


def detect_processes_materials(text: str) -> dict[str, Any]:
    low = (text or "").lower()
    processes = [p for p, kws in PROCESS_KEYWORDS.items() if any(k in low for k in kws)]
    materials = [m for m, kws in MATERIAL_KEYWORDS.items() if any(k in low for k in kws)]
    tool_families: list[str] = []
    for p in processes:
        tool_families.extend(TOOL_FAMILY_FROM_PROCESS.get(p, []))
    # dedupe
    tool_families = list(dict.fromkeys(tool_families))
    return {
        "likely_processes": processes,
        "likely_materials": materials,
        "likely_tool_families": tool_families,
    }


def extract_customer_products(text: str, limit: int = 12) -> list[str]:
    """Proste wyciąganie fraz produktowych z nagłówków / list."""
    products: list[str] = []
    for m in re.finditer(
        r"(?i)(?:produkujemy|oferujemy|manufactur(?:e|ing)|we produce|our products)[:\s]+([^.]{8,120})",
        text or "",
    ):
        chunk = re.sub(r"\s+", " ", m.group(1)).strip(" :;-")
        if chunk and chunk.lower() not in {p.lower() for p in products}:
            products.append(chunk[:120])
    # bullet-like short noun phrases near CNC context
    for m in re.finditer(
        r"(?i)\b((?:wał|wału|korpus|obudowa|koło zębate|flansza|tuleja|formy|"
        r"shaft|housing|gear|flange|bracket|impeller|blade)s?[^\n,]{0,40})",
        text or "",
    ):
        chunk = re.sub(r"\s+", " ", m.group(1)).strip()
        if 4 <= len(chunk) <= 60 and chunk.lower() not in {p.lower() for p in products}:
            products.append(chunk)
        if len(products) >= limit:
            break
    return products[:limit]


def extract_stakeholders(text: str, limit: int = 15) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Name + title patterns: "Jan Kowalski, Purchasing Manager"
    name_title = re.finditer(
        r"(?i)\b([A-ZÀ-ÖØ-Ý][a-zà-öø-ÿ'’\-]+(?:\s+[A-ZÀ-ÖØ-Ý][a-zà-öø-ÿ'’\-]+){1,2})"
        r"\s*[,–—\-]\s*"
        r"([^,\n]{5,60})",
        text or "",
    )
    for m in name_title:
        name, title = m.group(1).strip(), m.group(2).strip()
        role, influence = "other", "low"
        for pat, r, inf in STAKEHOLDER_PATTERNS:
            if re.search(pat, title):
                role, influence = r, inf
                break
        if role == "other" and not any(
            k in title.lower()
            for k in ("manager", "director", "kierownik", "dyrektor", "engineer", "inżynier", "ceo", "prezes")
        ):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        people.append(
            {
                "name": name,
                "title": title[:80],
                "role": role,
                "influence_on_purchase": influence,
                "source": "website_heuristic",
            }
        )
        if len(people) >= limit:
            break

    # Title-only mentions (no name) — still useful as "functions present"
    named_roles = {p["role"] for p in people if p.get("name")}
    for pat, role, influence in STAKEHOLDER_PATTERNS:
        if len(people) >= limit:
            break
        if role in named_roles:
            continue
        m = re.search(pat, text or "")
        if not m:
            continue
        title = m.group(0).strip()
        key = f"role:{role}:{title.lower()}"
        if key in seen:
            continue
        seen.add(key)
        people.append(
            {
                "name": None,
                "title": title[:80],
                "role": role,
                "influence_on_purchase": influence,
                "source": "function_mention",
            }
        )
    return people[:limit]


def quality_tier_from_equipment(equipment: list[dict[str, Any]]) -> str:
    if not equipment:
        return "unknown"
    tiers = [e.get("quality_tier") for e in equipment if e.get("quality_tier")]
    if "premium" in tiers and "economy" in tiers:
        return "mixed"
    if "premium" in tiers:
        return "premium"
    if "mid" in tiers:
        return "mid"
    if "economy" in tiers:
        return "economy"
    return "unknown"


def compute_opportunity_scorecard(
    *,
    vertical: str,
    equipment: list[dict[str, Any]],
    buys_from: list[dict[str, Any]],
    product_map: dict[str, Any],
    stakeholders: list[dict[str, Any]],
    budget: dict[str, Any],
    has_website: bool,
) -> dict[str, Any]:
    # Fit to TIZ cutting tools offering
    process_fit = min(100, len(product_map.get("likely_processes") or []) * 20)
    material_fit = min(100, len(product_map.get("likely_materials") or []) * 18)
    equipment_score = {"premium": 90, "mid": 70, "mixed": 75, "economy": 45, "unknown": 25}.get(
        quality_tier_from_equipment(equipment), 25
    )
    supplier_displace = min(100, len(buys_from) * 12)  # zna tooling → da się wypierać
    stakeholder_access = min(100, sum(25 if s.get("influence_on_purchase") == "high" else 12 for s in stakeholders))
    budget_score = 30
    if budget.get("annual_tooling_budget_eur"):
        mid = float(budget["annual_tooling_budget_eur"].get("mid") or 0)
        if mid >= 500_000:
            budget_score = 95
        elif mid >= 100_000:
            budget_score = 80
        elif mid >= 25_000:
            budget_score = 65
        elif mid > 0:
            budget_score = 50
    elif vertical != "unknown":
        budget_score = 45
    digital_presence = 70 if has_website else 20
    vertical_bonus = 80 if vertical in {"automotive", "aerospace", "medical", "mold_die", "energy"} else (
        55 if vertical == "general_machining" else 35
    )

    overall = round(
        0.18 * process_fit
        + 0.12 * material_fit
        + 0.15 * equipment_score
        + 0.12 * supplier_displace
        + 0.15 * stakeholder_access
        + 0.18 * budget_score
        + 0.05 * digital_presence
        + 0.05 * vertical_bonus
    )
    table = [
        {"dimension": "process_fit", "score": process_fit, "label": "Dopasowanie procesów skrawania"},
        {"dimension": "material_fit", "score": material_fit, "label": "Materiały obrabiane (ISO)"},
        {"dimension": "equipment_quality", "score": equipment_score, "label": "Poziom jakości parku maszyn"},
        {"dimension": "current_suppliers", "score": supplier_displace, "label": "Od kogo kupuje tooling dziś"},
        {"dimension": "stakeholder_access", "score": stakeholder_access, "label": "Wpływ osób na zakup"},
        {"dimension": "budget_potential", "score": budget_score, "label": "Potencjał budżetu narzędzi"},
        {"dimension": "digital_presence", "score": digital_presence, "label": "Widoczność WWW"},
        {"dimension": "vertical_fit", "score": vertical_bonus, "label": "Branża docelowa TIZ"},
        {"dimension": "opportunity_overall", "score": overall, "label": "Ocena szansy sprzedażowej"},
    ]
    return {
        "overall": overall,
        "table": table,
        "scored_at": utc_now_iso(),
        **{row["dimension"]: row["score"] for row in table if row["dimension"] != "opportunity_overall"},
    }


def analyze_prospect_text(
    text: str,
    *,
    company: str = "",
    website: str = "",
    assumptions: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    vertical, vert_conf = detect_vertical(text)
    revenue = extract_revenue_estimate(text)
    budget = estimate_tooling_budget(
        vertical=vertical,
        revenue_eur=(revenue or {}).get("value_eur_approx"),
        assumptions=assumptions,
    )
    equipment = detect_equipment(text)
    buys_from = detect_tool_suppliers(text)
    pm = detect_processes_materials(text)
    products = extract_customer_products(text)
    stakeholders = extract_stakeholders(text)
    emp = None
    em = _EMPLOYEE_COUNT_RE.search(text or "")
    if em:
        try:
            emp = int(re.sub(r"[^\d]", "", em.group(1)))
        except ValueError:
            emp = None

    product_map = {
        "customer_products": products,
        **pm,
        "mapping_note": (
            "Procesy/materiały/narzędzia wyinferowane z treści publicznej — bez kopiowania cutting data"
        ),
    }
    quality = quality_tier_from_equipment(equipment)
    scorecard = compute_opportunity_scorecard(
        vertical=vertical,
        equipment=equipment,
        buys_from=buys_from,
        product_map=product_map,
        stakeholders=stakeholders,
        budget=budget,
        has_website=bool(website),
    )
    return {
        "company": company,
        "role": "prospect",
        "vertical": vertical,
        "vertical_confidence": round(vert_conf, 3),
        "employees_approx": emp,
        "financial": {
            "revenue_signal": revenue,
            "budget": budget,
        },
        "buys_from": buys_from,
        "equipment": equipment,
        "quality_tier": quality,
        "product_map": product_map,
        "stakeholders": stakeholders,
        "opportunity": scorecard,
        "website": website,
        "analyzed_at": utc_now_iso(),
        "disclaimer": (
            "Budżet 4–10% kosztów produkcji na tooling to założenie branżowe (heurystyka), "
            "nie audyt finansowy. Dane wyłącznie z źródeł publicznych."
        ),
    }


def analyze_prospect_html(
    html: str,
    *,
    company: str = "",
    base_url: str = "",
    assumptions: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    contacts = extract_contacts_from_html(html, base_url=base_url, company=company)
    plain = contacts.get("excerpt") or ""
    # fuller plain text
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html or "")
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    plain_full = re.sub(r"<[^>]+>", " ", text)
    plain_full = re.sub(r"\s+", " ", plain_full)
    analysis = analyze_prospect_text(
        plain_full,
        company=company,
        website=base_url,
        assumptions=assumptions,
    )
    # merge financial signals from contact extractor
    if contacts.get("financial_signals") and not (analysis.get("financial") or {}).get("revenue_signal"):
        # retry on joined signal texts
        joined = " ".join(s.get("value_text", "") for s in contacts["financial_signals"])
        rev = extract_revenue_estimate(joined)
        if rev:
            analysis["financial"]["revenue_signal"] = rev
            analysis["financial"]["budget"] = estimate_tooling_budget(
                vertical=analysis["vertical"],
                revenue_eur=rev.get("value_eur_approx"),
                assumptions=assumptions,
            )
            analysis["opportunity"] = compute_opportunity_scorecard(
                vertical=analysis["vertical"],
                equipment=analysis["equipment"],
                buys_from=analysis["buys_from"],
                product_map=analysis["product_map"],
                stakeholders=analysis["stakeholders"],
                budget=analysis["financial"]["budget"],
                has_website=bool(base_url),
            )
    analysis["contacts_extracted"] = {
        "emails": contacts.get("emails") or [],
        "phones": contacts.get("phones") or [],
        "addresses": contacts.get("addresses") or [],
    }
    return analysis


def apply_prospect_to_profile(profile: FirmProfile, analysis: dict[str, Any]) -> FirmProfile:
    """Wgraj wynik analizy prospect do FirmProfile."""
    if "prospect" not in profile.roles:
        profile.roles = list(dict.fromkeys(list(profile.roles) + ["prospect"]))
    # drop pure unknown if we know it's a prospect
    if profile.roles == ["unknown", "prospect"] or set(profile.roles) == {"unknown", "prospect"}:
        profile.roles = ["prospect"]

    contacts = analysis.get("contacts_extracted") or {}
    if contacts.get("emails"):
        profile.emails = list(profile.emails) + list(contacts["emails"])
    if contacts.get("phones"):
        profile.phones = list(profile.phones) + list(contacts["phones"])
    if contacts.get("addresses"):
        profile.addresses = list(profile.addresses) + list(contacts["addresses"])

    fin = dict(profile.financial or {})
    signals = list(fin.get("signals") or [])
    rev = (analysis.get("financial") or {}).get("revenue_signal")
    if rev:
        signals.append(
            {
                "metric": "public_revenue_mention",
                "value_text": rev.get("value_text"),
                "value_eur_approx": rev.get("value_eur_approx"),
                "confidence": rev.get("confidence"),
            }
        )
    fin["signals"] = signals[:20]
    fin["tooling_budget"] = (analysis.get("financial") or {}).get("budget")
    profile.financial = fin

    net = dict(profile.network or {})
    suppliers = list(net.get("suppliers") or [])
    for b in analysis.get("buys_from") or []:
        brand = str(b.get("brand") or "")
        if brand and brand not in suppliers:
            suppliers.append(brand)
    net["suppliers"] = suppliers
    profile.network = net

    profile.prospect = {
        "vertical": analysis.get("vertical"),
        "vertical_confidence": analysis.get("vertical_confidence"),
        "employees_approx": analysis.get("employees_approx"),
        "budget": (analysis.get("financial") or {}).get("budget"),
        "buys_from": analysis.get("buys_from") or [],
        "equipment": analysis.get("equipment") or [],
        "quality_tier": analysis.get("quality_tier"),
        "product_map": analysis.get("product_map") or {},
        "stakeholders": analysis.get("stakeholders") or [],
        "opportunity": analysis.get("opportunity") or {},
        "disclaimer": analysis.get("disclaimer"),
        "analyzed_at": analysis.get("analyzed_at") or utc_now_iso(),
    }
    if "prospect_intel" not in profile.sources:
        profile.sources.append("prospect_intel")
    return profile


@dataclass
class ProspectRegistry:
    """Operacje na profilach z rolą prospect."""

    profiles: FirmProfileRegistry
    assumptions: dict[str, dict[str, Any]] = field(default_factory=load_vertical_assumptions)

    @classmethod
    def load(cls, data_dir: Path | str | None = None) -> ProspectRegistry:
        return cls(
            profiles=FirmProfileRegistry.load(data_dir),
            assumptions=load_vertical_assumptions(),
        )

    def list_prospects(self, *, q: str | None = None, limit: int = 40) -> list[FirmProfile]:
        rows = self.profiles.list(role="prospect", q=q, sort="overall", limit=500)
        # sort by opportunity overall if present
        rows.sort(
            key=lambda p: float(((p.prospect or {}).get("opportunity") or {}).get("overall") or 0),
            reverse=True,
        )
        return rows[:limit]

    def scoreboard(self, *, limit: int = 30) -> list[dict[str, Any]]:
        out = []
        for p in self.list_prospects(limit=limit):
            pr = p.prospect or {}
            opp = pr.get("opportunity") or {}
            budget = pr.get("budget") or {}
            budget_mid = (budget.get("annual_tooling_budget_eur") or {}) or {}
            if isinstance(budget_mid, dict):
                mid = budget_mid.get("mid")
            else:
                mid = None
            out.append(
                {
                    "company": p.company,
                    "id": p.id,
                    "vertical": pr.get("vertical"),
                    "quality_tier": pr.get("quality_tier"),
                    "opportunity": opp.get("overall", 0),
                    "budget_mid_eur": mid,
                    "buys_from_count": len(pr.get("buys_from") or []),
                    "stakeholders": len(pr.get("stakeholders") or []),
                    "processes": (pr.get("product_map") or {}).get("likely_processes") or [],
                }
            )
        return out

    def upsert_from_analysis(self, company: str, analysis: dict[str, Any], data_dir: Path | str) -> FirmProfile:
        reg = self.profiles
        p = reg.get(company) or FirmProfile(
            id=firm_id_from_name(company),
            company=company,
            roles=["prospect"],
            origin="prospect_intel",
        )
        if analysis.get("website"):
            p.websites = list(p.websites) + [
                {
                    "url": analysis["website"],
                    "primary": True,
                    "verified": True,
                    "status": "cross_checked",
                    "source": "prospect_analyze",
                }
            ]
        p = apply_prospect_to_profile(p, analysis)
        p = reg.upsert(p)
        # boost overall with opportunity blend
        sc = dict(p.scores or {})
        opp = float(((p.prospect or {}).get("opportunity") or {}).get("overall") or 0)
        if opp:
            sc["prospect_opportunity"] = opp
            base = float(sc.get("overall") or 0)
            sc["overall"] = round(0.55 * base + 0.45 * opp)
            table = list(sc.get("table") or [])
            table.append(
                {
                    "dimension": "prospect_opportunity",
                    "score": opp,
                    "label": "Szansa sprzedażowa (prospect)",
                }
            )
            sc["table"] = table
            p.scores = sc
            reg.profiles[p.id] = p
        reg.save(data_dir)
        return p

    def ingest_seeds(
        self,
        data_dir: Path | str,
        seed_path: Path | str = "config/prospects.seed.json",
    ) -> dict[str, Any]:
        seed = Path(seed_path)
        added = 0
        if seed.is_file():
            try:
                rows = json.loads(seed.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                rows = []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                company = str(row.get("company") or "").strip()
                if not company:
                    continue
                p = self.profiles.get(company) or FirmProfile(
                    id=firm_id_from_name(company),
                    company=company,
                    roles=["prospect"],
                    country=str(row.get("country") or ""),
                    product_focus=str(row.get("product_focus") or ""),
                    notes=str(row.get("notes") or "")[:2000],
                    websites=[
                        {
                            "url": row["site_url"],
                            "primary": True,
                            "verified": False,
                            "status": "heuristic",
                            "source": "prospects.seed",
                        }
                    ]
                    if row.get("site_url")
                    else [],
                    origin="prospect_seed",
                    sources=["prospects.seed"],
                )
                if "prospect" not in p.roles:
                    p.roles = list(dict.fromkeys(list(p.roles) + ["prospect"]))
                self.profiles.upsert(p)
                added += 1
        path = self.profiles.save(data_dir)
        return {"ok": True, "seeded": added, "path": str(path), "count": len(self.profiles.profiles)}
