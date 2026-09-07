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

    def list_known_firms(
        self,
        limit: int = 200,
        *,
        enrich_presentations: bool | None = None,
    ) -> list[dict[str, Any]]:
        """
        Lista znanych firm z bazy Notion „Katalogi konkurencji — indeks”.
        Notion = kanoniczny katalog / przedstawienie firm.
        Zwraca m.in. company, country, site_url, downloads, product_focus,
        presentation (właściwość lub treść strony firmy).
        """
        db_ref = self.config.known_firms_database
        if not db_ref:
            raise RuntimeError(
                "Brak sources.notion.known_firms_database w config — "
                "wskaż ID bazy „Katalogi konkurencji — indeks”"
            )
        database_id = _to_notion_id(db_ref)
        rows: list[dict[str, Any]] = []
        cursor = None
        with httpx.Client(timeout=60, headers=self._headers) as client:
            while len(rows) < limit:
                payload: dict[str, Any] = {"page_size": min(100, limit - len(rows))}
                if cursor:
                    payload["start_cursor"] = cursor
                r = client.post(
                    f"https://api.notion.com/v1/databases/{database_id}/query",
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
                for page in data.get("results", []):
                    firm = _firm_from_page(page)
                    if firm.get("company"):
                        rows.append(firm)
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
        do_enrich = (
            self.config.enrich_firm_presentations
            if enrich_presentations is None
            else enrich_presentations
        )
        if do_enrich:
            max_chars = int(self.config.presentation_max_chars or 4000)
            for firm in rows:
                if firm.get("presentation"):
                    continue
                page_id = str(firm.get("id") or "")
                if not page_id:
                    continue
                try:
                    doc = self.fetch_page_text(page_id)
                    text = str(doc.get("text") or "").strip()
                    if text:
                        firm["presentation"] = text[:max_chars]
                        firm["presentation_source"] = "notion_page_body"
                        firm["presentation_chars"] = len(text)
                except Exception as exc:  # noqa: BLE001
                    firm["presentation_error"] = str(exc)[:200]
        rows.sort(key=lambda x: str(x.get("company") or "").lower())
        return rows[:limit]

    def list_literature(
        self,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """
        Lista pozycji z bazy Notion „Pozycje” (literatura).
        Type Notion: book|paper|video|code → local: book|article|video|(skip code or map article).
        """
        db_ref = self.config.literature_database
        if not db_ref:
            raise RuntimeError(
                "Brak sources.notion.literature_database w config — "
                "wskaż ID bazy „Pozycje” / literatura"
            )
        database_id = _to_notion_id(db_ref)
        rows: list[dict[str, Any]] = []
        cursor = None
        with httpx.Client(timeout=60, headers=self._headers) as client:
            while len(rows) < limit:
                payload: dict[str, Any] = {"page_size": min(100, limit - len(rows))}
                if cursor:
                    payload["start_cursor"] = cursor
                r = client.post(
                    f"https://api.notion.com/v1/databases/{database_id}/query",
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
                for page in data.get("results", []):
                    item = _literature_from_page(page)
                    if item.get("title"):
                        rows.append(item)
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
        rows.sort(key=lambda x: str(x.get("title") or "").lower())
        return rows[:limit]

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

    def create_child_page(
        self,
        *,
        parent_page_id_or_url: str,
        title: str,
        body_lines: list[str] | None = None,
    ) -> dict[str, Any]:
        """Utwórz stronę-dziecko pod parent (CRM push)."""
        parent_id = _to_notion_id(parent_page_id_or_url)
        children: list[dict[str, Any]] = []
        for line in (body_lines or [])[:40]:
            text = (line or "").strip()
            if not text:
                continue
            children.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": text[:1900]}}]
                    },
                }
            )
        payload: dict[str, Any] = {
            "parent": {"page_id": parent_id},
            "properties": {
                "title": {
                    "title": [{"type": "text", "text": {"content": (title or "CRM")[:200]}}]
                }
            },
        }
        if children:
            payload["children"] = children
        with httpx.Client(headers=self._headers, timeout=60.0) as client:
            r = client.post("https://api.notion.com/v1/pages", json=payload)
            r.raise_for_status()
            page = r.json()
        page_id = page.get("id") or ""
        url = page.get("url") or f"https://www.notion.so/{page_id.replace('-', '')}"
        return {"ok": True, "id": page_id, "url": url, "title": title}

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


