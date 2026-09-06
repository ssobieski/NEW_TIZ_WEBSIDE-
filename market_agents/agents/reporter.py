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
            why = (
                analysis.get("why_it_matters")
                or analysis.get("summary")
                or (item.summary[:160] if item.summary else "")
            )
            domain = analysis.get("domain_context") or {}
            domain_bit = ""
            if isinstance(domain, dict) and domain.get("summary"):
                domain_bit = f" [{domain['summary']}]"
            line = f"{item.title} — {why}{domain_bit}"
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
        tech_section = self._tech_context_section(items)

        md_parts = [
            f"# Monitoring rynku: {self.config.industry.name}",
            "",
            f"_Wygenerowano: {utc_now().isoformat()}_",
            "",
            "## Executive summary",
            digest,
            "",
            "## Kontekst technologiczny (materiały / maszyny / chłodziwo / procesy)",
            tech_section,
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

    @staticmethod
    def _tech_context_section(items: list[MarketItem]) -> str:
        mats: set[str] = set()
        macs: set[str] = set()
        cools: set[str] = set()
        procs: set[str] = set()
        rows: list[str] = []
        for item in items:
            domain = (item.analysis or {}).get("domain_context") or {}
            if not isinstance(domain, dict):
                continue
            for x in domain.get("materials") or []:
                mats.add(str(x).split(":")[-1])
            for x in domain.get("machines") or []:
                macs.add(str(x).split(":")[-1])
            for x in domain.get("coolants") or []:
                cools.add(str(x).split(":")[-1])
            for x in domain.get("processes") or []:
                procs.add(str(x).split(":")[-1])
            summary = domain.get("summary") or ""
            if summary and summary != "brak wykrytego kontekstu technologicznego":
                rows.append(f"- **{item.title[:60]}**: {summary}")
        header = (
            f"- Materiały: {', '.join(sorted(mats)) or '—'}\n"
            f"- Maszyny: {', '.join(sorted(macs)) or '—'}\n"
            f"- Chłodziwo: {', '.join(sorted(cools)) or '—'}\n"
            f"- Procesy: {', '.join(sorted(procs)) or '—'}"
        )
        if not rows:
            return header + "\n\n_Brak szczegółowego kontekstu tech w tym cyklu._"
        return header + "\n\n" + "\n".join(rows[:12])

    def _llm_digest(self, items: list[MarketItem]) -> str:
        bullets = []
        for item in items[:15]:
            why = (item.analysis or {}).get("why_it_matters") or (
                item.analysis or {}
            ).get("summary") or item.summary[:180]
            domain = (item.analysis or {}).get("domain_context") or {}
            tech = ""
            if isinstance(domain, dict) and domain.get("summary"):
                tech = f" | tech: {domain['summary']}"
            bullets.append(f"- {item.title}: {why}{tech}")
        prompt = (
            f"Branża: {self.config.industry.name}\n"
            "Na podstawie sygnałów napisz 5-8 zdań briefu: co się dzieje, "
            "uwzględnij materiały/maszyny/chłodziwo/procesy gdy wykryte, "
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
