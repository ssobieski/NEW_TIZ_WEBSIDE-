from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from market_agents.models import MarketItem, MonitoringReport, utc_now


class Storage:
    def __init__(self, data_dir: Path, report_dir: Path) -> None:
        self.data_dir = data_dir
        self.report_dir = report_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.items_path = self.data_dir / "items.jsonl"
        self.seen_path = self.data_dir / "seen_urls.txt"

    def load_seen(self) -> set[str]:
        if not self.seen_path.exists():
            return set()
        return {
            line.strip()
            for line in self.seen_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }

    def mark_seen(self, urls: list[str]) -> None:
        existing = self.load_seen()
        existing.update(urls)
        self.seen_path.write_text("\n".join(sorted(existing)) + "\n", encoding="utf-8")

    def append_items(self, items: list[MarketItem]) -> None:
        with self.items_path.open("a", encoding="utf-8") as fh:
            for item in items:
                fh.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")

    def save_report(self, report: MonitoringReport) -> tuple[Path, Path]:
        stamp = utc_now().strftime("%Y%m%d_%H%M%S")
        json_path = self.report_dir / f"report_{stamp}.json"
        md_path = self.report_dir / f"report_{stamp}.md"
        json_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        md_path.write_text(report.summary_markdown, encoding="utf-8")
        (self.report_dir / "latest.md").write_text(
            report.summary_markdown, encoding="utf-8"
        )
        return json_path, md_path

    def export_snapshot(self, payload: dict[str, Any]) -> Path:
        stamp = utc_now().strftime("%Y%m%d_%H%M%S")
        path = self.data_dir / f"run_{stamp}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
