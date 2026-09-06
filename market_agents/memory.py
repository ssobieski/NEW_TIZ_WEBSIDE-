from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from market_agents.models import utc_now


class MarketMemory:
    """Lekka pamięć rynkowa (JSONL) — fakty, encje, decyzje agentów."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "memory.jsonl"
        data_dir.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def add(self, kind: str, content: str, meta: dict[str, Any] | None = None) -> None:
        row = {
            "ts": utc_now().isoformat(),
            "kind": kind,
            "content": content,
            "meta": meta or {},
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        q = query.lower().strip()
        if not q or not self.path.exists():
            return []
        hits: list[dict[str, Any]] = []
        tokens = [t for t in q.split() if len(t) > 2]
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = f"{row.get('kind', '')} {row.get('content', '')}".lower()
                score = sum(1 for t in tokens if t in text)
                if score:
                    row["_score"] = score
                    hits.append(row)
        hits.sort(key=lambda r: r.get("_score", 0), reverse=True)
        return hits[:limit]

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.path.exists():
            return rows
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows[-limit:]
