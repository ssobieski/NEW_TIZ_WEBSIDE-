"""
Operator approvals for governance require_approval effects.

File: data/governance/approvals.json
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_host(value: str | None) -> str:
    """Z URL lub hostname wyciągnij host bez www."""
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw or raw.startswith("//"):
        host = (urlparse(raw if "://" in raw else f"https:{raw}").hostname or "").lower()
    else:
        # host_or_url może być "example.com/path" albo samym hostem
        host = raw.split("/")[0].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def host_matches(grant_host: str, request_host: str) -> bool:
    g = normalize_host(grant_host)
    r = normalize_host(request_host)
    if not g or not r:
        return False
    return r == g or r.endswith("." + g) or g.endswith("." + r)


@dataclass
class ApprovalGrant:
    id: str
    tool: str
    approved_by: str = "operator"
    note: str = ""
    # optional scope
    company: str | None = None
    host: str | None = None
    expires_at: str | None = None  # ISO; None = until revoked
    revoked: bool = False
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ApprovalGrant:
        host = raw.get("host")
        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex[:10]),
            tool=str(raw.get("tool") or "").strip(),
            approved_by=str(raw.get("approved_by") or "operator"),
            note=str(raw.get("note") or "")[:400],
            company=(str(raw["company"]) if raw.get("company") else None),
            host=(normalize_host(str(host)) if host else None) or None,
            expires_at=(str(raw["expires_at"]) if raw.get("expires_at") else None),
            revoked=bool(raw.get("revoked")),
            created_at=str(raw.get("created_at") or utc_now_iso()),
        )

    def is_active(self, now: datetime | None = None) -> bool:
        if self.revoked or not self.tool:
            return False
        if not self.expires_at:
            return True
        now = now or datetime.now(timezone.utc)
        try:
            exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return now <= exp
        except ValueError:
            return False


class ApprovalStore:
    def __init__(self, grants: list[ApprovalGrant] | None = None) -> None:
        self.grants: list[ApprovalGrant] = list(grants or [])

    @classmethod
    def path_for(cls, audit_dir: Path | str) -> Path:
        return Path(audit_dir) / "approvals.json"

    @classmethod
    def load(cls, audit_dir: Path | str) -> ApprovalStore:
        path = cls.path_for(audit_dir)
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            rows = raw.get("grants") if isinstance(raw, dict) else raw
            return cls([ApprovalGrant.from_dict(r) for r in (rows or []) if isinstance(r, dict)])
        except Exception:  # noqa: BLE001
            return cls()

    def save(self, audit_dir: Path | str) -> Path:
        path = self.path_for(audit_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": utc_now_iso(),
            "grants": [g.to_dict() for g in self.grants],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def approve(
        self,
        tool: str,
        *,
        approved_by: str = "operator",
        note: str = "",
        company: str | None = None,
        host: str | None = None,
        expires_at: str | None = None,
    ) -> ApprovalGrant:
        host_n = normalize_host(host) if host else None
        grant = ApprovalGrant(
            id=uuid.uuid4().hex[:10],
            tool=tool.strip(),
            approved_by=approved_by,
            note=note,
            company=company.strip() if company else None,
            host=host_n or None,
            expires_at=expires_at,
        )
        self.grants.append(grant)
        return grant

    def revoke(self, grant_id: str) -> bool:
        for g in self.grants:
            if g.id == grant_id:
                g.revoked = True
                return True
        return False

    def is_approved(
        self,
        tool: str,
        *,
        company: str | None = None,
        host: str | None = None,
    ) -> ApprovalGrant | None:
        tool = (tool or "").strip()
        req_host = normalize_host(host) if host else ""
        req_company = (company or "").strip().lower()

        # 1) scoped grants (host and/or company) — must match
        for g in reversed(self.grants):
            if not g.is_active() or g.tool != tool:
                continue
            if not g.company and not g.host:
                continue
            if g.company:
                if not req_company or g.company.lower() != req_company:
                    continue
            if g.host:
                if not req_host or not host_matches(g.host, req_host):
                    continue
            return g

        # 2) unscoped grants (tool-wide)
        for g in reversed(self.grants):
            if g.is_active() and g.tool == tool and not g.company and not g.host:
                return g
        return None

    def list_active(self) -> list[ApprovalGrant]:
        return [g for g in self.grants if g.is_active()]
