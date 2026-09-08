from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from market_agents.agents.analyst import AnalystAgent
from market_agents.agents.collector import CollectorAgent
from market_agents.agents.parsing_agent import AgenticResult, ParsingAgent
from market_agents.agents.reporter import ReporterAgent
from market_agents.config import AppConfig, load_config
from market_agents.llm import LocalLLM
from market_agents.memory import MarketMemory
from market_agents.models import MonitoringReport
from market_agents.storage import Storage


@dataclass
class RunResult:
    report: MonitoringReport
    json_path: Path
    md_path: Path
    new_items: int
    mode: str = "pipeline"
    agentic: AgenticResult | None = None
    trace_path: Path | None = None


class Orchestrator:
    """Orkiestruje lokalnych agentów: pipeline klasyczny lub agentic ReAct."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.storage = Storage(config.data_path, config.report_path)
        self.llm = LocalLLM(config.llm)
        self.memory = MarketMemory(config.data_path)
        self.collector = CollectorAgent(config, self.storage)
        self.analyst = AnalystAgent(config, self.llm)
        self.reporter = ReporterAgent(config, self.llm)
        self.parsing_agent = ParsingAgent(config, self.llm, self.memory)

    @classmethod
    def from_path(cls, path: str | Path) -> "Orchestrator":
        return cls(load_config(path))

    def health(self) -> dict:
        h = self.llm.healthcheck()
        h["agentic_enabled"] = self.config.agents.agentic.enabled
        h["gpus_hint"] = f"tensor_parallel_size={self.config.llm.tensor_parallel_size}"
        return h

    def _fleet_role(self) -> str:
        fleet = getattr(self.config.agents, "fleet", None)
        return str(getattr(fleet, "role", "central") or "central").lower()

    def run(self, skip_llm: bool = False, agentic: bool | None = None) -> RunResult:
        use_agentic = self.config.agents.agentic.enabled if agentic is None else agentic
        if skip_llm:
            use_agentic = False

        items = self.collector.run()
        agentic_result: AgenticResult | None = None

        # Ontology rebuild — tylko centrala (worker nie mutuje knowledge graph)
        if self._fleet_role() != "worker":
            try:
                from market_agents.ontology import MachiningOntology

                ont = MachiningOntology.load(self.config.data_path)
                if len(ont.nodes) < 10:
                    ont.build_from_knowledge(self.config.data_path)
            except Exception:  # noqa: BLE001
                pass

        if use_agentic and items:
            try:
                agentic_result = self.parsing_agent.run(items)
            except Exception as exc:  # noqa: BLE001
                print(f"[agentic] padł LLM/ReAct ({exc}) — fallback pipeline + raport")
                if not skip_llm:
                    items = self.analyst.analyze_batch(items)
                report = self.reporter.build(items)
                report.summary_markdown = (
                    f"# Monitoring rynku (agentic fallback): {self.config.industry.name}\n\n"
                    f"Agentic ReAct nie dokończył cyklu: {exc}\n\n"
                    "Poniżej briefing z pipeline (bez twardego stopu na Notion/LLM).\n\n---\n\n"
                    + report.summary_markdown
                )
                mode = "agentic_fallback"
                trace_path = None
            else:
                items = agentic_result.items
                final = (agentic_result.final_text or "").strip()
                llm_broke = (
                    "Agentic LLM niedostępny" in final
                    or "Nie udało się domknąć briefingu LLM" in final
                )
                if llm_broke and not skip_llm:
                    items = self.analyst.analyze_batch(items)
                report = self.reporter.build(items)
                if final and not _is_notion_abort_briefing(final):
                    title = (
                        "agentic fallback"
                        if llm_broke
                        else "agentic"
                    )
                    if llm_broke:
                        report.summary_markdown = (
                            f"# Monitoring rynku ({title}): {self.config.industry.name}\n\n"
                            f"{final}\n\n---\n\n"
                            + report.summary_markdown
                        )
                    else:
                        sources = _sources_section(report.summary_markdown)
                        report.summary_markdown = (
                            f"# Monitoring rynku ({title}): {self.config.industry.name}\n\n"
                            f"_Wygenerowano: {report.generated_at}_\n\n"
                            f"{final}"
                            f"{sources}"
                        )
                mode = "agentic_fallback" if llm_broke else "agentic"
                trace_path = agentic_result.trace_path
        else:
            if not skip_llm and items:
                items = self.analyst.analyze_batch(items)
            report = self.reporter.build(items)
            mode = "pipeline" if not skip_llm else "collect_only"
            trace_path = None

        json_path, md_path = self.storage.save_report(report)
        if items:
            self.storage.append_items(items)
            self.storage.mark_seen([i.url for i in items])

        enrich_meta: dict = {}
        try:
            enrich_meta = self._post_enrich()
        except Exception as exc:  # noqa: BLE001
            enrich_meta = {"ok": False, "error": str(exc)[:200]}

        metrics_row: dict = {}
        try:
            from market_agents.metrics import record_run_metrics

            metrics_row = record_run_metrics(
                self.config.data_path,
                mode=mode,
                new_items=len(items),
                report_path=str(md_path),
                trace_path=str(trace_path) if trace_path else None,
                post_enrich=enrich_meta,
            )
        except Exception as exc:  # noqa: BLE001
            metrics_row = {"ok": False, "error": str(exc)[:200]}

        fleet_meta: dict = {}
        try:
            fleet_meta = self._maybe_fleet_publish(mode=mode)
        except Exception as exc:  # noqa: BLE001
            fleet_meta = {"ok": False, "error": str(exc)[:200]}

        self.storage.export_snapshot(
            {
                "industry": self.config.industry.name,
                "new_items": len(items),
                "mode": mode,
                "report_md": str(md_path),
                "trace": str(trace_path) if trace_path else None,
                "post_enrich": enrich_meta,
                "metrics": {
                    "ts": metrics_row.get("ts"),
                    "knowledge_counts": metrics_row.get("knowledge_counts"),
                },
                "fleet": fleet_meta,
            }
        )
        return RunResult(
            report=report,
            json_path=json_path,
            md_path=md_path,
            new_items=len(items),
            mode=mode,
            agentic=agentic_result,
            trace_path=trace_path,
        )

    def _maybe_fleet_publish(self, *, mode: str) -> dict:
        """Centrala: auto-publish knowledge pack po run gdy auto_push_after_run."""
        fleet = getattr(self.config.agents, "fleet", None)
        role = self._fleet_role()
        if role not in {"central", "both"}:
            return {"skipped": True, "reason": f"role={role}"}
        if not bool(getattr(fleet, "auto_push_after_run", True)):
            return {"skipped": True, "reason": "auto_push_after_run=false"}
        from market_agents.fleet import FleetSync, fleet_config_from_app

        sync = FleetSync(self.config.data_path, fleet_config_from_app(self.config))
        return sync.publish_knowledge(notes=f"auto after {mode}")

    def _post_enrich(self) -> dict:
        """Odśwież firm profiles, suppliers, opcjonalnie golden records + CRM.

        Worker VPS: NIE mutuje knowledge (governance: tylko collect/feedback).
        Każdy krok ma własny error boundary — awaria jednego nie blokuje reszty.
        """
        role = self._fleet_role()
        if role == "worker":
            return {
                "ok": True,
                "skipped": True,
                "reason": "fleet.role=worker — post_enrich disabled (central owns knowledge writes)",
            }

        out: dict = {"ok": True, "errors": {}}
        data_dir = self.config.data_path

        def _step(name: str, enabled: bool, fn) -> None:
            if not enabled:
                return
            try:
                result = fn()
                out[name] = result
                if isinstance(result, dict) and result.get("ok") is False:
                    out["errors"][name] = str(
                        result.get("error") or "step reported failure"
                    )[:200]
                    out["ok"] = False
            except Exception as exc:  # noqa: BLE001
                out["errors"][name] = str(exc)[:200]
                out["ok"] = False

        def _profiles():
            from market_agents.firm_profiles import FirmProfileRegistry

            reg = FirmProfileRegistry.load(data_dir)
            built = reg.build_from_ecosystem(
                data_dir,
                competitors=list(self.config.industry.competitors or []),
                default_role="competitor",
            )
            return {"count": built.get("count"), "path": built.get("path")}

        def _resolve():
            from market_agents.entity_resolution import resolve_golden_records

            return resolve_golden_records(data_dir, dry_run=False)

        def _suppliers():
            from market_agents.suppliers import SupplierRegistry

            return SupplierRegistry.load(data_dir).refresh_and_save(data_dir)

        def _crm():
            from market_agents.crm_tasks import create_tasks_from_prospect_scoreboard

            return create_tasks_from_prospect_scoreboard(
                data_dir,
                min_opportunity=float(
                    getattr(self.config.agents, "crm_min_opportunity", 60.0) or 60.0
                ),
            )

        def _crm_notion():
            from market_agents.crm_tasks import push_crm_tasks_to_notion

            return push_crm_tasks_to_notion(self.config, only_hot=True, limit=20)

        def _digest():
            from market_agents.change_digest import refresh_digest

            dig = refresh_digest(data_dir)
            return {
                "ok": dig.get("ok"),
                "score_deltas": len((dig.get("digest") or {}).get("score_deltas") or []),
                "new_firms": len((dig.get("digest") or {}).get("new_firms") or []),
                "path": dig.get("markdown_path"),
            }

        _step("profiles", getattr(self.config.agents, "post_enrich_profiles", True), _profiles)
        _step(
            "entity_resolution",
            getattr(self.config.agents, "post_enrich_resolve_duplicates", False),
            _resolve,
        )
        _step("suppliers", getattr(self.config.agents, "post_enrich_suppliers", True), _suppliers)
        _step("crm", getattr(self.config.agents, "post_enrich_crm_tasks", True), _crm)
        _step(
            "crm_notion",
            getattr(self.config.agents, "post_enrich_crm_notion", False),
            _crm_notion,
        )
        _step("digest", getattr(self.config.agents, "post_enrich_digest", True), _digest)
        if not out["errors"]:
            out.pop("errors", None)
        return out


def _is_notion_abort_briefing(text: str) -> bool:
    """Model sometimes aborts with a Notion-token error instead of a briefing."""
    t = (text or "").lower()
    if "notion" not in t:
        return False
    if "notion_token" not in t and "brak token" not in t and "brak notion" not in t:
        return False
    if len(text) > 2500:
        return False
    return bool(re.search(r"bł[aą]d|error|ustaw notion", t))


def _sources_section(markdown: str) -> str:
    """Keep provenance without appending a contradictory second briefing."""
    marker = "\n## Źródła\n"
    if marker not in markdown:
        return ""
    return "\n\n---\n\n## Źródła\n" + markdown.split(marker, 1)[1].strip()
