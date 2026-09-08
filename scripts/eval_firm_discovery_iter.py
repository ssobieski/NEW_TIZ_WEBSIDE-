#!/usr/bin/env python3
"""Iteracyjna ewaluacja firm_discovery: zbierz → oceń → wypisz szum."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_agents.config import load_config
from market_agents.firms import (
    KnownFirmsIndex,
    extract_candidate_firm_names,
    filter_new_firms,
    is_plausible_firm_name,
    is_tooling_relevant,
)
from market_agents.collectors.firm_discovery import FirmDiscoveryCollector

# Ręcznie oznaczone z ostatnich raportów Dell
GOLD_KEEP = {
    "diaedge",
    "walter fmt",
    "toolpath",
    "heritage cutter",
    "msc",
}
GOLD_REJECT_EVIDENCE = [
    "PHOTOS: Mississauga introduces brand new grass cutting tools",
    "Berry Introduces Global Tooling Services",
    "Tooling manufacturer celebrates 50 years",
    "Groupe Chaumont launches Swiss watchmaking",
    "Haas F1 Team to Promote HaasTooling.com",
]
NOISE_NAME_RE = re.compile(
    r"(?i)^(photos|hypepotamus|mynewsdesk|mississauga|key acquisitions|"
    r"engineering news|plastics technology|fmt|pes|cnc|cam|"
    r"dry-cut|hobbing|helical|cemented carbide|global tooling)$"
)


def score_items(items) -> dict:
    signal, noise, unknown = [], [], []
    for it in items:
        firms = (it.analysis or {}).get("new_firms") or []
        headline = (it.analysis or {}).get("headline") or it.title
        blob = f"{headline} {it.summary or ''}"
        if not is_tooling_relevant(blob):
            noise.append({"why": "off_domain", "title": headline, "firms": firms})
            continue
        bad = False
        for f in firms:
            name = str(f.get("company") or "")
            if NOISE_NAME_RE.match(name.strip()) or not is_plausible_firm_name(name):
                noise.append({"why": "bad_name", "title": headline, "name": name})
                bad = True
                break
        if bad:
            continue
        norms = {str(f.get("normalized") or "") for f in firms}
        if norms & GOLD_KEEP:
            signal.append({"title": headline, "firms": [f.get("company") for f in firms]})
        else:
            unknown.append({"title": headline, "firms": [f.get("company") for f in firms]})
    return {
        "n": len(items),
        "signal": signal,
        "noise": noise,
        "unknown": unknown,
        "noise_rate": (len(noise) / len(items)) if items else 0.0,
    }


def replay_gold() -> list[str]:
    fails = []
    for ev in GOLD_REJECT_EVIDENCE:
        if is_tooling_relevant(ev):
            fails.append(f"should_reject_domain: {ev[:60]}")
    for good in (
        "DIAEDGE, A New Brand of Cemented Carbide Products",
        "Walter FMT: a new brand for lightweight machining",
        "MSC introduces new brand of cutting tools",
    ):
        if not is_tooling_relevant(good):
            fails.append(f"should_keep_domain: {good}")
    assert not is_plausible_firm_name("Hypepotamus")
    assert not is_plausible_firm_name("Key Acquisitions")
    assert is_plausible_firm_name("DIAEDGE")
    return fails


def main() -> int:
    cfg_path = Path("config/industry.yaml")
    if not cfg_path.exists():
        ex = Path("config/tiz_cutting_tools.example.yaml")
        cfg_path.write_text(ex.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = load_config(cfg_path)
    known = KnownFirmsIndex.load(
        data_dir=cfg.agents.data_dir,
        competitors=cfg.industry.competitors,
    )
    fd = cfg.sources.firm_discovery
    collector = FirmDiscoveryCollector(fd, known, lookback_hours=cfg.agents.lookback_hours)
    items = collector.collect(max_items=int(fd.max_items or 20))
    sc = score_items(items)
    gold_fails = replay_gold()
    out = {
        "rss": len(cfg.sources.rss),
        "media": len(cfg.sources.media),
        "gold_fails": gold_fails,
        "noise_rate": round(sc["noise_rate"], 3),
        "n": sc["n"],
        "signal": sc["signal"][:12],
        "noise": sc["noise"][:12],
        "unknown": sc["unknown"][:15],
        "headlines": [
            {
                "title": (it.analysis or {}).get("headline") or it.title,
                "firms": [f.get("company") for f in ((it.analysis or {}).get("new_firms") or [])],
            }
            for it in items
        ],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    # exit non-zero if gold regression or high noise
    if gold_fails or sc["noise_rate"] > 0.35:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
