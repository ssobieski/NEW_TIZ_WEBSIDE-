from __future__ import annotations

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

    def run(self, skip_llm: bool = False, agentic: bool | None = None) -> RunResult:
        use_agentic = self.config.agents.agentic.enabled if agentic is None else agentic
        if skip_llm:
            use_agentic = False

        items = self.collector.run()
        agentic_result: AgenticResult | None = None

        # Upewnij się że ontology DB istnieje (kontekst dla botów)
        try:
            from market_agents.ontology import MachiningOntology

            ont = MachiningOntology.load(self.config.data_path)
            if len(ont.nodes) < 10:
                ont.build_from_knowledge(self.config.data_path)
        except Exception:  # noqa: BLE001
            pass

        if use_agentic and items:
            agentic_result = self.parsing_agent.run(items)
            items = agentic_result.items
            report = self.reporter.build(items)
            if agentic_result.final_text:
                report.summary_markdown = (
                    f"# Monitoring rynku (agentic): {self.config.industry.name}\n\n"
                    f"{agentic_result.final_text}\n\n---\n\n"
                    + report.summary_markdown
                )
            mode = "agentic"
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

        self.storage.export_snapshot(
            {
                "industry": self.config.industry.name,
                "new_items": len(items),
                "mode": mode,
                "report_md": str(md_path),
                "trace": str(trace_path) if trace_path else None,
                "post_enrich": enrich_meta,
                "metrics": {"ts": metrics_row.get("ts"), "knowledge_counts": metrics_row.get("knowledge_counts")},
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

    def _post_enrich(self) -> dict:
        """Odśwież firm profiles, suppliers, opcjonalnie golden records + CRM."""
        out: dict = {"ok": True}
        data_dir = self.config.data_path
        if getattr(self.config.agents, "post_enrich_profiles", True):
            from market_agents.firm_profiles import FirmProfileRegistry

            reg = FirmProfileRegistry.load(data_dir)
            built = reg.build_from_ecosystem(
                data_dir,
                competitors=list(self.config.industry.competitors or []),
                default_role="competitor",
            )
            out["profiles"] = {
                "count": built.get("count"),
                "path": built.get("path"),
            }
        if getattr(self.config.agents, "post_enrich_resolve_duplicates", False):
            from market_agents.entity_resolution import resolve_golden_records

            out["entity_resolution"] = resolve_golden_records(data_dir, dry_run=False)
        if getattr(self.config.agents, "post_enrich_suppliers", True):
            from market_agents.suppliers import SupplierRegistry

            out["suppliers"] = SupplierRegistry.load(data_dir).refresh_and_save(data_dir)
        if getattr(self.config.agents, "post_enrich_crm_tasks", True):
            from market_agents.crm_tasks import create_tasks_from_prospect_scoreboard

            crm = create_tasks_from_prospect_scoreboard(
                data_dir,
                min_opportunity=float(
                    getattr(self.config.agents, "crm_min_opportunity", 60.0) or 60.0
                ),
            )
            out["crm"] = crm
        return out
