"""
Lokalne zadania CRM / follow-up dla hot prospectów.

Bez pełnego UI: JSON queue + opcjonalny sync do Notion (gdy włączony).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from market_agents.firms import normalize_firm_name

TaskStatus = Literal["open", "in_progress", "done", "cancelled"]
TaskPriority = Literal["low", "medium", "high", "hot"]

_PRIORITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "hot": 3}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class CrmTask:
    id: str
    title: str
    company: str
    status: TaskStatus = "open"
    priority: TaskPriority = "medium"
    reason: str = ""
    opportunity_score: float | None = None
    owner: str = ""
    due: str = ""
    source: str = "local"
    notion_url: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CrmTask:
        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex[:12]),
            title=str(raw.get("title") or ""),
            company=str(raw.get("company") or ""),
            status=str(raw.get("status") or "open"),  # type: ignore[arg-type]
            priority=str(raw.get("priority") or "medium"),  # type: ignore[arg-type]
            reason=str(raw.get("reason") or "")[:800],
            opportunity_score=(
                float(raw["opportunity_score"])
                if raw.get("opportunity_score") is not None
                else None
            ),
            owner=str(raw.get("owner") or ""),
            due=str(raw.get("due") or ""),
            source=str(raw.get("source") or "local"),
            notion_url=raw.get("notion_url"),
            meta=dict(raw.get("meta") or {}),
            created_at=str(raw.get("created_at") or utc_now_iso()),
            updated_at=str(raw.get("updated_at") or utc_now_iso()),
        )


class CrmTaskStore:
    def __init__(self, tasks: list[CrmTask] | None = None) -> None:
        self.tasks: list[CrmTask] = list(tasks or [])

    @classmethod
    def load(cls, data_dir: Path | str | None = None) -> CrmTaskStore:
        path = Path(data_dir or "data") / "knowledge" / "crm_tasks.json"
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            rows = raw.get("tasks") if isinstance(raw, dict) else raw
        except Exception:  # noqa: BLE001
            return cls()
        tasks: list[CrmTask] = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            try:
                tasks.append(CrmTask.from_dict(r))
            except Exception:  # noqa: BLE001
                continue
        return cls(tasks)

    def save(self, data_dir: Path | str | None = None) -> Path:
        data_dir = Path(data_dir or "data")
        path = data_dir / "knowledge" / "crm_tasks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": utc_now_iso(),
            "count": len(self.tasks),
            "tasks": [t.to_dict() for t in self.tasks],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def create(
        self,
        *,
        company: str,
        title: str | None = None,
        reason: str = "",
        opportunity_score: float | None = None,
        priority: TaskPriority | None = None,
        owner: str = "",
        meta: dict[str, Any] | None = None,
        dedupe_by_company: bool = False,
        source: str | None = None,
    ) -> tuple[CrmTask, bool]:
        """Zwraca (task, created) — created=False gdy zaktualizowano istniejący open task.

        dedupe_by_company=True: jeden open lead na firmę (łączy przetarg↔prospect).
        """
        company = (company or "").strip()
        if not company:
            raise ValueError("company required")
        if priority is None:
            if opportunity_score is not None and opportunity_score >= 75:
                priority = "hot"
            elif opportunity_score is not None and opportunity_score >= 60:
                priority = "high"
            else:
                priority = "medium"
        meta_in = dict(meta or {})
        company_norm = normalize_firm_name(company)
        # dedupe open tasks for same company+similar title, or whole company
        for t in self.tasks:
            same_company = (
                normalize_firm_name(t.company) == company_norm
                if company_norm
                else t.company.lower() == company.lower()
            )
            title_match = not title or t.title.lower() == (title or "").lower()
            company_lead = dedupe_by_company and same_company and t.status in {
                "open",
                "in_progress",
            }
            if t.status in {"open", "in_progress"} and same_company and (title_match or company_lead):
                t.updated_at = utc_now_iso()
                if opportunity_score is not None:
                    # keep higher opportunity
                    prev = t.opportunity_score
                    if prev is None or opportunity_score >= float(prev):
                        t.opportunity_score = opportunity_score
                if reason:
                    # append distinct reason snippets
                    if reason[:80] not in (t.reason or ""):
                        t.reason = ((t.reason or "") + " | " + reason).strip(" |")[:800]
                if title and company_lead and title not in (t.title or ""):
                    # keep original title; stash alternate in meta
                    t.meta = dict(t.meta or {})
                    alts = list(t.meta.get("alt_titles") or [])
                    if title not in alts:
                        alts.append(title)
                    t.meta["alt_titles"] = alts[:8]
                if meta_in:
                    t.meta = dict(t.meta or {})
                    # preserve existing keys unless new value is non-empty
                    for k, v in meta_in.items():
                        if v is None or v == "" or v == []:
                            continue
                        t.meta[k] = v
                if source:
                    # track multi-source lead
                    t.meta = dict(t.meta or {})
                    sources = list(t.meta.get("sources") or ([t.source] if t.source else []))
                    if source not in sources:
                        sources.append(source)
                    t.meta["sources"] = sources[:8]
                    if not t.source or t.source == "local":
                        t.source = source
                if _PRIORITY_RANK.get(priority, 0) > _PRIORITY_RANK.get(t.priority, 0):
                    t.priority = priority
                return t, False
        task = CrmTask(
            id=uuid.uuid4().hex[:12],
            title=title or f"Follow-up: {company}",
            company=company,
            priority=priority,
            reason=reason,
            opportunity_score=opportunity_score,
            owner=owner,
            source=source or "local",
            meta=meta_in,
        )
        self.tasks.insert(0, task)
        return task, True

    def list(
        self,
        *,
        status: str | None = None,
        company: str | None = None,
        limit: int = 50,
    ) -> list[CrmTask]:
        rows = self.tasks
        if status:
            rows = [t for t in rows if t.status == status]
        if company:
            c = company.lower()
            rows = [t for t in rows if c in t.company.lower()]
        return rows[:limit]

    def set_status(self, task_id: str, status: TaskStatus) -> CrmTask | None:
        for t in self.tasks:
            if t.id == task_id:
                t.status = status
                t.updated_at = utc_now_iso()
                return t
        return None


def create_tasks_from_prospect_scoreboard(
    data_dir: Path | str,
    *,
    min_opportunity: float = 60.0,
    limit: int = 20,
) -> dict[str, Any]:
    """Utwórz CRM tasks z hot prospectów (scoreboard)."""
    from market_agents.prospects import ProspectRegistry

    store = CrmTaskStore.load(data_dir)
    pr = ProspectRegistry.load(data_dir)
    created = 0
    updated = 0
    for row in pr.scoreboard(limit=limit):
        opp = float(row.get("opportunity") or 0)
        if opp < min_opportunity:
            continue
        _task, was_created = store.create(
            company=str(row["company"]),
            title=f"Prospect outreach: {row['company']}",
            reason=f"vertical={row.get('vertical')} quality={row.get('quality_tier')} "
            f"budget_mid={row.get('budget_mid_eur')} families={','.join(row.get('product_families') or [])}",
            opportunity_score=opp,
            source="prospect",
            dedupe_by_company=True,
            meta={
                "processes": row.get("processes") or [],
                "product_families": row.get("product_families") or [],
                "practical_tools": row.get("practical_tools") or [],
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1
    path = store.save(data_dir)
    return {
        "ok": True,
        "created": created,
        "updated": updated,
        "created_or_updated": created + updated,
        "path": str(path),
        "open": len(store.list(status="open")),
    }


def push_crm_tasks_to_notion(
    config: Any,
    *,
    status: str = "open",
    only_hot: bool = True,
    limit: int = 20,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Opcjonalny push otwartych CRM tasks do Notion (dzieci crm_parent_page).
    Wymaga: Notion enabled + token + sources.notion.crm_parent_page.
    """
    from market_agents.collectors.notion import NotionCollector

    notion_cfg = getattr(getattr(config, "sources", None), "notion", None)
    if notion_cfg is None or not getattr(notion_cfg, "enabled", False):
        return {"ok": False, "error": "Notion disabled — włącz sources.notion.enabled"}
    parent = getattr(notion_cfg, "crm_parent_page", None)
    if not parent:
        return {
            "ok": False,
            "error": "Brak sources.notion.crm_parent_page w config",
        }
    token_fn = getattr(config, "notion_token", None)
    token = token_fn() if callable(token_fn) else None
    if not token:
        return {"ok": False, "error": f"Brak tokenu Notion ({notion_cfg.token_env})"}

    data_dir = getattr(config, "data_path", Path("data"))
    store = CrmTaskStore.load(data_dir)
    rows = store.list(status=status, limit=200)
    if only_hot:
        rows = [t for t in rows if t.priority in {"hot", "high"}]
    rows = [t for t in rows if not t.notion_url][:limit]

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "would_push": len(rows),
            "tasks": [{"id": t.id, "company": t.company, "priority": t.priority} for t in rows],
        }

    collector = NotionCollector(notion_cfg, token)
    pushed = 0
    errors: list[str] = []
    for t in rows:
        try:
            body = [
                f"Company: {t.company}",
                f"Priority: {t.priority}",
                f"Opportunity: {t.opportunity_score}",
                f"Reason: {t.reason}",
                f"Local CRM id: {t.id}",
                f"Source: {t.source}",
            ]
            created = collector.create_child_page(
                parent_page_id_or_url=str(parent),
                title=t.title or f"CRM: {t.company}",
                body_lines=body,
            )
            t.notion_url = created.get("url")
            t.meta = dict(t.meta or {})
            t.meta["notion_id"] = created.get("id")
            t.updated_at = utc_now_iso()
            store.save(data_dir)
            pushed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{t.id}:{exc}"[:160])
    path = store.save(data_dir)
    ok = pushed > 0 or not errors
    if not rows and not errors:
        ok = True  # nothing to push
    return {
        "ok": ok,
        "pushed": pushed,
        "errors": errors[:10],
        "path": str(path),
        "open_with_notion": sum(1 for t in store.tasks if t.notion_url),
    }
