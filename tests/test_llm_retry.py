from __future__ import annotations

import httpx
import pytest

from market_agents.agents.parsing_agent import (
    OLD_TOOL_RESULT_CONTEXT_CHARS,
    RECENT_TOOL_RESULTS_TO_KEEP,
    _compact_tool_context,
)
from market_agents.agents.orchestrator import _is_notion_abort_briefing
from market_agents.llm import _raise_for_status_with_body, is_retryable_llm_error


def test_400_is_not_retryable():
    req = httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions")
    resp = httpx.Response(400, request=req, text='{"error":"bad schema"}')
    err = httpx.HTTPStatusError("400", request=req, response=resp)
    assert is_retryable_llm_error(err) is False


def test_429_and_503_are_retryable():
    req = httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions")
    for code in (429, 503):
        resp = httpx.Response(code, request=req, text="busy")
        err = httpx.HTTPStatusError(str(code), request=req, response=resp)
        assert is_retryable_llm_error(err) is True


def test_timeout_is_retryable():
    assert is_retryable_llm_error(httpx.TimeoutException("t")) is True


def test_raise_for_status_includes_body():
    req = httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions")
    resp = httpx.Response(400, request=req, text='{"message":"tool_choice invalid"}')
    with pytest.raises(httpx.HTTPStatusError) as ei:
        _raise_for_status_with_body(resp)
    assert "tool_choice invalid" in str(ei.value)


def test_notion_abort_briefing_detected():
    text = """### BŁĄD
- **Błąd**: Brak NOTION_TOKEN — ustaw env z tokenem integracji Notion.

### NASTĘPNE KROKI
1. **Ustaw NOTION_TOKEN**
"""
    assert _is_notion_abort_briefing(text) is True
    assert (
        _is_notion_abort_briefing("Pełny briefing. Notion opcjonalne, idziemy dalej.")
        is False
    )


def test_old_tool_results_are_compacted_but_recent_results_stay_complete():
    messages = [{"role": "system", "content": "system"}]
    for i in range(RECENT_TOOL_RESULTS_TO_KEEP + 2):
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call-{i}",
                "content": str(i) * (OLD_TOOL_RESULT_CONTEXT_CHARS + 500),
            }
        )

    _compact_tool_context(messages)

    tool_messages = [m for m in messages if m["role"] == "tool"]
    assert len(tool_messages[0]["content"]) < OLD_TOOL_RESULT_CONTEXT_CHARS + 100
    assert "skrócony" in tool_messages[0]["content"]
    assert len(tool_messages[-1]["content"]) == OLD_TOOL_RESULT_CONTEXT_CHARS + 500
