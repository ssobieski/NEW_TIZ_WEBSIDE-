"""
Inferencja tooling dla profilowania klientów (prospects).

Z publicznych opisów (WWW / film / news) buduje:
1) jakie produkty firma produkuje
2) teoretyczny proces technologiczny (how it's made)
3) jakie narzędzia praktyczne prawdopodobnie używa
4) z parkem maszynowym → dodatkowe rodziny narzędzi / oprzyrządowanie
5) reguły peerów: podobne firmy (np. formy) → podobny zakres tooling

Reguły: config/tooling_inference.rules.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_RULES_PATH = Path("config/tooling_inference.rules.json")


def load_tooling_rules(path: Path | str | None = None) -> dict[str, Any]:
    path = Path(path or DEFAULT_RULES_PATH)
    if not path.is_file():
        return {"product_families": {}, "equipment_to_tools": {}, "peer_rules": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"product_families": {}, "equipment_to_tools": {}, "peer_rules": []}
    return raw if isinstance(raw, dict) else {}


def detect_product_families(
    text: str,
    *,
    rules: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rules = rules or load_tooling_rules()
    low = (text or "").lower()
    found: list[dict[str, Any]] = []
    for fam_id, meta in (rules.get("product_families") or {}).items():
        if not isinstance(meta, dict):
            continue
        kws = [str(k).lower() for k in (meta.get("keywords") or [])]
        hits = [k for k in kws if k and k in low]
        if not hits:
            continue
        found.append(
            {
                "id": fam_id,
                "label": meta.get("label") or fam_id,
                "confidence": min(0.95, 0.45 + 0.12 * len(hits)),
                "matched_keywords": hits[:6],
                "peer_vertical": meta.get("peer_vertical"),
                "theoretical_process": list(meta.get("theoretical_process") or []),
                "typical_materials": list(meta.get("typical_materials") or []),
                "practical_tools": list(meta.get("practical_tools") or []),
                "workholding": list(meta.get("workholding") or []),
            }
        )
    found.sort(key=lambda r: float(r.get("confidence") or 0), reverse=True)
    return found


def detect_equipment_categories(
    text: str,
    *,
    rules: dict[str, Any] | None = None,
    brand_equipment: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Kategorie maszyn z opisu + mapowanie brand machines → kategorie."""
    rules = rules or load_tooling_rules()
    low = (text or "").lower()
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cat_id, meta in (rules.get("equipment_to_tools") or {}).items():
        if not isinstance(meta, dict):
            continue
        kws = [str(k).lower() for k in (meta.get("keywords") or [])]
        hits = [k for k in kws if k and k in low]
        if not hits:
            continue
        seen.add(cat_id)
        found.append(
            {
                "category": cat_id,
                "confidence": min(0.95, 0.5 + 0.1 * len(hits)),
                "matched_keywords": hits[:6],
                "processes": list(meta.get("processes") or []),
                "practical_tools": list(meta.get("practical_tools") or []),
                "workholding": list(meta.get("workholding") or []),
                "source": "text_equipment",
            }
        )
    # Brand machines from detect_equipment → soft map to mill/lathe
    for row in brand_equipment or []:
        brand = str(row.get("brand") or "").lower()
        cat = "cnc_mill"
        if any(x in brand for x in ("okuma", "mazak", "haas", "dmg", "doosan", "hyundai", "makino", "hermle", "grob")):
            # unknown — keep mill as default for machining centers; lathe brands weaker signal
            cat = "cnc_mill"
        if cat not in seen:
            seen.add(cat)
            meta = (rules.get("equipment_to_tools") or {}).get(cat) or {}
            found.append(
                {
                    "category": cat,
                    "confidence": 0.55,
                    "matched_keywords": [row.get("brand")],
                    "processes": list(meta.get("processes") or []),
                    "practical_tools": list(meta.get("practical_tools") or []),
                    "workholding": list(meta.get("workholding") or []),
                    "source": "machine_brand",
                    "quality_tier": row.get("quality_tier"),
                }
            )
    found.sort(key=lambda r: float(r.get("confidence") or 0), reverse=True)
    return found


