"""
Change digest — porównanie scoreboardów między runami (bez LLM / zewnętrznych API).

Snapshot: data/metrics/score_snapshot.json
Digest:   data/metrics/digest_latest.json + digest_latest.md
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_profiles(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "knowledge" / "firm_profiles.json"
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw.get("profiles") if isinstance(raw, dict) else raw
        return [r for r in (rows or []) if isinstance(r, dict)]
    except Exception:  # noqa: BLE001
        return []


def build_score_snapshot(data_dir: Path | str) -> dict[str, Any]:
    data_dir = Path(data_dir)
    firms: dict[str, dict[str, Any]] = {}
    for p in _load_profiles(data_dir):
        company = str(p.get("company") or "").strip()
        if not company:
            continue
        scores = p.get("scores") or {}
        prospect = p.get("prospect") or {}
        opp = ((prospect.get("opportunity") or {}) if isinstance(prospect, dict) else {}).get(
            "overall"
        )
        firms[company] = {
            "id": p.get("id"),
            "roles": list(p.get("roles") or []),
            "overall": float(scores.get("overall") or 0),
            "supplier_overall": float(scores.get("supplier_overall") or 0),
            "opportunity": float(opp or 0),
            "completeness": float(scores.get("completeness") or 0),
        }
    crm_open = 0
    crm_path = data_dir / "knowledge" / "crm_tasks.json"
    if crm_path.is_file():
        try:
            raw = json.loads(crm_path.read_text(encoding="utf-8"))
            tasks = raw.get("tasks") if isinstance(raw, dict) else raw
            crm_open = sum(
                1 for t in (tasks or []) if isinstance(t, dict) and t.get("status") == "open"
            )
        except Exception:  # noqa: BLE001
            pass
    return {
        "ts": utc_now_iso(),
        "firms": firms,
        "crm_open": crm_open,
        "firm_count": len(firms),
    }


def compute_digest(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    *,
    min_delta: float = 5.0,
    limit: int = 25,
) -> dict[str, Any]:
    prev_firms = (previous or {}).get("firms") or {}
    cur_firms = current.get("firms") or {}
    deltas: list[dict[str, Any]] = []
    new_firms: list[str] = []
    removed: list[str] = []

    for name, cur in cur_firms.items():
        if name not in prev_firms:
            new_firms.append(name)
            continue
        old = prev_firms[name]
        for dim in ("overall", "supplier_overall", "opportunity", "completeness"):
            a = float(old.get(dim) or 0)
            b = float(cur.get(dim) or 0)
            diff = b - a
            if abs(diff) >= min_delta:
                deltas.append(
                    {
                        "company": name,
                        "dimension": dim,
                        "old": a,
                        "new": b,
                        "delta": round(diff, 2),
                    }
                )
        old_roles = set(old.get("roles") or [])
        new_roles = set(cur.get("roles") or [])
        if old_roles != new_roles:
            deltas.append(
                {
                    "company": name,
                    "dimension": "roles",
                    "old": sorted(old_roles),
                    "new": sorted(new_roles),
                    "delta": None,
                }
            )

    for name in prev_firms:
        if name not in cur_firms:
            removed.append(name)

    deltas.sort(
        key=lambda d: abs(float(d["delta"])) if d.get("delta") is not None else 100,
        reverse=True,
    )
    crm_delta = int(current.get("crm_open") or 0) - int((previous or {}).get("crm_open") or 0)
    return {
        "ts": utc_now_iso(),
        "compared_to": (previous or {}).get("ts"),
        "new_firms": new_firms[:limit],
        "removed_firms": removed[:limit],
        "score_deltas": deltas[:limit],
        "crm_open_delta": crm_delta,
        "firm_count": current.get("firm_count"),
        "has_previous": previous is not None,
    }


def digest_to_markdown(digest: dict[str, Any]) -> str:
    lines = [
        "# Market change digest",
        "",
        f"Generated: {digest.get('ts')}",
        f"Compared to: {digest.get('compared_to') or '— (first snapshot)'}",
        f"Firms tracked: {digest.get('firm_count')}",
        f"CRM open Δ: {digest.get('crm_open_delta')}",
        "",
    ]
    if not digest.get("has_previous"):
        lines.append("_Brak poprzedniego snapshota — to baza do kolejnych porównań._")
        lines.append("")
        return "\n".join(lines)

    news = digest.get("new_firms") or []
    if news:
        lines.append("## Nowe firmy w profilach")
        for n in news:
            lines.append(f"- {n}")
        lines.append("")

    removed = digest.get("removed_firms") or []
    if removed:
        lines.append("## Usunięte / scalone")
        for n in removed:
            lines.append(f"- {n}")
        lines.append("")

    deltas = digest.get("score_deltas") or []
    if deltas:
        lines.append("## Zmiany score")
        for d in deltas:
            if d.get("dimension") == "roles":
                lines.append(f"- **{d['company']}** roles: {d['old']} → {d['new']}")
            else:
                sign = "+" if float(d.get("delta") or 0) >= 0 else ""
                lines.append(
                    f"- **{d['company']}** {d['dimension']}: "
                    f"{d['old']} → {d['new']} ({sign}{d['delta']})"
                )
        lines.append("")
    else:
        lines.append("_Brak istotnych zmian score (≥ próg)._")
        lines.append("")
    return "\n".join(lines)


def refresh_digest(
    data_dir: Path | str,
    *,
    min_delta: float = 5.0,
    limit: int = 25,
) -> dict[str, Any]:
    """Zbuduj snapshot, porównaj z poprzednim, zapisz digest + nowy snapshot."""
    data_dir = Path(data_dir)
    metrics_dir = data_dir / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    snap_path = metrics_dir / "score_snapshot.json"
    previous = None
    if snap_path.is_file():
        try:
            previous = json.loads(snap_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            previous = None

    current = build_score_snapshot(data_dir)
    digest = compute_digest(previous, current, min_delta=min_delta, limit=limit)
    md = digest_to_markdown(digest)

    (metrics_dir / "digest_latest.json").write_text(
        json.dumps(digest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (metrics_dir / "digest_latest.md").write_text(md, encoding="utf-8")
    snap_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "digest": digest, "markdown_path": str(metrics_dir / "digest_latest.md")}
