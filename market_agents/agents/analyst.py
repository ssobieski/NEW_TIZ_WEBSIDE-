from __future__ import annotations

from market_agents.config import AppConfig
from market_agents.llm import LocalLLM
from market_agents.models import MarketItem


class AnalystAgent:
    """Lokalny analityk: relevance, typ sygnału, wpływ na branżę."""

    SYSTEM = (
        "Jesteś analitykiem rynku. Pracujesz lokalnie, bez ujawniania danych poza ten system. "
        "Oceniaj tylko na podstawie podanego tekstu. Odpowiadaj po polsku."
    )

    def __init__(self, config: AppConfig, llm: LocalLLM) -> None:
        self.config = config
        self.llm = llm

    def analyze_batch(self, items: list[MarketItem], limit: int = 12) -> list[MarketItem]:
        analyzed: list[MarketItem] = []
        for item in items[:limit]:
            prompt = (
                f"Branża: {self.config.industry.name}\n"
                f"Słowa kluczowe: {', '.join(self.config.industry.keywords)}\n\n"
                f"Tytuł: {item.title}\n"
                f"Źródło: {item.source}\n"
                f"URL: {item.url}\n"
                f"Treść: {item.content or item.summary}\n\n"
                "Zwróć JSON o polach:\n"
                "{\n"
                '  "signal_type": "threat|opportunity|competitor|regulation|trend|noise",\n'
                '  "impact": "high|medium|low",\n'
                '  "why_it_matters": "1-2 zdania",\n'
                '  "action": "konkretna rekomendacja lub null",\n'
                '  "relevance_0_to_1": 0.0\n'
                "}"
            )
            try:
                result = self.llm.analyze_json(self.SYSTEM, prompt)
                if result.get("parse_error"):
                    item.analysis = {"why_it_matters": result.get("raw", "")[:400]}
                else:
                    item.analysis = result
                    if isinstance(result.get("relevance_0_to_1"), (int, float)):
                        item.relevance_score = max(
                            item.relevance_score, float(result["relevance_0_to_1"])
                        )
            except Exception as exc:  # noqa: BLE001
                item.analysis = {
                    "signal_type": "noise",
                    "impact": "low",
                    "why_it_matters": f"Analiza LLM niedostępna: {exc}",
                    "action": None,
                }
            analyzed.append(item)
        analyzed.extend(items[limit:])
        return analyzed
