from __future__ import annotations

import json
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from market_agents.config import LlmConfig


class LocalLLM:
    """Klient LLM działający lokalnie przez Ollamę (lub kompatybilne API)."""

    def __init__(self, config: LlmConfig) -> None:
        self.config = config

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def chat(self, system: str, user: str) -> str:
        if self.config.provider == "ollama":
            return self._ollama_chat(system, user)
        return self._openai_compatible_chat(system, user)

    def _ollama_chat(self, system: str, user: str) -> str:
        url = f"{self.config.base_url.rstrip('/')}/api/chat"
        payload = {
            "model": self.config.model,
            "stream": False,
            "options": {"temperature": self.config.temperature},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        return str(data.get("message", {}).get("content", "")).strip()

    def _openai_compatible_chat(self, system: str, user: str) -> str:
        url = f"{self.config.base_url.rstrip('/')}/v1/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        return str(data["choices"][0]["message"]["content"]).strip()

    def healthcheck(self) -> dict[str, Any]:
        try:
            if self.config.provider == "ollama":
                with httpx.Client(timeout=5) as client:
                    r = client.get(f"{self.config.base_url.rstrip('/')}/api/tags")
                    r.raise_for_status()
                    models = [m.get("name") for m in r.json().get("models", [])]
                return {"ok": True, "provider": "ollama", "models": models}
            return {"ok": True, "provider": self.config.provider, "models": [self.config.model]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def analyze_json(self, system: str, user: str) -> dict[str, Any]:
        raw = self.chat(system, user + "\n\nOdpowiedz TYLKO poprawnym JSON bez markdown.")
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                return json.loads(cleaned[start : end + 1])
            return {"raw": raw, "parse_error": True}
