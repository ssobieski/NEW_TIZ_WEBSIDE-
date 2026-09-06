from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from market_agents.config import AppConfig
from market_agents.llm import LocalLLM
from market_agents.memory import MarketMemory
from market_agents.models import MarketItem, utc_now
from market_agents.tools import ToolRegistry


@dataclass
class AgentTraceStep:
    step: int
    assistant: dict[str, Any]
    tool_results: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AgenticResult:
    items: list[MarketItem]
    structured: list[dict[str, Any]]
    final_text: str
    steps: list[AgentTraceStep]
    trace_path: Path | None = None


class ParsingAgent:
    """
    Agentic ReAct loop z tool-calling.
    Model sam wybiera: parse, batch_parse, extract_market_intel, memory.
    Cel: pełne wykorzystanie lokalnego vLLM na 4x A100.
    """

    def __init__(self, config: AppConfig, llm: LocalLLM, memory: MarketMemory) -> None:
        self.config = config
        self.llm = llm
        self.memory = memory

    def run(self, candidates: list[MarketItem]) -> AgenticResult:
        tools = ToolRegistry(self.config, self.memory, candidates=candidates)
        industry = self.config.industry
        system = (
            "Jesteś agentem wywiadu rynkowego TIZ (narzędzia skrawające) na lokalnym GPU. "
            "Masz narzędzia do parsowania WWW ORAZ dostęp do istniejącej bazy Notion "
            "i archiwum Cloudflare R2.\n"
            "Procedura:\n"
            "1) list_candidates — przejrzyj świeże sygnały\n"
            "2) search_notion — sprawdź katalogi konkurencji / handbooki / ISO 13399\n"
            "3) search_r2 — jeśli włączone, przeszukaj duże archiwum plików\n"
            "4) search_memory — kontekst z poprzednich cykli\n"
            "5) batch_parse / fetch_and_parse / fetch_notion_page — głęboki research\n"
            "6) extract_market_intel — ustrukturyzuj wnioski\n"
            "7) remember — zapisz kluczowe fakty\n"
            "Na końcu napisz zwięzły briefing po polsku (bez JSON).\n"
            "Priorytet: katalogi/gatunki/geometrie konkurencji, kalkulatory online, "
            "ISO 13399/GTC, luki vs TIZ. Ignoruj spam i oferty pracy."
        )
        user = (
            f"Branża: {industry.name}\n"
            f"Język: {industry.language}\n"
            f"Keywords: {', '.join(industry.keywords)}\n"
            f"Konkurenci: {', '.join(industry.competitors)}\n"
            f"Pytania fokusowe: {'; '.join(industry.focus_questions) or 'brak'}\n"
            f"Notion enabled: {self.config.sources.notion.enabled}\n"
            f"R2 enabled: {self.config.sources.cloudflare_r2.enabled}\n"
            f"Liczba kandydatów: {len(candidates)}\n"
            f"Limit głębokich parse: {self.config.agents.agentic.max_deep_parses}\n"
            "Zacznij od list_candidates, potem search_notion dla kontekstu TIZ."
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        schema = tools.openai_tools_schema()
        steps: list[AgentTraceStep] = []
        final_text = ""
        max_steps = self.config.agents.agentic.max_steps

        for step_idx in range(1, max_steps + 1):
            tool_choice: str | dict[str, Any] | None = "auto"
            if self.config.agents.agentic.require_tool_use and step_idx == 1:
                tool_choice = "required"

            message = self.llm.chat_messages(
                messages, tools=schema, tool_choice=tool_choice
            )
            messages.append(message)
            tool_calls = message.get("tool_calls") or []
            step = AgentTraceStep(step=step_idx, assistant=message, tool_results=[])

            if not tool_calls:
                final_text = str(message.get("content") or "").strip()
                steps.append(step)
                break

            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                raw_args = fn.get("arguments") or "{}"
                result = tools.call(name, raw_args)
                step.tool_results.append({"tool": name, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or f"call_{step_idx}_{name}",
                        "content": json.dumps(result, ensure_ascii=False)[:12000],
                    }
                )
            steps.append(step)

            if len(tools.structured) >= min(5, max(1, len(candidates) // 2)) and step_idx >= 3:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Masz już wystarczająco ustrukturyzowanego intelu. "
                            "Zakończ research i napisz finalny briefing (bez tool calli)."
                        ),
                    }
                )

        if not final_text:
            messages.append(
                {
                    "role": "user",
                    "content": "Zakończ. Napisz finalny briefing rynkowy po polsku, bez tool calli.",
                }
            )
            closing = self.llm.chat_messages(messages)
            final_text = str(closing.get("content") or "").strip()
            steps.append(AgentTraceStep(step=len(steps) + 1, assistant=closing))

        trace_path = self._save_trace(steps, final_text)
        return AgenticResult(
            items=candidates,
            structured=tools.structured,
            final_text=final_text,
            steps=steps,
            trace_path=trace_path,
        )

    def _save_trace(self, steps: list[AgentTraceStep], final_text: str) -> Path:
        trace_dir = Path(self.config.agents.agentic.trace_dir)
        trace_dir.mkdir(parents=True, exist_ok=True)
        stamp = utc_now().strftime("%Y%m%d_%H%M%S")
        path = trace_dir / f"trace_{stamp}.json"
        payload = {
            "generated_at": utc_now().isoformat(),
            "final_text": final_text,
            "steps": [
                {
                    "step": s.step,
                    "assistant": s.assistant,
                    "tool_results": s.tool_results,
                }
                for s in steps
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
