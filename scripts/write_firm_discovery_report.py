#!/usr/bin/env python3
"""Zbuduj krótki raport markdown z live firm_discovery (bez LLM) do iteracji."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.eval_firm_discovery_iter import main as eval_main, score_items, replay_gold
from market_agents.config import load_config
from market_agents.firms import KnownFirmsIndex
from market_agents.collectors.firm_discovery import FirmDiscoveryCollector


def build_report() -> Path:
    cfg = load_config("config/industry.yaml")
    known = KnownFirmsIndex.load(
        data_dir=cfg.agents.data_dir,
        competitors=cfg.industry.competitors,
    )
    fd = cfg.sources.firm_discovery
    items = FirmDiscoveryCollector(fd, known, lookback_hours=cfg.agents.lookback_hours).collect(
        max_items=int(fd.max_items or 20)
    )
    sc = score_items(items)
    gold_fails = replay_gold()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(cfg.agents.report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"firm_discovery_iter_{ts}.md"
    lines = [
        f"# Firm discovery iter — {cfg.industry.name}",
        "",
        f"_Wygenerowano: {datetime.now(timezone.utc).isoformat()}_",
        "",
        f"- items: **{sc['n']}**",
        f"- noise_rate: **{sc['noise_rate']:.2f}**",
        f"- gold_fails: {gold_fails or '[]'}",
        f"- rss/media/social/lit: {len(cfg.sources.rss)}/"
        f"{len(cfg.sources.media)}/{len(cfg.sources.social)}/{len(cfg.sources.literature)}",
        "",
        "## Signal (gold)",
    ]
    for row in sc["signal"]:
        lines.append(f"- {', '.join(map(str, row['firms']))} — {row['title']}")
    lines += ["", "## Kandydaci"]
    for row in sc["unknown"]:
        lines.append(f"- {', '.join(map(str, row['firms']))} — {row['title']}")
    lines += ["", "## Noise"]
    if not sc["noise"]:
        lines.append("- Brak")
    else:
        for row in sc["noise"]:
            lines.append(f"- {row}")
    lines += ["", "## Wszystkie headlines"]
    for it in items:
        firms = [f.get("company") for f in ((it.analysis or {}).get("new_firms") or [])]
        headline = (it.analysis or {}).get("headline") or it.title
        lines.append(f"- `{firms}` — {headline}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    latest = out_dir / "firm_discovery_iter_latest.md"
    latest.write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")
    meta = {
        "n": sc["n"],
        "noise_rate": sc["noise_rate"],
        "gold_fails": gold_fails,
        "path": str(md_path),
    }
    print(json.dumps(meta, ensure_ascii=False))
    return md_path


if __name__ == "__main__":
    build_report()
