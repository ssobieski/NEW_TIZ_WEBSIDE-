from __future__ import annotations

import json
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from market_agents.config import LlmConfig


def is_retryable_llm_error(exc: BaseException) -> bool:
    """Retry timeouts and 5xx/429 — never retry 4xx schema/payload errors."""
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code if exc.response is not None else 0
        return code >= 500 or code == 429
    return False


def _raise_for_status_with_body(response: httpx.Response) -> None:
    if response.is_success:
        return
    body = (response.text or "").strip().replace("\n", " ")[:800]
    detail = f"{response.status_code} {response.request.url}"
    if body:
        detail = f"{detail}: {body}"
    raise httpx.HTTPStatusError(
        detail,
        request=response.request,
        response=response,
    )


class LocalLLM:
    """Klient lokalnego LLM z tool-calling (vLLM / Ollama / OpenAI-compatible)."""

    def __init__(self, config: LlmConfig) -> None:
        self.config = config

    @property
    def _is_ollama(self) -> bool:
        return self.config.provider == "ollama"

    @property
    def _openai_base(self) -> str:
        base = self.config.base_url.rstrip("/")
        if self._is_ollama:
            return f"{base}/v1"
        if base.endswith("/v1"):
            return base
        return f"{base}/v1"

    def chat(self, system: str, user: str) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        result = self.chat_messages(messages)
        return str(result.get("content") or "").strip()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception(is_retryable_llm_error),
        reraise=True,
    )
    def chat_messages(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._is_ollama and not tools:
            return {"role": "assistant", "content": self._ollama_native(messages)}

        url = f"{self._openai_base}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        payload: dict[str, Any] = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice

        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
            _raise_for_status_with_body(response)
            data = response.json()
        return data["choices"][0]["message"]

    def _ollama_native(self, messages: list[dict[str, Any]]) -> str:
        url = f"{self.config.base_url.rstrip('/')}/api/chat"
        payload = {
            "model": self.config.model,
            "stream": False,
            "options": {"temperature": self.config.temperature},
            "messages": [
                {"role": m["role"], "content": m.get("content") or ""}
                for m in messages
                if m.get("role") in {"system", "user", "assistant"}
            ],
        }
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.post(url, json=payload)
            _raise_for_status_with_body(response)
            data = response.json()
        return str(data.get("message", {}).get("content", "")).strip()

    def healthcheck(self) -> dict[str, Any]:
        try:
            if self._is_ollama:
                with httpx.Client(timeout=5) as client:
                    r = client.get(f"{self.config.base_url.rstrip('/')}/api/tags")
                    r.raise_for_status()
                    models = [m.get("name") for m in r.json().get("models", [])]
                return {
                    "ok": True,
                    "provider": "ollama",
                    "models": models,
                    "hint": "Dla 4x A100 lepiej vLLM — bash scripts/run_vllm_a100.sh",
                }

            with httpx.Client(timeout=8) as client:
                r = client.get(f"{self._openai_base}/models")
                r.raise_for_status()
                models = [m.get("id") for m in r.json().get("data", [])]
            return {
                "ok": True,
                "provider": self.config.provider,
                "models": models,
                "tensor_parallel_size": self.config.tensor_parallel_size,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": str(exc),
                "hint": (
                    "Uruchom vLLM na Dellu (VPN Cybertech): bash scripts/run_vllm_a100.sh "
                    f"(TP={self.config.tensor_parallel_size}). "
                    "Klient: export VLLM_BASE_URL=http://<dell-vpn-ip>:8000 "
                    "&& python -m market_agents doctor"
                ),
            }

    def analyze_json(self, system: str, user: str) -> dict[str, Any]:
        raw = self.chat(system, user + "\n\nOdpowiedz TYLKO poprawnym JSON bez markdown.")
        return _parse_json_loose(raw)


def _parse_json_loose(raw: str) -> dict[str, Any]:
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
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
        return {"raw": raw, "parse_error": True}
