"""
AI Governance layer — Policy-as-Code engine for local market agents.

Evaluates declarative YAML policies before tool calls / fetches / knowledge
writes. Complements (does not replace) cybersecurity controls in security.py.

Effects: allow | deny | require_approval | redact | monitor
Decision strategy: deny-overrides (any matching deny wins).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import urlparse

import yaml

from market_agents.security import (
    FETCH_TOOLS,
    SAFE_TOOLS,
    SENSITIVE_CLOUD_TOOLS,
    WRITE_TOOLS,
    redact_secrets,
)

Effect = Literal["allow", "deny", "require_approval", "redact", "monitor"]
ActionType = Literal[
    "tool_call",
    "fetch",
    "knowledge_write",
    "fleet_publish",
    "llm_output",
]


class GovernanceError(RuntimeError):
    """Odrzucenie przez warstwę AI governance."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def tool_tier(name: str) -> str:
    if name in SAFE_TOOLS:
        return "safe"
    if name in FETCH_TOOLS:
        return "fetch"
    if name in WRITE_TOOLS:
        return "write"
    if name in SENSITIVE_CLOUD_TOOLS:
        return "cloud"
    return "unknown"


@dataclass
class PolicyRequest:
    action: ActionType
    tool: str | None = None
    role: str | None = None  # central | worker | both
    url: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    content: str | None = None
    actor: str = "agent"
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyDecision:
    effect: Effect
    allowed: bool
    reason: str
    matched_rules: list[str] = field(default_factory=list)
    obligations: list[str] = field(default_factory=list)
    policy_id: str | None = None
    mode: str = "enforce"  # enforce | monitor

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyRule:
    id: str
    effect: Effect = "allow"
    priority: int = 100
    description: str = ""
    reason: str = ""
    match: dict[str, Any] = field(default_factory=dict)
    obligations: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class PolicyPack:
    version: int = 1
    id: str = "default"
    description: str = ""
    defaults: dict[str, Any] = field(default_factory=lambda: {"effect": "allow", "audit": True})
    rules: list[PolicyRule] = field(default_factory=list)
    source_path: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source_path: str | None = None) -> PolicyPack:
        rules: list[PolicyRule] = []
        for raw in data.get("rules") or []:
            if not isinstance(raw, dict):
                continue
            rid = str(raw.get("id") or "").strip()
            if not rid:
                continue
            effect = str(raw.get("effect") or "allow").strip().lower()
            if effect not in {"allow", "deny", "require_approval", "redact", "monitor"}:
                effect = "deny"
            rules.append(
                PolicyRule(
                    id=rid,
                    effect=effect,  # type: ignore[arg-type]
                    priority=int(raw.get("priority") or 100),
                    description=str(raw.get("description") or ""),
                    reason=str(raw.get("reason") or raw.get("description") or rid),
                    match=dict(raw.get("match") or {}),
                    obligations=[str(x) for x in (raw.get("obligations") or [])],
                    enabled=bool(raw.get("enabled", True)),
                )
            )
        return cls(
            version=int(data.get("version") or 1),
            id=str(data.get("id") or "default"),
            description=str(data.get("description") or ""),
            defaults=dict(data.get("defaults") or {"effect": "allow", "audit": True}),
            rules=rules,
            source_path=source_path,
        )


