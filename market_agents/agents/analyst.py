from __future__ import annotations

from market_agents.config import AppConfig
from market_agents.llm import LocalLLM
from market_agents.models import MarketItem
from market_agents.ontology import attach_domain_context


class AnalystAgent:
    """Lokalny analityk: relevance + kontekst tech (materiały/maszyny/chłodziwo/procesy)."""

    SYSTEM = (
        "Jesteś analitykiem rynku narzędzi skrawających. Pracujesz lokalnie. "
        "Zawsze uwzględniaj kontekst technologiczny: materiały (ISO P/M/K/N/S/H), "
        "maszyny, chłodziwo (emulsion/MQL/dry), procesy (toczenie/frezowanie/wiercenie). "
        "Nie kopiuj tabel vc/fz — tylko schemat i znaczenie biznesowe. Odpowiadaj po polsku."
    )

    def __init__(self, config: AppConfig, llm: LocalLLM) -> None:
        self.config = config
        self.llm = llm

    def analyze_batch(self, items: list[MarketItem], limit: int = 12) -> list[MarketItem]:
        analyzed: list[MarketItem] = []
        for item in items[:limit]:
            # zawsze wzbogacaj kontekst tech (nawet bez LLM)
            domain = attach_domain_context(item)
            domain_line = domain.get("summary") or "brak"
            prompt = (
                f"Branża: {self.config.industry.name}\n"
                f"Słowa kluczowe: {', '.join(self.config.industry.keywords)}\n\n"
                f"Tytuł: {item.title}\n"
                f"Źródło: {item.source}\n"
                f"URL: {item.url}\n"
                f"Kontekst tech (auto): {domain_line}\n"
                f"Materiały: {', '.join(domain.get('materials') or []) or '—'}\n"
                f"Maszyny: {', '.join(domain.get('machines') or []) or '—'}\n"
                f"Chłodziwo: {', '.join(domain.get('coolants') or []) or '—'}\n"
                f"Procesy: {', '.join(domain.get('processes') or []) or '—'}\n"
                f"Treść: {item.content or item.summary}\n\n"
                "Zwróć JSON o polach:\n"
                "{\n"
                '  "signal_type": "threat|opportunity|competitor|product_tech|literature|trend|noise",\n'
                '  "impact": "high|medium|low",\n'
                '  "why_it_matters": "1-2 zdania z kontekstem tech jeśli obecny",\n'
                '  "materials": ["iso-p", "..."],\n'
                '  "machines": ["cnc-lathe", "..."],\n'
                '  "coolants": ["mql", "..."],\n'
                '  "processes": ["milling", "..."],\n'
                '  "action": "konkretna rekomendacja lub null",\n'
                '  "relevance_0_to_1": 0.0\n'
                "}"
            )
            try:
                result = self.llm.analyze_json(self.SYSTEM, prompt)
                if result.get("parse_error"):
                    item.analysis = {
                        **(item.analysis or {}),
                        "why_it_matters": result.get("raw", "")[:400],
                        "domain_context": (item.analysis or {}).get("domain_context"),
                    }
                else:
                    prev_domain = (item.analysis or {}).get("domain_context")
                    item.analysis = result
                    if prev_domain:
                        item.analysis["domain_context"] = prev_domain
                    if isinstance(result.get("relevance_0_to_1"), (int, float)):
                        item.relevance_score = max(
                            item.relevance_score, float(result["relevance_0_to_1"])
                        )
            except Exception as exc:  # noqa: BLE001
                item.analysis = {
                    **(item.analysis or {}),
                    "signal_type": "noise",
                    "impact": "low",
                    "why_it_matters": f"Analiza LLM niedostępna: {exc}",
                    "action": None,
                    "domain_context": (item.analysis or {}).get("domain_context") or domain,
                }
            analyzed.append(item)
        analyzed.extend(items[limit:])
        return analyzed
