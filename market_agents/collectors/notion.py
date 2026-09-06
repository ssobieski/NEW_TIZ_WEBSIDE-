from __future__ import annotations

import re
from typing import Any

import httpx

from market_agents.config import NotionConfig
from market_agents.models import MarketItem


NOTION_VERSION = "2022-06-28"


class NotionCollector:
    """
    Czyta istniejącą bazę Notion (katalogi konkurencji, literatura, review)
    i mapuje strony na MarketItem / wiedzę lokalną.
    """

    name = "notion"

    def __init__(self, config: NotionConfig, token: str) -> None:
        self.config = config
        self.token = token
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def collect(self, max_items: int = 50) -> list[MarketItem]:
        pages: list[dict[str, Any]] = []
        # 1) root pages
        for root in self.config.root_pages:
            page_id = _to_notion_id(root)
            try:
                pages.append(self._get_page(page_id))
                if self.config.include_child_pages:
                    pages.extend(self._list_children_pages(page_id))
            except Exception as exc:  # noqa: BLE001
                print(f"[notion] root {root}: {exc}")

        # 2) search queries
        for query in self.config.search_queries:
            try:
                pages.extend(self._search(query, page_size=min(20, max_items)))
            except Exception as exc:  # noqa: BLE001
                print(f"[notion] search '{query}': {exc}")

        # dedupe by id
        by_id: dict[str, dict[str, Any]] = {}
        for page in pages:
            pid = page.get("id")
            if pid:
                by_id[pid] = page

        items: list[MarketItem] = []
        for page in list(by_id.values())[:max_items]:
            item = self._page_to_item(page)
            if item:
                items.append(item)
        return items

    def fetch_page_text(self, page_id_or_url: str) -> dict[str, Any]:
        page_id = _to_notion_id(page_id_or_url)
        page = self._get_page(page_id)
        blocks = self._list_block_children(page_id)
        text = _blocks_to_text(blocks)
        title = _page_title(page)
        url = page.get("url") or f"https://www.notion.so/{page_id.replace('-', '')}"
        return {
            "ok": True,
            "id": page_id,
            "title": title,
            "url": url,
            "text": text,
            "chars": len(text),
            "excerpt": text[:1500],
        }

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        pages = self._search(query, page_size=limit)
        out = []
        for page in pages[:limit]:
            out.append(
                {
                    "id": page.get("id"),
                    "title": _page_title(page),
                    "url": page.get("url"),
                    "last_edited": page.get("last_edited_time"),
                }
            )
        return out

    def _get_page(self, page_id: str) -> dict[str, Any]:
        with httpx.Client(timeout=60, headers=self._headers) as client:
            r = client.get(f"https://api.notion.com/v1/pages/{page_id}")
            r.raise_for_status()
            return r.json()

    def _search(self, query: str, page_size: int = 20) -> list[dict[str, Any]]:
        payload = {
            "query": query,
            "page_size": page_size,
            "filter": {"value": "page", "property": "object"},
        }
        with httpx.Client(timeout=60, headers=self._headers) as client:
            r = client.post("https://api.notion.com/v1/search", json=payload)
            r.raise_for_status()
            data = r.json()
        return [x for x in data.get("results", []) if x.get("object") == "page"]

    def _list_children_pages(self, block_id: str) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        cursor = None
        with httpx.Client(timeout=60, headers=self._headers) as client:
            while True:
                params: dict[str, Any] = {"page_size": 50}
                if cursor:
                    params["start_cursor"] = cursor
                r = client.get(
                    f"https://api.notion.com/v1/blocks/{block_id}/children",
                    params=params,
                )
                r.raise_for_status()
                data = r.json()
                for block in data.get("results", []):
                    if block.get("type") == "child_page":
                        child_id = block.get("id")
                        if child_id:
                            try:
                                pages.append(self._get_page(child_id))
                            except Exception:  # noqa: BLE001
                                continue
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
                if len(pages) >= self.config.max_pages:
                    break
        return pages

    def _list_block_children(self, block_id: str) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        cursor = None
        with httpx.Client(timeout=60, headers=self._headers) as client:
            while True:
                params: dict[str, Any] = {"page_size": 100}
                if cursor:
                    params["start_cursor"] = cursor
                r = client.get(
                    f"https://api.notion.com/v1/blocks/{block_id}/children",
                    params=params,
                )
                r.raise_for_status()
                data = r.json()
                blocks.extend(data.get("results", []))
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
                if len(blocks) >= 400:
                    break
        return blocks

    def _page_to_item(self, page: dict[str, Any]) -> MarketItem | None:
        title = _page_title(page)
        if not title:
            return None
        page_id = page.get("id") or ""
        url = page.get("url") or f"https://www.notion.so/{page_id.replace('-', '')}"
        # lekki preview bez pełnego fetch blocks (szybki sync)
        summary = f"Notion page: {title}"
        return MarketItem(
            title=title,
            url=url,
            source="notion",
            published_at=page.get("last_edited_time"),
            summary=summary,
            content="",
            tags=["notion", "knowledge"],
            relevance_score=0.55,
            analysis={"notion_id": page_id, "origin": "notion"},
        )


def _to_notion_id(value: str) -> str:
    value = value.strip()
    # URL → id
    m = re.search(
        r"([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})",
        value,
        re.I,
    )
    if not m:
        # bare 32 hex
        m2 = re.search(r"([0-9a-f]{32})", value, re.I)
        if not m2:
            raise ValueError(f"Niepoprawne Notion ID/URL: {value}")
        raw = m2.group(1)
        return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"
    raw = m.group(1).replace("-", "")
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def _page_title(page: dict[str, Any]) -> str:
    props = page.get("properties") or {}
    for prop in props.values():
        if prop.get("type") == "title":
            parts = prop.get("title") or []
            return "".join(p.get("plain_text", "") for p in parts).strip()
    # child_page fallback
    return ""


def _rich_text(parts: list[dict[str, Any]] | None) -> str:
    if not parts:
        return ""
    return "".join(p.get("plain_text", "") for p in parts)


def _blocks_to_text(blocks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for block in blocks:
        btype = block.get("type")
        data = block.get(btype) or {}
        if btype in {
            "paragraph",
            "heading_1",
            "heading_2",
            "heading_3",
            "bulleted_list_item",
            "numbered_list_item",
            "quote",
            "callout",
            "toggle",
        }:
            text = _rich_text(data.get("rich_text"))
            if text:
                lines.append(text)
        elif btype == "code":
            text = _rich_text(data.get("rich_text"))
            if text:
                lines.append(text)
        elif btype == "to_do":
            text = _rich_text(data.get("rich_text"))
            if text:
                lines.append(text)
        elif btype == "child_page":
            title = (data.get("title") or "").strip()
            if title:
                lines.append(f"[child] {title}")
    return "\n".join(lines)