def load_policy_pack(path: Path | str) -> PolicyPack:
    p = Path(path)
    if not p.is_file():
        raise GovernanceError(f"policy pack not found: {p}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise GovernanceError("policy pack must be a mapping")
    pack = PolicyPack.from_dict(data, source_path=str(p))
    validate_policy_pack(pack)
    return pack


def validate_policy_pack(pack: PolicyPack) -> list[str]:
    """Zwróć listę ostrzeżeń; rzuca GovernanceError przy błędach krytycznych."""
    warnings: list[str] = []
    ids: set[str] = set()
    for rule in pack.rules:
        if rule.id in ids:
            raise GovernanceError(f"duplicate rule id: {rule.id}")
        ids.add(rule.id)
        if rule.effect == "deny" and not rule.reason:
            warnings.append(f"{rule.id}: deny without reason")
    return warnings


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _match_value(expected: Any, actual: Any) -> bool:
    if expected is None:
        return True
    if isinstance(expected, list):
        return actual in expected
    return actual == expected


def rule_matches(rule: PolicyRule, req: PolicyRequest) -> bool:
    if not rule.enabled:
        return False
    m = rule.match or {}
    if not m:
        return True

    if "action" in m and not _match_value(m["action"], req.action):
        return False
    if "role" in m and not _match_value(m["role"], req.role):
        return False
    if "actor" in m and not _match_value(m["actor"], req.actor):
        return False
    if "tool" in m and not _match_value(m["tool"], req.tool):
        return False
    if "tool_tier" in m:
        tier = tool_tier(req.tool or "")
        if not _match_value(m["tool_tier"], tier):
            return False
    if "tool_in" in m:
        allowed = {str(x) for x in _as_list(m["tool_in"])}
        if (req.tool or "") not in allowed:
            return False
    # tool_not_in: rule matches only when tool is outside the set
    if "tool_not_in" in m:
        blocked = {str(x) for x in _as_list(m["tool_not_in"])}
        if (req.tool or "") in blocked:
            return False

    if "url_scheme" in m:
        scheme = (urlparse(req.url or "").scheme or "").lower()
        if not _match_value(m["url_scheme"], scheme):
            return False
    if "url_host_suffix" in m:
        host = (urlparse(req.url or "").hostname or "").lower()
        suffixes = [str(s).lower().lstrip(".") for s in _as_list(m["url_host_suffix"])]
        if not any(host == s or host.endswith("." + s) for s in suffixes):
            return False
    if "url_host_not_suffix" in m:
        host = (urlparse(req.url or "").hostname or "").lower()
        suffixes = [str(s).lower().lstrip(".") for s in _as_list(m["url_host_not_suffix"])]
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return False
    if "arg_has" in m:
        keys = {str(k) for k in _as_list(m["arg_has"])}
        if not keys.issubset(set((req.args or {}).keys())):
            return False
    if "arg_equals" in m and isinstance(m["arg_equals"], dict):
        for k, v in m["arg_equals"].items():
            if (req.args or {}).get(k) != v:
                return False
    content = req.content or ""
    if not content and req.args:
        # soft scan string args
        content = " ".join(str(v) for v in req.args.values() if isinstance(v, str))
    if "content_contains_any" in m:
        needles = [str(x).lower() for x in _as_list(m["content_contains_any"])]
        low = content.lower()
        if not any(n in low for n in needles):
            return False
    if "content_regex" in m:
        pat = str(m["content_regex"])
        if not re.search(pat, content, flags=re.I | re.M):
            return False
    if "metadata" in m and isinstance(m["metadata"], dict):
        for k, v in m["metadata"].items():
            if (req.metadata or {}).get(k) != v:
                return False
    return True


def evaluate_pack(
    pack: PolicyPack,
    req: PolicyRequest,
    *,
    mode: str = "enforce",
    deny_require_approval: bool = True,
) -> PolicyDecision:
    """
    Deny-overrides: wśród pasujących reguł deny > require_approval > redact > monitor > allow.
    """
    matched = [r for r in pack.rules if rule_matches(r, req)]
    matched.sort(key=lambda r: (-r.priority, r.id))

    denials = [r for r in matched if r.effect == "deny"]
    approvals = [r for r in matched if r.effect == "require_approval"]
    redacts = [r for r in matched if r.effect == "redact"]
    monitors = [r for r in matched if r.effect == "monitor"]
    allows = [r for r in matched if r.effect == "allow"]

    obligations: list[str] = []
    for r in matched:
        obligations.extend(r.obligations)

    if denials:
        top = denials[0]
        return PolicyDecision(
            effect="deny",
            allowed=False if mode == "enforce" else True,
            reason=top.reason or top.id,
            matched_rules=[r.id for r in matched],
            obligations=obligations or ["audit"],
            policy_id=pack.id,
            mode=mode,
        )
    if approvals:
        top = approvals[0]
        allowed = False if (mode == "enforce" and deny_require_approval) else True
        return PolicyDecision(
            effect="require_approval",
            allowed=allowed,
            reason=top.reason or "requires human approval",
            matched_rules=[r.id for r in matched],
            obligations=list(dict.fromkeys(obligations + ["human_approval"])),
            policy_id=pack.id,
            mode=mode,
        )
    if redacts:
        top = redacts[0]
        return PolicyDecision(
            effect="redact",
            allowed=True,
            reason=top.reason or "redact output",
            matched_rules=[r.id for r in matched],
            obligations=list(dict.fromkeys(obligations + ["redact"])),
            policy_id=pack.id,
            mode=mode,
        )
    if monitors:
        top = monitors[0]
        return PolicyDecision(
            effect="monitor",
            allowed=True,
            reason=top.reason or "monitor only",
            matched_rules=[r.id for r in matched],
            obligations=list(dict.fromkeys(obligations + ["audit"])),
            policy_id=pack.id,
            mode=mode,
        )
    if allows:
        top = allows[0]
        return PolicyDecision(
            effect="allow",
            allowed=True,
            reason=top.reason or "allowed by policy",
            matched_rules=[r.id for r in matched],
            obligations=obligations or ["audit"],
            policy_id=pack.id,
            mode=mode,
        )

    default_effect = str((pack.defaults or {}).get("effect") or "allow").lower()
    if default_effect not in {"allow", "deny"}:
        default_effect = "allow"
    return PolicyDecision(
        effect=default_effect,  # type: ignore[arg-type]
        allowed=default_effect == "allow" or mode != "enforce",
        reason=f"default:{default_effect}",
        matched_rules=[],
        obligations=["audit"] if (pack.defaults or {}).get("audit", True) else [],
        policy_id=pack.id,
        mode=mode,
    )


class GovernanceEngine:
    """Runtime policy-as-code engine with audit trail."""

    def __init__(
        self,
        pack: PolicyPack | None = None,
        *,
        enabled: bool = True,
        mode: str = "enforce",
        audit_dir: Path | str | None = None,
        role: str | None = None,
        fail_closed: bool = False,
        deny_require_approval: bool = True,
    ) -> None:
        self.enabled = enabled
        self.mode = mode if mode in {"enforce", "monitor"} else "enforce"
        self.pack = pack
        self.role = role
        self.fail_closed = fail_closed
        self.deny_require_approval = deny_require_approval
        self.audit_dir = Path(audit_dir) if audit_dir else Path("data/governance")
        self._run_counts: dict[str, int] = {}

    @classmethod
    def from_config(cls, config: Any) -> GovernanceEngine:
        gov = getattr(getattr(config, "agents", None), "governance", None)
        fleet = getattr(getattr(config, "agents", None), "fleet", None)
        role = getattr(fleet, "role", None) if fleet else None
        if gov is None:
            return cls(enabled=False, role=role)
        pack = None
        path = getattr(gov, "policy_pack", None)
        err: Exception | None = None
        if path:
            try:
                pack = load_policy_pack(path)
            except Exception as exc:  # noqa: BLE001
                err = exc
        engine = cls(
            pack=pack,
            enabled=bool(getattr(gov, "enabled", True)),
            mode=str(getattr(gov, "mode", "enforce") or "enforce"),
            audit_dir=getattr(gov, "audit_dir", "data/governance"),
            role=role,
            fail_closed=bool(getattr(gov, "fail_closed", False)),
            deny_require_approval=bool(getattr(gov, "deny_require_approval", True)),
        )
        if err is not None:
            engine._load_error = str(err)  # type: ignore[attr-defined]
            if engine.fail_closed and engine.enabled:
                # synthetic deny-all pack
                engine.pack = PolicyPack(
                    id="fail-closed",
                    description=f"load error: {err}",
                    defaults={"effect": "deny", "audit": True},
                    rules=[],
                )
            else:
                engine.enabled = False
        return engine

    def status(self) -> dict[str, Any]:
        load_error = getattr(self, "_load_error", None)
        return {
            "ok": True,
            "enabled": self.enabled,
            "mode": self.mode,
            "role": self.role,
            "fail_closed": self.fail_closed,
            "policy_id": self.pack.id if self.pack else None,
            "policy_path": self.pack.source_path if self.pack else None,
            "rules": len(self.pack.rules) if self.pack else 0,
            "audit_dir": str(self.audit_dir),
            "load_error": load_error,
            "layer": "ai-governance-policy-as-code",
        }

    def evaluate(self, req: PolicyRequest) -> PolicyDecision:
        if req.role is None and self.role:
            req = PolicyRequest(
                action=req.action,
                tool=req.tool,
                role=self.role,
                url=req.url,
                args=dict(req.args or {}),
                content=req.content,
                actor=req.actor,
                run_id=req.run_id,
                metadata=dict(req.metadata or {}),
            )
        if not self.enabled:
            return PolicyDecision(
                effect="allow",
                allowed=True,
                reason="governance disabled",
                policy_id=None,
                mode=self.mode,
            )
        if self.pack is None:
            if self.fail_closed:
                decision = PolicyDecision(
                    effect="deny",
                    allowed=False,
                    reason="no policy pack (fail-closed)",
                    mode=self.mode,
                )
            else:
                decision = PolicyDecision(
                    effect="allow",
                    allowed=True,
                    reason="no policy pack",
                    mode=self.mode,
                )
            self._audit(req, decision)
            return decision

        # rate-limit obligation helpers via metadata max_per_run on allow rules
        decision = evaluate_pack(
            self.pack,
            req,
            mode=self.mode,
            deny_require_approval=self.deny_require_approval,
        )
        decision = self._apply_rate_limits(req, decision)
        self._audit(req, decision)
        return decision

    def authorize_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        run_id: str | None = None,
    ) -> PolicyDecision:
        args = arguments or {}
        url = str(args.get("url") or "").strip() or None
        return self.evaluate(
            PolicyRequest(
                action="tool_call",
                tool=name,
                url=url,
                args=args,
                run_id=run_id,
            )
        )

    def authorize_fetch(self, url: str, *, run_id: str | None = None) -> PolicyDecision:
        return self.evaluate(PolicyRequest(action="fetch", url=url, run_id=run_id))

    def _apply_rate_limits(
        self, req: PolicyRequest, decision: PolicyDecision
    ) -> PolicyDecision:
        if not decision.allowed or not self.pack:
            return decision
        # Look for matching rules with match.max_per_run
        for rule in self.pack.rules:
            if not rule.enabled or not rule_matches(rule, req):
                continue
            limit = rule.match.get("max_per_run")
            if limit is None:
                continue
            key = f"{req.action}:{req.tool or ''}:{rule.id}"
            count = self._run_counts.get(key, 0) + 1
            self._run_counts[key] = count
            if count > int(limit):
                return PolicyDecision(
                    effect="deny",
                    allowed=False if self.mode == "enforce" else True,
                    reason=f"rate limit exceeded ({rule.id}: {limit}/run)",
                    matched_rules=list(dict.fromkeys(decision.matched_rules + [rule.id])),
                    obligations=decision.obligations,
                    policy_id=self.pack.id,
                    mode=self.mode,
                )
        return decision

    def _audit(self, req: PolicyRequest, decision: PolicyDecision) -> None:
        if "audit" not in (decision.obligations or []) and decision.effect == "allow":
            # still audit denials / approval / redact always
            if decision.effect == "allow" and not (self.pack and (self.pack.defaults or {}).get("audit", True)):
                return
        try:
            self.audit_dir.mkdir(parents=True, exist_ok=True)
            path = self.audit_dir / "audit.jsonl"
            row = {
                "ts": utc_now_iso(),
                "request": {
                    "action": req.action,
                    "tool": req.tool,
                    "role": req.role,
                    "url": req.url,
                    "actor": req.actor,
                    "run_id": req.run_id,
                    "arg_keys": sorted((req.args or {}).keys()),
                },
                "decision": decision.to_dict(),
            }
            line = redact_secrets(json.dumps(row, ensure_ascii=False))
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def recent_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        path = self.audit_dir / "audit.jsonl"
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


