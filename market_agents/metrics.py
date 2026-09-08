"""
Lekkie metryki runu agentów (observability bez zewnętrznego stacka).

Zapis: data/metrics/runs.jsonl + data/metrics/latest.json
       + data/knowledge/run_status.json (dla fleet / operatorów)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def record_run_metrics(
    data_dir: Path | str,
    *,
    mode: str,
    new_items: int,
    report_path: str | None = None,
    trace_path: str | None = None,
    post_enrich: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data_dir = Path(data_dir)
    metrics_dir = data_dir / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)

    knowledge = data_dir / "knowledge"
    counts = knowledge_counts(data_dir)
    row = {
        "ts": utc_now_iso(),
        "mode": mode,
        "new_items": new_items,
        "report_path": report_path,
        "trace_path": trace_path,
        "post_enrich": post_enrich or {},
        "knowledge_counts": counts,
        **(extra or {}),
    }
    path = metrics_dir / "runs.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    latest = metrics_dir / "latest.json"
    latest.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")

    # Slim status for fleet pack / VPS workers
    knowledge.mkdir(parents=True, exist_ok=True)
    status = {
        "ts": row["ts"],
        "mode": mode,
        "new_items": new_items,
        "knowledge_counts": counts,
        "crm_notion_pushed": counts.get("crm_with_notion", 0),
        "post_enrich_ok": bool((post_enrich or {}).get("ok", True)),
    }
    (knowledge / "run_status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return row


def knowledge_counts(data_dir: Path | str) -> dict[str, int]:
    data_dir = Path(data_dir)
    knowledge = data_dir / "knowledge"
    profiles_path = knowledge / "firm_profiles.json"
    return {
        "firm_profiles": _json_count(profiles_path, "profiles"),
        "suppliers": _role_count(profiles_path, ("supplier", "distributor", "dealer")),
        "prospects": _role_count(profiles_path, ("prospect",)),
        "crm_tasks_open": _crm_open(knowledge / "crm_tasks.json"),
        "crm_with_notion": _crm_with_notion(knowledge / "crm_tasks.json"),
        "tenders": _json_count(knowledge / "tenders.json", "signals"),
        "literature": _json_count(knowledge / "literature.json", "items"),
        "pricelists": _json_count(knowledge / "available_pricelists.json", "items"),
        "product_tech": _json_count(knowledge / "product_tech.json", "items"),
        "ontology_nodes": _json_count(knowledge / "ontology.json", "nodes"),
        "relations": _json_count(knowledge / "firm_relations.json", "edges"),
    }


def _json_count(path: Path, key: str) -> int:
    if not path.is_file():
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return 0
    if isinstance(raw, list):
        return len(raw)
    if isinstance(raw, dict):
        val = raw.get(key)
        if isinstance(val, list):
            return len(val)
        if isinstance(val, dict):
            return len(val)
    return 0


def _role_count(path: Path, roles: tuple[str, ...]) -> int:
    if not path.is_file():
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw.get("profiles") if isinstance(raw, dict) else raw
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    want = set(roles)
    supplier_like = {"supplier", "distributor", "dealer"}
    for p in rows or []:
        if not isinstance(p, dict):
            continue
        proles = p.get("roles") or []
        if isinstance(proles, str):
            proles = [proles]
        prole_set = {str(r) for r in proles}
        if want.intersection(prole_set):
            n += 1
            continue
        if "prospect" in want and isinstance(p.get("prospect"), dict) and p.get("prospect"):
            n += 1
            continue
        if want.intersection(supplier_like):
            net = p.get("network") or {}
            if isinstance(net, dict) and (net.get("customers") or net.get("dealers")):
                n += 1
    return n


def _crm_open(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        tasks = raw.get("tasks") if isinstance(raw, dict) else raw
        return sum(1 for t in (tasks or []) if isinstance(t, dict) and t.get("status") == "open")
    except Exception:  # noqa: BLE001
        return 0


def _crm_with_notion(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        tasks = raw.get("tasks") if isinstance(raw, dict) else raw
        return sum(1 for t in (tasks or []) if isinstance(t, dict) and t.get("notion_url"))
    except Exception:  # noqa: BLE001
        return 0


def recent_metrics(data_dir: Path | str, limit: int = 20) -> list[dict[str, Any]]:
    path = Path(data_dir) / "metrics" / "runs.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
