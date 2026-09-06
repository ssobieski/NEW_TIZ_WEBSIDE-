from __future__ import annotations

from market_agents.config import AppConfig
from market_agents.llm import LocalLLM
from market_agents.models import MarketItem, MonitoringReport, utc_now


class ReporterAgent:
    """Składa briefing rynkowy w Markdown + strukturę JSON."""

    SYSTEM = (
        "Jesteś dyrektorem wywiadu rynkowego. Piszesz zwięzłe briefy po polsku. "
        "Bez lania wody. Skup się na decyzjach biznesowych."
    )

    def __init__(self, config: AppConfig, llm: LocalLLM) -> None:
        self.config = config
        self.llm = llm

    def build(self, items: list[MarketItem]) -> MonitoringReport:
        highlights: list[str] = []
        threats: list[str] = []
        opportunities: list[str] = []
        competitor_moves: list[str] = []

        for item in items:
            analysis = item.analysis or {}
            line = f"{item.title} — {analysis.get('why_it_matters', item.summary[:160])}"
            signal = str(analysis.get("signal_type", "")).lower()
            if signal == "threat":
                threats.append(line)
            elif signal == "opportunity":
                opportunities.append(line)
            elif signal == "competitor":
                competitor_moves.append(line)
            else:
                highlights.append(line)

        digest = self._llm_digest(items) if items else "Brak nowych sygnałów w tym cyklu."

        md_parts = [
            f"# Monitoring rynku: {self.config.industry.name}",
            "",
            f"_Wygenerowano: {utc_now().isoformat()}_",
            "",
            "## Executive summary",
            digest,
            "",
            "## Zagrożenia",
            *(f"- {t}" for t in (threats or ["Brak"])),
            "",
            "## Szanse",
            *(f"- {o}" for o in (opportunities or ["Brak"])),
            "",
            "## Ruchy konkurencji",
            *(f"- {c}" for c in (competitor_moves or ["Brak"])),
            "",
            "## Pozostałe sygnały",
            *(f"- {h}" for h in (highlights[:10] or ["Brak"])),
            "",
            "## Źródła",
        ]
        for item in items[:25]:
            md_parts.append(f"- [{item.title}]({item.url}) ({item.source})")

        return MonitoringReport(
            industry=self.config.industry.name,
            generated_at=utc_now().isoformat(),
            item_count=len(items),
            highlights=highlights[:15],
            threats=threats[:15],
            opportunities=opportunities[:15],
            competitor_moves=competitor_moves[:15],
            summary_markdown="\n".join(md_parts),
            items=[i.to_dict() for i in items],
        )

    def _llm_digest(self, items: list[MarketItem]) -> str:
        bullets = []
        for item in items[:15]:
            why = (item.analysis or {}).get("why_it_matters", item.summary[:180])
            bullets.append(f"- {item.title}: {why}")
        prompt = (
            f"Branża: {self.config.industry.name}\n"
            "Na podstawie sygnałów napisz 5-8 zdań briefu: co się dzieje, "
            "co obserwować, co zrobić w tym tygodniu.\n\n"
            + "\n".join(bullets)
        )
        try:
            return self.llm.chat(self.SYSTEM, prompt)
        except Exception as exc:  # noqa: BLE001
            return (
                f"Zebrano {len(items)} sygnałów. "
                f"Podsumowanie LLM niedostępne ({exc}). "
                "Sprawdź sekcje poniżej."
            )
