from __future__ import annotations

from pathlib import Path

from market_agents.config import (
    AgentsConfig,
    AppConfig,
    IndustryConfig,
    SourcesConfig,
)
from market_agents.firm_profiles import (
    FirmProfile,
    FirmProfileRegistry,
    extract_contacts_from_html,
    score_sentiment,
    verify_identity,
    compute_scorecard,
)
from market_agents.firm_relations import FirmRelationsGraph, RELATION_TYPES
from market_agents.memory import MarketMemory
from market_agents.tools import ToolRegistry


SAMPLE_HTML = """
<html><body>
<footer>
© 2024 Sandvik Coromant AB
Contact: info@sandvik.com Phone: +46 26 26 26 00
Street: Mossvägen 10, 811 81 Sandviken, Sweden
Revenue of EUR 12.5 billion in 2023.
<a href="mailto:sales@sandvik.com">sales</a>
</footer>
</body></html>
"""


def test_extract_contacts_and_finance():
    out = extract_contacts_from_html(
        SAMPLE_HTML, base_url="https://www.sandvik.coromant.com/", company="Sandvik"
    )
    assert any("sandvik.com" in e["value"] for e in out["emails"])
    assert out["phones"]
    assert out["financial_signals"]
    assert out["websites"][0]["url"].startswith("https://")


def test_sentiment_and_scorecard():
    sent = score_sentiment(
        ["Award for innovation and growth partnership", "Company faces lawsuit and fine"]
    )
    assert sent["sample_size"] == 2
    assert sent["label"] in {"mixed", "neutral", "positive", "negative"}

    p = FirmProfile(
        id="demo",
        company="Demo Tools",
        roles=["competitor"],
        country="DE",
        websites=[{"url": "https://demo.example", "primary": True, "status": "heuristic"}],
        emails=[{"value": "info@demo.example", "status": "heuristic"}],
        phones=[{"value": "+491234567890", "status": "heuristic"}],
        addresses=[{"raw": "ul. Testowa 1, 00-001 Warszawa", "status": "heuristic"}],
        assets={
            "catalogs": [{"url": "https://demo.example/cat.pdf"}],
            "pricelists": [{"url": "https://demo.example/price.pdf"}],
            "eshops": [{"url": "https://demo.example/shop"}],
            "leaflets": [],
        },
        network={"distributors": ["Hoffmann Group"], "customers": ["ACME"]},
        public_relations={"mentions": [{"title": "launch"}], "sentiment": sent},
        financial={"signals": [{"metric": "public_revenue_mention", "value_text": "EUR 1m"}]},
    )
    p.verification = verify_identity(p)
    p.scores = compute_scorecard(p)
    assert p.scores["overall"] > 0
    assert p.scores["table"]
    assert p.verification["score"] >= 0


def test_build_profiles_from_ecosystem(tmp_path: Path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir(parents=True)
    # known firms
    (knowledge / "known_firms.json").write_text(
        '[{"company":"Sandvik Coromant","country":"SE","site_url":"https://www.sandvik.coromant.com/","product_focus":"inserts"}]',
        encoding="utf-8",
    )
    # relations
    g = FirmRelationsGraph()
    g.add_relation(
        "Hoffmann Group",
        "Sandvik Coromant",
        "distributor_of",
        evidence="test",
        confidence=0.8,
    )
    g.add_relation(
        "Hoffmann Group",
        "Sandvik Coromant",
        "customer_of",
        evidence="buys tooling",
        confidence=0.7,
    )
    g.add_relation(
        "Sandvik Coromant",
        "Kennametal",
        "competes_with",
        evidence="OEM rivals",
        confidence=0.9,
    )
    g.save(tmp_path)
    # social
    (knowledge / "social_mentions.json").write_text(
        '{"mentions":[{"company":"Sandvik Coromant","platform":"linkedin","title":"Expansion award","snippet":"growth innovation partnership"}]}',
        encoding="utf-8",
    )

    reg = FirmProfileRegistry()
    result = reg.build_from_ecosystem(tmp_path, competitors=["Kennametal"], default_role="competitor")
    assert result["ok"] is True
    assert result["count"] >= 2
    sc = reg.get("Sandvik Coromant")
    assert sc is not None
    assert sc.network.get("distributors") or sc.network.get("customers") or sc.network.get("competitors")
    assert sc.public_relations.get("sentiment")
    assert sc.scores.get("overall") is not None
    assert (tmp_path / "knowledge" / "firm_profiles.json").is_file()


def test_relation_types_include_customer_supplier():
    assert "customer_of" in RELATION_TYPES
    assert "supplier_of" in RELATION_TYPES
    assert "competes_with" in RELATION_TYPES


def test_tools_profiles(tmp_path: Path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "known_firms.json").write_text(
        '[{"company":"Ceratizit","country":"LU","site_url":"https://www.ceratizit.com/","product_focus":"carbide"}]',
        encoding="utf-8",
    )
    cfg = AppConfig(
        industry=IndustryConfig(name="Test", keywords=["cutting"], competitors=["Ceratizit"]),
        sources=SourcesConfig(),
        agents=AgentsConfig(data_dir=str(tmp_path)),
    )
    tools = ToolRegistry(cfg, MarketMemory(tmp_path))
    built = tools.call("build_firm_profiles", {})
    assert built["ok"] is True
    listed = tools.call("list_firm_profiles", {"limit": 10})
    assert listed["ok"] is True
    assert listed["count"] >= 1
    board = tools.call("firm_scoreboard", {"limit": 5})
    assert board["ok"] is True
    up = tools.call(
        "upsert_firm_profile",
        {
            "company": "Ceratizit",
            "email": "info@ceratizit.com",
            "phone": "+35212345678",
            "address": "ul. Example 1, 00-001 City",
            "roles": ["competitor", "supplier"],
        },
    )
    assert up["ok"] is True
    got = tools.call("get_firm_profile", {"company": "Ceratizit"})
    assert got["ok"] is True
    assert got["profile"]["emails"]
    social = tools.call(
        "register_social_mention",
        {
            "company": "Ceratizit",
            "platform": "linkedin",
            "title": "New product launch",
            "snippet": "innovation award growth",
        },
    )
    assert social["ok"] is True
    assert "governance" not in social or social.get("ok")


def test_merge_preserves_downloads_and_dedupes_signals():
    old = FirmProfile(
        id="acme",
        company="Acme",
        roles=["competitor"],
        assets={
            "downloads": [{"url": "https://ex.com/a.pdf", "title": "A"}],
            "catalogs": [{"url": "https://ex.com/c.pdf"}],
        },
        financial={
            "signals": [{"metric": "revenue", "value_text": "10m"}, {"metric": "employees", "value_text": "100"}]
        },
    )
    new = FirmProfile(
        id="acme",
        company="Acme",
        roles=["supplier"],
        assets={
            "catalogs": [{"url": "https://ex.com/c2.pdf"}],
            "pricelists": [{"url": "https://ex.com/p.pdf"}],
        },
        financial={
            "signals": [{"metric": "revenue", "value_text": "10m"}, {"metric": "revenue", "value_text": "12m"}]
        },
    )
    merged = FirmProfileRegistry._merge(old, new)
    assert any(d.get("url") == "https://ex.com/a.pdf" for d in merged.assets.get("downloads") or [])
    assert len(merged.assets.get("catalogs") or []) == 2
    signals = (merged.financial or {}).get("signals") or []
    assert len(signals) == 3  # duplicate revenue|10m dropped
    keys = {f"{s.get('metric')}|{s.get('value_text')}" for s in signals}
    assert len(keys) == 3
