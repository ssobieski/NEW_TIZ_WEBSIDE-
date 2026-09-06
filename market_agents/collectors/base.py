from __future__ import annotations

from abc import ABC, abstractmethod

from market_agents.models import MarketItem


class BaseCollector(ABC):
    name: str = "base"

    @abstractmethod
    def collect(self, max_items: int = 15) -> list[MarketItem]:
        raise NotImplementedError