def _prop_plain(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    ptype = prop.get("type")
    if ptype == "title":
        return "".join(x.get("plain_text", "") for x in (prop.get("title") or [])).strip()
    if ptype == "rich_text":
        return "".join(x.get("plain_text", "") for x in (prop.get("rich_text") or [])).strip()
    if ptype == "url":
        return str(prop.get("url") or "").strip()
    if ptype == "select":
        sel = prop.get("select") or {}
        return str(sel.get("name") or "").strip()
    if ptype == "multi_select":
        return ", ".join(
            str(x.get("name") or "").strip()
            for x in (prop.get("multi_select") or [])
            if x.get("name")
        )
    if ptype == "number":
        val = prop.get("number")
        return "" if val is None else str(val)
    if ptype == "date":
        date = prop.get("date") or {}
        return str(date.get("start") or "").strip()
    return ""


def _firm_from_page(page: dict[str, Any]) -> dict[str, Any]:
    props = page.get("properties") or {}
    company = _prop_plain(props.get("Company") or props.get("Firma") or props.get("Name"))
    if not company:
        company = _page_title(page)
    presentation = _prop_plain(
        props.get("Presentation")
        or props.get("Przedstawienie")
        or props.get("Opis")
        or props.get("Description")
        or props.get("About")
    )
    row: dict[str, Any] = {
        "id": page.get("id"),
        "company": company,
        "country": _prop_plain(props.get("Country") or props.get("Kraj")),
        "site_url": _prop_plain(
            props.get("Site URL") or props.get("Site Url") or props.get("WWW")
        ),
        "downloads": _prop_plain(props.get("Downloads") or props.get("Pobrania")),
        "product_focus": _prop_plain(
            props.get("Product focus")
            or props.get("Fokus produktowy")
            or props.get("Product Focus")
        ),
        "pdf_count": _prop_plain(props.get("PDF count") or props.get("Liczba PDF")),
        "note": _prop_plain(props.get("Note") or props.get("Notatka")),
        "disk_folder": _prop_plain(props.get("Disk folder") or props.get("Folder")),
        "notion_url": page.get("url"),
    }
    if presentation:
        row["presentation"] = presentation
        row["presentation_source"] = "notion_property"
    return row


_NOTION_TYPE_TO_KIND = {
    "book": "book",
    "paper": "article",
    "article": "article",
    "video": "video",
    "proceedings": "proceedings",
    "whitepaper": "whitepaper",
    "white paper": "whitepaper",
    "code": "article",  # kod / repo traktujemy jako powiązany materiał
}


def _literature_from_page(page: dict[str, Any]) -> dict[str, Any]:
    props = page.get("properties") or {}
    title = _page_title(page) or _prop_plain(props.get("Title") or props.get("Name"))
    raw_type = _prop_plain(props.get("Type") or props.get("Rodzaj") or props.get("Kind")).lower()
    kind = _NOTION_TYPE_TO_KIND.get(raw_type, "article" if raw_type else "article")
    topics_raw = _prop_plain(props.get("Topics") or props.get("Tematy") or props.get("Tags"))
    topics = [t.strip() for t in re.split(r"[,;|/]", topics_raw) if t.strip()] if topics_raw else []
    return {
        "id": page.get("id"),
        "title": title,
        "kind": kind,
        "notion_type": raw_type or None,
        "authors": _prop_plain(props.get("Authors") or props.get("Author") or props.get("Autorzy"))
        or None,
        "year": _prop_plain(props.get("Year") or props.get("Rok")) or None,
        "doi": _prop_plain(props.get("DOI") or props.get("Doi")) or None,
        "venue": _prop_plain(props.get("Venue") or props.get("Journal") or props.get("Źródło"))
        or None,
        "url": _prop_plain(props.get("URL") or props.get("Url") or props.get("Link")) or None,
        "notes": _prop_plain(props.get("Notes") or props.get("Notatki") or props.get("Note"))
        or "",
        "language": _prop_plain(props.get("Language") or props.get("Język")) or None,
        "country": _prop_plain(props.get("Country") or props.get("Kraj")) or None,
        "citations": _prop_plain(props.get("Citations") or props.get("Cytowania")) or None,
        "method": _prop_plain(props.get("Method") or props.get("Metoda")) or None,
        "code": _prop_plain(props.get("Code") or props.get("Repo")) or None,
        "topics": topics,
        "notion_url": page.get("url"),
        "source": "notion:pozycje",
    }


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