def apply_peer_rules(
    *,
    product_families: list[dict[str, Any]],
    vertical: str,
    rules: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rules = rules or load_tooling_rules()
    fam_ids = {f.get("id") for f in product_families}
    families = rules.get("product_families") or {}
    applied: list[dict[str, Any]] = []
    for rule in rules.get("peer_rules") or []:
        if not isinstance(rule, dict):
            continue
        when_fam = set(rule.get("when_product_family") or [])
        or_vert = set(rule.get("or_vertical") or [])
        hit = bool(when_fam & fam_ids) or (vertical in or_vert and vertical != "unknown")
        if not hit:
            continue
        src_id = str(rule.get("apply_tooling_from_family") or "")
        src = families.get(src_id) or {}
        if not src:
            continue
        applied.append(
            {
                "rule_id": rule.get("id"),
                "rationale": rule.get("rationale") or "",
                "from_family": src_id,
                "practical_tools": list(src.get("practical_tools") or []),
                "workholding": list(src.get("workholding") or []),
                "theoretical_process": list(src.get("theoretical_process") or []),
                "typical_materials": list(src.get("typical_materials") or []),
            }
        )
    return applied


def infer_customer_tooling(
    text: str,
    *,
    vertical: str = "unknown",
    brand_equipment: list[dict[str, Any]] | None = None,
    process_hint: list[str] | None = None,
    material_hint: list[str] | None = None,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Główna inferencja: produkty → proces teoretyczny → narzędzia praktyczne
    + sprzęt → narzędzia + peer rules.
    """
    rules = rules or load_tooling_rules()
    families = detect_product_families(text, rules=rules)
    equipment_cats = detect_equipment_categories(
        text, rules=rules, brand_equipment=brand_equipment
    )
    peers = apply_peer_rules(
        product_families=families, vertical=vertical, rules=rules
    )

    processes: list[str] = list(process_hint or [])
    materials: list[str] = list(material_hint or [])
    tools: list[str] = []
    workholding: list[str] = []
    theoretical: list[str] = []
    evidence: list[dict[str, Any]] = []

    for fam in families:
        theoretical.extend(fam.get("theoretical_process") or [])
        materials.extend(fam.get("typical_materials") or [])
        tools.extend(fam.get("practical_tools") or [])
        workholding.extend(fam.get("workholding") or [])
        # map process steps that look like known process keys
        for step in fam.get("theoretical_process") or []:
            s = str(step).lower()
            if "mill" in s or "hsm" in s:
                processes.append("milling" if "hsm" not in s else "hsm")
            if "turn" in s or "swiss" in s:
                processes.append("turning")
            if "drill" in s or "deep_hole" in s:
                processes.append("drilling")
            if "thread" in s or "tapp" in s:
                processes.append("threading")
            if "grind" in s or "polish" in s:
                processes.append("grinding")
            if "edm" in s:
                processes.append("edm")
        evidence.append(
            {
                "source": "product_family",
                "id": fam.get("id"),
                "label": fam.get("label"),
                "confidence": fam.get("confidence"),
            }
        )

    for eq in equipment_cats:
        processes.extend(eq.get("processes") or [])
        tools.extend(eq.get("practical_tools") or [])
        workholding.extend(eq.get("workholding") or [])
        evidence.append(
            {
                "source": "equipment",
                "id": eq.get("category"),
                "confidence": eq.get("confidence"),
            }
        )

    for peer in peers:
        theoretical.extend(peer.get("theoretical_process") or [])
        materials.extend(peer.get("typical_materials") or [])
        tools.extend(peer.get("practical_tools") or [])
        workholding.extend(peer.get("workholding") or [])
        evidence.append(
            {
                "source": "peer_rule",
                "id": peer.get("rule_id"),
                "rationale": peer.get("rationale"),
            }
        )

    def _dedupe(rows: list[str]) -> list[str]:
        return list(dict.fromkeys(str(x) for x in rows if x))

    # Confidence: more independent sources → higher
    src_kinds = {e.get("source") for e in evidence}
    conf = 0.35
    if families:
        conf += 0.2
    if equipment_cats:
        conf += 0.15
    if peers:
        conf += 0.15
    if process_hint:
        conf += 0.1
    conf = min(0.95, conf)

    return {
        "product_families": families,
        "equipment_categories": equipment_cats,
        "peer_rules_applied": peers,
        "theoretical_process": _dedupe(theoretical),
        "likely_processes": _dedupe(processes),
        "likely_materials": _dedupe(materials),
        "practical_tools": _dedupe(tools),
        "workholding_fixturing": _dedupe(workholding),
        "inference_confidence": round(conf, 3),
        "evidence": evidence[:20],
        "method_note": (
            "Heurystyka: produkt→proces teoretyczny→narzędzia; sprzęt→narzędzia; "
            "peerzy branżowi (np. formy) dzielą podobny zakres tooling/oprzyrządowania. "
            "Źródła: opis WWW / film / publiczne info — bez gwarancji kompletności."
        ),
        "sources_used": sorted(src_kinds),
    }