def builtin_default_pack() -> PolicyPack:
    """Minimal built-in pack used when file missing and fail_closed=False path tests."""
    return PolicyPack.from_dict(
        {
            "version": 1,
            "id": "builtin-safe",
            "description": "Built-in safe defaults",
            "defaults": {"effect": "allow", "audit": True},
            "rules": [
                {
                    "id": "deny-file-scheme",
                    "priority": 200,
                    "effect": "deny",
                    "reason": "file:// scheme forbidden",
                    "match": {"action": ["tool_call", "fetch"], "url_scheme": "file"},
                },
                {
                    "id": "approval-promote-skill",
                    "priority": 150,
                    "effect": "require_approval",
                    "reason": "promote_host_skill requires human approval",
                    "match": {"action": "tool_call", "tool": "promote_host_skill"},
                },
                {
                    "id": "worker-deny-write",
                    "priority": 140,
                    "effect": "deny",
                    "reason": "worker role cannot use write tools",
                    "match": {"action": "tool_call", "role": "worker", "tool_tier": "write"},
                },
                {
                    "id": "monitor-cloud-tools",
                    "priority": 50,
                    "effect": "monitor",
                    "reason": "cloud tool usage audited",
                    "match": {"action": "tool_call", "tool_tier": "cloud"},
                    "obligations": ["audit"],
                },
            ],
        }
    )
