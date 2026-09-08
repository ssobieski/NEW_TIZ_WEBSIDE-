"""Regression corpus for firm_discovery filters (keep/reject headlines + names)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from market_agents.config import load_config
from market_agents.firms import (
    KnownFirmsIndex,
    extract_candidate_firm_names,
    filter_new_firms,
    is_plausible_firm_name,
    is_tooling_relevant,
)

FIX = Path(__file__).parent / "fixtures" / "firm_discovery_headlines.json"
CORPUS = json.loads(FIX.read_text(encoding="utf-8"))


@pytest.mark.parametrize("text", CORPUS["reject_domain"])
def test_reject_domain(text: str):
    assert not is_tooling_relevant(text), text


@pytest.mark.parametrize("text", CORPUS["keep_domain"])
def test_keep_domain(text: str):
    assert is_tooling_relevant(text), text


@pytest.mark.parametrize("name", CORPUS["reject_names"])
def test_reject_names(name: str):
    assert not is_plausible_firm_name(name), name


@pytest.mark.parametrize("name", CORPUS["keep_names"])
def test_keep_names(name: str):
    assert is_plausible_firm_name(name), name


@pytest.mark.parametrize("case", CORPUS["extract_cases"], ids=lambda c: c["text"][:48])
def test_extract_cases(case: dict):
    names = extract_candidate_firm_names(case["text"])
    joined = " | ".join(names)
    for must in case.get("must_include") or []:
        assert any(must.lower() in n.lower() for n in names), f"missing {must} in {joined}"
    for bad in case.get("must_exclude") or []:
        assert all(bad.lower() != n.lower() for n in names), f"got excluded {bad} in {joined}"
        # also reject as standalone firm label
        if " " not in bad:
            assert bad not in names


def test_known_filter_with_tiz_competitors():
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    idx = KnownFirmsIndex.load(competitors=cfg.industry.competitors)
    for row in CORPUS["known_should_filter"]:
        got = idx.is_known(row["name"])
        assert got is row["known"], f"{row['name']}: expected known={row['known']} got={got}"


def test_filter_new_drops_known_and_noise():
    cfg = load_config("config/tiz_cutting_tools.example.yaml")
    idx = KnownFirmsIndex.load(competitors=cfg.industry.competitors)
    text = (
        "Sandvik Coromant and DIAEDGE and Leverages and PHOTOS appear in machining news"
    )
    names = extract_candidate_firm_names(text) + ["Sandvik Coromant", "DIAEDGE", "Leverages"]
    rows = filter_new_firms(names, idx, evidence=text, require_domain=True)
    companies = [r["company"] for r in rows]
    assert "Sandvik Coromant" not in companies
    assert "Leverages" not in companies
    assert any("DIAEDGE" in c for c in companies)
