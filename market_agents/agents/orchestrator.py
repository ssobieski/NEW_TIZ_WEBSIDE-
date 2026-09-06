from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from market_agents.agents.analyst import AnalystAgent
from market_agents.agents.collector import CollectorAgent
from market_agents.agents.reporter import ReporterAgent
from market_agents.config import AppConfig, load_config
from market_agents.llm import LocalLLM
from market_agents.models import MonitoringReport
from market_agents.storage import Storage


@dataclass
class RunResult:
    report: MonitoringReport
    json_path: Path
    md_path: Path
    new_items: int


class Orchestrator:
    """Orkiestruje lokalnych agentów: zbierz → analizuj → raportuj."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.storage = Storage(config.data_path, config.report_path)
        self.llm = LocalLLM(config.llm)
        self.collector = CollectorAgent(config, self.storage)
        self.analyst = AnalystAgent(config, self.llm)
        self.reporter = ReporterAgent(config, self.llm)

    @classmethod
    def from_path(cls, path: str | Path) -> "Orchestrator":
        return cls(load_config(path))

    def health(self) -> dict:
        return self.llm.healthcheck()

    def run(self, skip_llm: bool = False) -> RunResult:
        items = self.collector.run()
        if not skip_llm and items:
            items = self.analyst.analyze_batch(items)
        report = self.reporter.build(items)
        json_path, md_path = self.storage.save_report(report)
        if items:
            self.storage.append_items(items)
            self.storage.mark_seen([i.url for i in items])
        self.storage.export_snapshot(
            {
                "industry": self.config.industry.name,
                "new_items": len(items),
                "report_md": str(md_path),
            }
        )
        return RunResult(
            report=report,
            json_path=json_path,
            md_path=md_path,
            new_items=len(items),
        )
