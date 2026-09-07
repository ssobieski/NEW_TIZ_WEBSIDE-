from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class MarketItem:
    title: str
    url: str
    source: str
    published_at: str | None = None
    summary: str = ""
    content: str = ""
    collected_at: str = field(default_factory=lambda: utc_now().isoformat())
    relevance_score: float = 0.0
    tags: list[str] = field(default_factory=list)
    analysis: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MonitoringReport:
    industry: str
    generated_at: str
    item_count: int
    highlights: list[str]
    threats: list[str]
    opportunities: list[str]
    competitor_moves: list[str]
    summary_markdown: str
    items: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
