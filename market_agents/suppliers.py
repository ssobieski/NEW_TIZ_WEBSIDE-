"""
Dostawcy (suppliers) — first-class scorecard obok prospects.

Ocena: pokrycie marek, sieć dystrybucji, kompletność profilu, sygnały relacji.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from market_agents.firm_profiles import FirmProfile, FirmProfileRegistry, utc_now_iso


def compute_supplier_scorecard(profile: FirmProfile) -> dict[str, Any]:
    net = profile.network or {}
    brands = list(net.get("oem_group") or []) + list(profile.aliases or [])
    customers = list(net.get("customers") or [])
    dealers = list(net.get("dealers") or [])
    distributors = list(net.get("distributors") or [])

    brand_coverage = min(
        100,
        max(len(brands), 1 if "supplier" in (profile.roles or []) else 0) * 20,
    )
    channel_reach = min(100, (len(customers) + len(dealers)) * 18)
    profile_completeness = float((profile.scores or {}).get("completeness") or 0)
    identity = float((profile.scores or {}).get("identity_trust") or 0)
    digital = float((profile.scores or {}).get("digital_commerce") or 0)
    sources = profile.sources or []
    evidence = min(100, len(sources) * 15 + (25 if "firm_relations" in sources else 0))

    overall = round(
        0.20 * brand_coverage
        + 0.25 * channel_reach
        + 0.15 * profile_completeness
        + 0.15 * identity
        + 0.10 * digital
        + 0.15 * evidence
    )
    table = [
        {"dimension": "brand_coverage", "score": brand_coverage, "label": "Pokrycie marek / portfolio"},
        {"dimension": "channel_reach", "score": channel_reach, "label": "Zasięg kanału (klienci/dealerzy)"},
        {"dimension": "profile_completeness", "score": profile_completeness, "label": "Kompletność karty"},
        {"dimension": "identity_trust", "score": identity, "label": "Weryfikacja kontaktu"},
        {"dimension": "digital_commerce", "score": digital, "label": "E-shop / cenniki"},
        {"dimension": "relation_evidence", "score": evidence, "label": "Siła dowodów relacji"},
        {"dimension": "supplier_overall", "score": overall, "label": "Ocena dostawcy"},
    ]
    return {
        "overall": overall,
        "table": table,
        "brands": brands[:20],
        "customers": customers[:20],
        "dealers": dealers[:20],
        "distributors_linked": distributors[:20],
        "scored_at": utc_now_iso(),
    }


def _is_supplier_like(profile: FirmProfile) -> bool:
    roles = profile.roles or []
    if any(r in roles for r in ("supplier", "distributor", "dealer")):
        return True
    net = profile.network or {}
    return bool(net.get("customers") or net.get("dealers"))


class SupplierRegistry:
    def __init__(self, profiles: FirmProfileRegistry) -> None:
        self.profiles = profiles

    @classmethod
    def load(cls, data_dir: Path | str | None = None) -> SupplierRegistry:
        return cls(FirmProfileRegistry.load(data_dir))

    def list_suppliers(self, *, q: str | None = None, limit: int = 40) -> list[FirmProfile]:
        rows = [p for p in self.profiles.profiles.values() if _is_supplier_like(p)]
        if q:
            ql = q.lower()
            rows = [
                p
                for p in rows
                if ql in p.company.lower() or any(ql in a.lower() for a in p.aliases)
            ]
        # Sort by live scorecard without mutating persisted profile.scores
        scored: list[tuple[float, FirmProfile]] = []
        for p in rows:
            sc = compute_supplier_scorecard(p)
            scored.append((float(sc["overall"]), p))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in scored[:limit]]

    def scoreboard(self, *, limit: int = 30) -> list[dict[str, Any]]:
        rows = [p for p in self.profiles.profiles.values() if _is_supplier_like(p)]
        board: list[dict[str, Any]] = []
        for p in rows:
            sc = compute_supplier_scorecard(p)
            board.append(
                {
                    "company": p.company,
                    "id": p.id,
                    "roles": p.roles,
                    "country": p.country,
                    "supplier_overall": sc["overall"],
                    "brand_coverage": sc["table"][0]["score"],
                    "channel_reach": sc["table"][1]["score"],
                    "customers": len(sc.get("customers") or []),
                    "brands": sc.get("brands") or [],
                }
            )
        board.sort(key=lambda r: float(r.get("supplier_overall") or 0), reverse=True)
        return board[:limit]

    def refresh_and_save(self, data_dir: Path | str) -> dict[str, Any]:
        """Policz scorecard i zapisz w profile.scores."""
        n = 0
        for p in list(self.profiles.profiles.values()):
            if not _is_supplier_like(p):
                continue
            sc = compute_supplier_scorecard(p)
            scores = dict(p.scores or {})
            scores["supplier_overall"] = sc["overall"]
            scores["supplier_table"] = sc["table"]
            p.scores = scores
            self.profiles.profiles[p.id] = p
            n += 1
        path = self.profiles.save(data_dir)
        return {
            "ok": True,
            "scored": n,
            "path": str(path),
            "scoreboard": self.scoreboard(limit=10),
        }
