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
        self.storage.export_snapshot(
            {
                "industry": self.config.industry.name,
                "new_items": len(items),
                "mode": mode,
                "report_md": str(md_path),
                "trace": str(trace_path) if trace_path else None,
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
