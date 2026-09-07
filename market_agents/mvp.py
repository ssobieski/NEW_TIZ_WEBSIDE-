"""
MVP bootstrap — offline ścieżka testowa bez GPU / Notion / sieci.

Buduje lokalną wiedzę z seedów, scoreboardy, CRM, digest, export i fleet pack.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


SAMPLE_PROSPECT_COMPANY = "Beta Precision CNC"


def bootstrap_mvp(
    data_dir: Path | str,
    *,
    config_path: Path | str | None = None,
    prospect_sample: Path | str = "config/mvp_prospect_sample.txt",
    reset: bool = False,
) -> dict[str, Any]:
    """
    Przygotuj katalog data/mvp z pełnym offline knowledge loop.

    Kroki: known_firms → relations → ontology → profiles → prospects →
    CRM → suppliers → digest → export → fleet publish → collect_only run.
    """
    from market_agents.agents.orchestrator import Orchestrator
    from market_agents.change_digest import refresh_digest
    from market_agents.config import load_config
    from market_agents.crm_tasks import create_tasks_from_prospect_scoreboard
    from market_agents.firm_profiles import FirmProfileRegistry
    from market_agents.firm_relations import FirmRelationsGraph
    from market_agents.firms import KnownFirmsIndex
    from market_agents.fleet import FleetSync, fleet_config_from_app
    from market_agents.knowledge_export import export_knowledge_pack
    from market_agents.ontology import MachiningOntology
    from market_agents.prospects import ProspectRegistry, analyze_prospect_text, load_vertical_assumptions
    from market_agents.suppliers import SupplierRegistry

    data_dir = Path(data_dir)
    if reset and data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    knowledge = data_dir / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {"ok": True, "data_dir": str(data_dir)}

    # 1) known firms cache from seed
    idx = KnownFirmsIndex.load(data_dir)
    firms_path = knowledge / "known_firms.json"
    firms_path.write_text(json.dumps(idx.firms, ensure_ascii=False, indent=2), encoding="utf-8")
    out["known_firms"] = len(idx.firms)

    # 2) materialize relations (seed merge → save)
    graph = FirmRelationsGraph.load(data_dir)
    rel_path = graph.save(data_dir)
    out["relations"] = {"count": len(graph.edges), "path": str(rel_path)}

    # 3) ontology
    ont = MachiningOntology.load(data_dir)
    if len(ont.nodes) < 10:
        ont.merge_seed("config/machining_ontology.seed.json")
        ont.build_from_knowledge(data_dir)
    ont_path = ont.save(data_dir)
    out["ontology"] = {"nodes": len(ont.nodes), "edges": len(ont.edges), "path": str(ont_path)}

    # 4) firm profiles from ecosystem
    competitors: list[str] = []
    if config_path and Path(config_path).is_file():
        cfg_preview = load_config(config_path)
        competitors = list(cfg_preview.industry.competitors or [])
    reg = FirmProfileRegistry.load(data_dir)
    built = reg.build_from_ecosystem(
        data_dir, competitors=competitors or None, default_role="competitor"
    )
    out["profiles"] = built

    # 5) prospect seeds + sample analysis (no network)
    pr = ProspectRegistry.load(data_dir)
    seeded = pr.ingest_seeds(data_dir)
    out["prospect_seeds"] = seeded

    sample_path = Path(prospect_sample)
    if sample_path.is_file():
        text = sample_path.read_text(encoding="utf-8")
        analysis = analyze_prospect_text(
            text,
            company=SAMPLE_PROSPECT_COMPANY,
            website="",
            assumptions=load_vertical_assumptions(),
        )
        profile = pr.upsert_from_analysis(SAMPLE_PROSPECT_COMPANY, analysis, data_dir)
        out["sample_prospect"] = {
            "company": profile.company,
            "opportunity": ((profile.prospect or {}).get("opportunity") or {}).get("overall"),
            "vertical": (profile.prospect or {}).get("vertical"),
            "quality_tier": (profile.prospect or {}).get("quality_tier"),
        }
    else:
        out["sample_prospect"] = {"ok": False, "error": f"missing {sample_path}"}

    # 6) CRM from hot prospects
    out["crm"] = create_tasks_from_prospect_scoreboard(
        data_dir, min_opportunity=50.0, limit=20
    )

    # 7) suppliers scorecard
    out["suppliers"] = SupplierRegistry.load(data_dir).refresh_and_save(data_dir)

    # 8) digest (two passes so second can show deltas if scores changed)
    refresh_digest(data_dir, min_delta=1.0)
    # bump one score to guarantee visible delta for smoke
    reg2 = FirmProfileRegistry.load(data_dir)
    if reg2.profiles:
        any_p = next(iter(reg2.profiles.values()))
        scores = dict(any_p.scores or {})
        scores["overall"] = float(scores.get("overall") or 0) + 8
        any_p.scores = scores
        reg2.profiles[any_p.id] = any_p
        reg2.save(data_dir)
    dig = refresh_digest(data_dir, min_delta=1.0)
    out["digest"] = {
        "path": dig.get("markdown_path"),
        "deltas": len((dig.get("digest") or {}).get("score_deltas") or []),
    }

    # 9) export pack
    export_dir = data_dir / "export"
    out["export"] = export_knowledge_pack(data_dir, export_dir, limit=20)

    # 10) orchestrator collect_only (empty sources → fast) + metrics/run_status
    cfg_path = Path(config_path or "config/mvp.example.yaml")
    if not cfg_path.is_file():
        out["run"] = {"ok": False, "error": f"missing config {cfg_path}"}
        out["acceptance"] = mvp_acceptance(data_dir)
        out["ok"] = False
        return out
    cfg = load_config(cfg_path)
    # force data_dir from bootstrap target
    cfg.agents.data_dir = str(data_dir)
    cfg.agents.report_dir = str(data_dir / "reports")
    cfg.agents.fleet.sync_dir = str(data_dir / "fleet")
    cfg.agents.fleet.auto_push_after_run = False  # explicit publish below
    orch = Orchestrator(cfg)
    result = orch.run(skip_llm=True)
    out["run"] = {
        "mode": result.mode,
        "new_items": result.new_items,
        "report_md": str(result.md_path),
    }

    # 11) fleet publish after run_status exists
    sync = FleetSync(data_dir, fleet_config_from_app(cfg))
    out["fleet"] = sync.publish_knowledge(notes="mvp bootstrap")

    # acceptance snapshot
    out["acceptance"] = mvp_acceptance(data_dir)
    out["ok"] = bool(out["acceptance"].get("ok"))
    return out


def mvp_acceptance(data_dir: Path | str) -> dict[str, Any]:
    """Sprawdź minimalne artefakty MVP."""
    data_dir = Path(data_dir)
    knowledge = data_dir / "knowledge"
    checks = {
        "known_firms": (knowledge / "known_firms.json").is_file(),
        "firm_relations": (knowledge / "firm_relations.json").is_file(),
        "ontology": (knowledge / "ontology.json").is_file(),
        "firm_profiles": (knowledge / "firm_profiles.json").is_file(),
        "crm_tasks": (knowledge / "crm_tasks.json").is_file(),
        "run_status": (knowledge / "run_status.json").is_file(),
        "digest_md": (data_dir / "metrics" / "digest_latest.md").is_file(),
        "metrics_latest": (data_dir / "metrics" / "latest.json").is_file(),
        "export_md": (data_dir / "export" / "knowledge_export.md").is_file(),
        "fleet_latest": (data_dir / "fleet" / "latest.json").is_file(),
    }
    # content thresholds
    counts: dict[str, Any] = {}
    try:
        profiles = json.loads((knowledge / "firm_profiles.json").read_text(encoding="utf-8"))
        counts["profiles"] = len(profiles.get("profiles") or [])
        checks["profiles_ge_5"] = counts["profiles"] >= 5
    except Exception:  # noqa: BLE001
        checks["profiles_ge_5"] = False
    try:
        crm = json.loads((knowledge / "crm_tasks.json").read_text(encoding="utf-8"))
        counts["crm_open"] = sum(
            1 for t in (crm.get("tasks") or []) if isinstance(t, dict) and t.get("status") == "open"
        )
        checks["crm_open_ge_1"] = counts["crm_open"] >= 1
    except Exception:  # noqa: BLE001
        checks["crm_open_ge_1"] = False

    failed = [k for k, v in checks.items() if not v]
    return {"ok": not failed, "checks": checks, "counts": counts, "failed": failed}


def mvp_status(data_dir: Path | str) -> dict[str, Any]:
    from market_agents.metrics import knowledge_counts

    data_dir = Path(data_dir)
    return {
        "data_dir": str(data_dir),
        "acceptance": mvp_acceptance(data_dir),
        "knowledge_counts": knowledge_counts(data_dir) if data_dir.exists() else {},
    }
