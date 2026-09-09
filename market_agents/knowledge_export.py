"""
Eksport wiedzy operatorskiej — offline pack (Markdown + JSON) bez UI / zewnętrznych API.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def export_knowledge_pack(
    data_dir: Path | str,
    out_dir: Path | str,
    *,
    limit: int = 30,
) -> dict[str, Any]:
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    knowledge = data_dir / "knowledge"

    from market_agents.firm_profiles import FirmProfileRegistry
    from market_agents.prospects import ProspectRegistry
    from market_agents.suppliers import SupplierRegistry
    from market_agents.crm_tasks import CrmTaskStore
    from market_agents.contacts import ContactRegistry
    from market_agents.deals import DealRegistry
    from market_agents.metrics import knowledge_counts, recent_metrics

    profiles = FirmProfileRegistry.load(data_dir)
    firm_board = profiles.scoreboard(limit=limit)
    prospect_board = ProspectRegistry.load(data_dir).scoreboard(limit=limit)
    supplier_board = SupplierRegistry.load(data_dir).scoreboard(limit=limit)
    crm = [t.to_dict() for t in CrmTaskStore.load(data_dir).list(status="open", limit=limit)]
    contacts = ContactRegistry.load(data_dir).scoreboard(limit=limit)
    deals = DealRegistry.load(data_dir).pipeline(limit=limit)
    counts = knowledge_counts(data_dir)
    metrics = recent_metrics(data_dir, limit=5)
    relations = _load_json(knowledge / "firm_relations.json") or {}
    edges = relations.get("edges") if isinstance(relations, dict) else relations
    edge_n = len(edges) if isinstance(edges, list) else 0
    digest = _load_json(Path(data_dir) / "metrics" / "digest_latest.json") or {}

    payload = {
        "exported_at": utc_now_iso(),
        "counts": counts,
        "firm_scoreboard": firm_board,
        "prospect_scoreboard": prospect_board,
        "supplier_scoreboard": supplier_board,
        "crm_open": crm,
        "contact_scoreboard": contacts,
        "deal_pipeline": deals,
        "relations_count": edge_n,
        "digest": digest,
        "recent_metrics": metrics,
    }
    json_path = out_dir / "knowledge_export.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    md_lines = [
        f"# TIZ market knowledge export",
        "",
        f"Exported: {payload['exported_at']}",
        "",
        "## Counts",
        "",
    ]
    for k, v in counts.items():
        md_lines.append(f"- **{k}**: {v}")
    md_lines += ["", "## Firm scoreboard (top)", ""]
    for row in firm_board[:limit]:
        md_lines.append(
            f"- {row.get('company')} — overall={row.get('overall')} "
            f"roles={','.join(row.get('roles') or [])}"
        )
    md_lines += ["", "## Prospects", ""]
    for row in prospect_board[:limit]:
        md_lines.append(
            f"- {row.get('company')} — opportunity={row.get('opportunity')} "
            f"vertical={row.get('vertical')} budget_mid={row.get('budget_mid_eur')}"
        )
    md_lines += ["", "## Suppliers", ""]
    for row in supplier_board[:limit]:
        md_lines.append(
            f"- {row.get('company')} — supplier={row.get('supplier_overall')} "
            f"channel={row.get('channel_reach')}"
        )
    md_lines += ["", "## CRM open", ""]
    for t in crm:
        notion = t.get("notion_url") or "—"
        md_lines.append(
            f"- [{t.get('priority')}] {t.get('company')}: {t.get('title')} "
            f"(opp={t.get('opportunity_score')}, notion={notion})"
        )
    md_lines += ["", "## Contacts (buying center)", ""]
    for c in contacts:
        md_lines.append(
            f"- {c.get('name')} @ {c.get('company')} — {c.get('role')} "
            f"infl={c.get('influence_on_purchase')} reach={c.get('reachability')}"
        )
    md_lines += ["", "## Deal pipeline", ""]
    for d in deals:
        md_lines.append(
            f"- [{d.get('stage')}] {d.get('company')} opp={d.get('opportunity_score')} "
            f"EV={d.get('expected_value_eur')} next={d.get('next_action')}"
        )
    if digest.get("score_deltas"):
        md_lines += ["", "## Recent score deltas", ""]
        for d in (digest.get("score_deltas") or [])[:15]:
            md_lines.append(
                f"- {d.get('company')} {d.get('dimension')}: {d.get('old')} → {d.get('new')}"
            )
    md_lines.append("")
    md_path = out_dir / "knowledge_export.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    return {
        "ok": True,
        "out_dir": str(out_dir),
        "json": str(json_path),
        "markdown": str(md_path),
        "counts": counts,
    }
