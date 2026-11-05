from __future__ import annotations

import json
import math
import shutil
import socket
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

KNOWLEDGE_FILES = (
    "site_parse_rules.json",
    "parsing_skills.json",
    "known_firms.json",
    "firm_relations.json",
    "crawl_health.json",
    "available_pricelists.json",
    "product_tech.json",
    "literature.json",
    "ontology.json",
    "firm_profiles.json",
    "crm_tasks.json",
    "tenders.json",
    "run_status.json",
)

FEEDBACK_FILES = (
    "crawl_health.json",
    "site_parse_rules.json",
)

# Central-only; deliberately absent from KNOWLEDGE_FILES and FLEET_ALLOWED_KNOWLEDGE.
RULE_SUGGESTIONS_FILE = "fleet_rule_suggestions.json"
MAX_RULE_SUGGESTIONS = 500
MAX_FEEDBACK_COUNT_PER_PACK = 10_000
MAX_MERGED_COUNT = 1_000_000


@dataclass
class FleetManifest:
    pack_id: str
    role: str  # central | worker
    worker_id: str | None = None
    created_at: float = field(default_factory=time.time)
    hostname: str = field(default_factory=socket.gethostname)
    files: list[str] = field(default_factory=list)
    notes: str = ""
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FleetManifest":
        return cls(
            pack_id=str(raw.get("pack_id") or ""),
            role=str(raw.get("role") or "central"),
            worker_id=str(raw.get("worker_id") or "") or None,
            created_at=float(raw.get("created_at") or time.time()),
            hostname=str(raw.get("hostname") or ""),
            files=list(raw.get("files") or []),
            notes=str(raw.get("notes") or "")[:500],
            version=int(raw.get("version") or 1),
        )


@dataclass
class FleetConfig:
    """
    Podział ról:
    - central = serwer GPU (agenty uczą skills/rules i publikują knowledge pack)
    - worker  = VPS (tylko parsing/collect, bez LLM; pobiera knowledge, odsyła feedback)
    """

    role: str = "central"  # central | worker
    worker_id: str | None = None
    # Katalog współdzielony (NFS / rsync / skopiowany bucket)
    sync_dir: str = "data/fleet"
    auto_pull_before_run: bool = True
    auto_push_after_run: bool = True
    publish_known_firms: bool = True
    publish_relations: bool = True
    publish_ontology: bool = True
    publish_firm_profiles: bool = True
    publish_crm_tasks: bool = False
    publish_tenders: bool = True
    publish_pricelists: bool = True
    publish_literature: bool = True
    publish_product_tech: bool = True
    publish_run_status: bool = True
    # Upper bound for delay_seconds absorbed from worker crawl_health (agents.crawl.max_delay_seconds)
    max_delay_seconds: float = 45.0


class FleetSync:
    """
    Synchronizacja wiedzy parserów między centralą (GPU) a workerami (VPS).

    Layout:
      data/fleet/
        outbox/knowledge/<pack_id>/   # central → workers
        inbox/<worker_id>/<pack_id>/  # workers → central
        latest.json                   # wskaźnik najnowszego knowledge packa
    """

    def __init__(
        self,
        data_dir: Path | str,
        fleet: FleetConfig | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.knowledge_dir = self.data_dir / "knowledge"
        self.fleet = fleet or FleetConfig()
        # sync_dir: absolute path, or relative to CWD (np. data/fleet współdzielony NFS/rsync)
        self.sync_root = Path(self.fleet.sync_dir)
        self.outbox = self.sync_root / "outbox" / "knowledge"
        self.inbox = self.sync_root / "inbox"
        self.worker_id = self.fleet.worker_id or self._default_worker_id()

    def _default_worker_id(self) -> str:
        host = socket.gethostname().split(".")[0].lower()
        return f"vps-{host}"[:48]

    def ensure_dirs(self) -> None:
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)

    def _knowledge_files(self) -> list[str]:
        files = ["site_parse_rules.json", "parsing_skills.json", "crawl_health.json"]
        if self.fleet.publish_known_firms:
            files.append("known_firms.json")
        if self.fleet.publish_relations:
            files.append("firm_relations.json")
        if getattr(self.fleet, "publish_ontology", True):
            files.append("ontology.json")
        if getattr(self.fleet, "publish_firm_profiles", True):
            files.append("firm_profiles.json")
        if getattr(self.fleet, "publish_crm_tasks", False):
            files.append("crm_tasks.json")
        if getattr(self.fleet, "publish_tenders", True):
            files.append("tenders.json")
        if getattr(self.fleet, "publish_pricelists", True):
            files.append("available_pricelists.json")
        if getattr(self.fleet, "publish_literature", True):
            files.append("literature.json")
        if getattr(self.fleet, "publish_product_tech", True):
            files.append("product_tech.json")
        if getattr(self.fleet, "publish_run_status", True):
            files.append("run_status.json")
        # dedupe preserve order
        return list(dict.fromkeys(files))

    def publish_knowledge(self, notes: str = "") -> dict[str, Any]:
        """Centrala: spakuj aktualną wiedzę parserów dla VPS."""
        if self.fleet.role not in {"central", "both"}:
            return {"ok": False, "error": "publish_knowledge tylko dla role=central|both"}
        try:
            from market_agents.governance import GovernanceEngine, PolicyRequest, load_policy_pack

            pack = None
            pack_path = Path("config/governance/tiz.policy.yaml")
            if pack_path.is_file():
                pack = load_policy_pack(pack_path)
            gov = GovernanceEngine(
                pack=pack,
                enabled=pack is not None,
                mode="enforce",
                audit_dir=self.data_dir / "governance",
                role=self.fleet.role,
            )
            decision = gov.evaluate(
                PolicyRequest(action="fleet_publish", role=self.fleet.role, content=notes)
            )
            if not decision.allowed:
                return {
                    "ok": False,
                    "error": decision.reason,
                    "governance": True,
                    "effect": decision.effect,
                    "matched_rules": decision.matched_rules,
                }
        except Exception:  # noqa: BLE001
            pass
        self.ensure_dirs()
        pack_id = f"knowledge-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        dest = self.outbox / pack_id
        dest.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for name in self._knowledge_files():
            src = self.knowledge_dir / name
            if src.exists():
                shutil.copy2(src, dest / name)
                copied.append(name)
        manifest = FleetManifest(
            pack_id=pack_id,
            role="central",
            created_at=time.time(),
            files=copied,
            notes=notes or "knowledge pack for edge parsers",
        )
        (dest / "manifest.json").write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        latest = {
            "pack_id": pack_id,
            "path": str(dest),
            "created_at": manifest.created_at,
            "files": copied,
        }
        (self.sync_root / "latest.json").write_text(
            json.dumps(latest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {"ok": True, "pack_id": pack_id, "path": str(dest), "files": copied}

    def latest_knowledge_pack(self) -> Path | None:
        from market_agents.security import resolve_fleet_pack_path

        latest_path = self.sync_root / "latest.json"
        if latest_path.exists():
            try:
                payload = json.loads(latest_path.read_text(encoding="utf-8"))
                pack_id = str(payload.get("pack_id") or "")
                safe = resolve_fleet_pack_path(
                    payload.get("path"),
                    sync_root=self.sync_root,
                    pack_id=pack_id or None,
                )
                if safe is not None:
                    return safe
            except Exception:  # noqa: BLE001
                pass
        outbox = self.outbox
        if not outbox.exists():
            return None
        packs = sorted(
            [p for p in outbox.iterdir() if p.is_dir() and not p.is_symlink()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return packs[0] if packs else None

    def pull_knowledge(self) -> dict[str, Any]:
        """Worker: pobierz najnowszy knowledge pack z centrali do lokalnego knowledge/."""
        from market_agents.security import FLEET_ALLOWED_KNOWLEDGE, SecurityError, iter_safe_pack_files

        self.ensure_dirs()
        pack = self.latest_knowledge_pack()
        if pack is None:
            return {"ok": False, "error": "brak knowledge packa w fleet/outbox"}
        copied: list[str] = []
        try:
            files = iter_safe_pack_files(pack, allowlist=FLEET_ALLOWED_KNOWLEDGE)
        except SecurityError as exc:
            return {"ok": False, "error": str(exc)}
        for src in files:
            shutil.copy2(src, self.knowledge_dir / src.name)
            copied.append(src.name)
        # zapisz marker lokalny
        marker = {
            "pulled_at": time.time(),
            "pack_id": pack.name,
            "files": copied,
            "worker_id": self.worker_id,
            "security": "allowlist+no-symlink",
        }
        (self.knowledge_dir / "fleet_pulled.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {"ok": True, "pack_id": pack.name, "files": copied, "worker_id": self.worker_id}

    def push_feedback(self, notes: str = "") -> dict[str, Any]:
        """Worker: wyślij crawl_health + lokalne reguły z powrotem do centrali."""
        self.ensure_dirs()
        pack_id = f"feedback-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        dest = self.inbox / self.worker_id / pack_id
        dest.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for name in FEEDBACK_FILES:
            src = self.knowledge_dir / name
            if src.exists():
                shutil.copy2(src, dest / name)
                copied.append(name)
        # dołącz lokalne items jeśli są — tylko metadane (bez pełnej treści = mniej wycieku)
        from market_agents.security import redact_secrets

        items = self.data_dir / "items.jsonl"
        if items.exists():
            lines = items.read_text(encoding="utf-8").splitlines()
            slim: list[str] = []
            for line in lines[-200:]:
                try:
                    row = json.loads(line)
                    slim.append(
                        json.dumps(
                            {
                                "title": row.get("title"),
                                "url": row.get("url"),
                                "source": row.get("source"),
                                "tags": row.get("tags"),
                                "relevance_score": row.get("relevance_score"),
                            },
                            ensure_ascii=False,
                        )
                    )
                except Exception:  # noqa: BLE001
                    slim.append(redact_secrets(line[:300]))
            (dest / "items_tail.jsonl").write_text(
                "\n".join(slim) + ("\n" if slim else ""), encoding="utf-8"
            )
            copied.append("items_tail.jsonl")
        manifest = FleetManifest(
            pack_id=pack_id,
            role="worker",
            worker_id=self.worker_id,
            files=copied,
            notes=notes or "worker feedback for central agents",
        )
        (dest / "manifest.json").write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "ok": True,
            "pack_id": pack_id,
            "path": str(dest),
            "files": copied,
            "worker_id": self.worker_id,
        }

    def list_feedback(self) -> list[dict[str, Any]]:
        self.ensure_dirs()
        rows: list[dict[str, Any]] = []
        if not self.inbox.exists():
            return rows
        for worker_dir in sorted(self.inbox.iterdir()):
            if not worker_dir.is_dir():
                continue
            for pack in sorted(worker_dir.iterdir()):
                if not pack.is_dir():
                    continue
                manifest_path = pack / "manifest.json"
                meta: dict[str, Any] = {"worker_id": worker_dir.name, "path": str(pack)}
                if manifest_path.exists():
                    try:
                        meta.update(json.loads(manifest_path.read_text(encoding="utf-8")))
                    except Exception:  # noqa: BLE001
                        pass
                rows.append(meta)
        return rows

    def absorb_feedback(self, limit: int = 50) -> dict[str, Any]:
        """
        Centrala: wchłłoń feedback z VPS.
        - merge crawl_health (max delay ograniczony polityką, capped counts, bez cooldown z VPS)
        - site_parse_rules: istniejące reguły nie są nadpisywane (tylko capped counts);
          reguły dla nowych hostów trafiają do fleet_rule_suggestions.json do przeglądu
        """
        if self.fleet.role not in {"central", "both"}:
            return {"ok": False, "error": "absorb_feedback tylko dla role=central|both"}
        self.ensure_dirs()
        pending = [
            p
            for p in self.list_feedback()
            if not (Path(str(p.get("path") or "")) / "ABSORBED").exists()
        ][:limit]
        absorbed = 0
        merged_hosts = 0
        merged_rules = 0
        suggested_rules = 0
        signals_appended = 0
        for pack_meta in pending:
            pack = Path(str(pack_meta.get("path") or ""))
            if not pack.exists():
                continue
            crawl_src = pack / "crawl_health.json"
            if crawl_src.exists():
                merged_hosts += self._merge_crawl_health(crawl_src)
            rules_src = pack / "site_parse_rules.json"
            if rules_src.exists():
                merged, suggested = self._merge_parse_rules(
                    rules_src,
                    worker_id=str(pack_meta.get("worker_id") or pack.parent.name),
                    pack_id=pack.name,
                )
                merged_rules += merged
                suggested_rules += suggested
            tail = pack / "items_tail.jsonl"
            if tail.exists():
                signals_appended += self._absorb_items_tail(
                    tail, worker_id=str(pack_meta.get("worker_id") or pack.parent.name)
                )
            (pack / "ABSORBED").write_text(
                json.dumps({"absorbed_at": time.time()}, indent=2),
                encoding="utf-8",
            )
            absorbed += 1
        return {
            "ok": True,
            "absorbed_packs": absorbed,
            "merged_hosts": merged_hosts,
            "merged_rules": merged_rules,
            "suggested_rules": suggested_rules,
            "signals_appended": signals_appended,
        }

    def _absorb_items_tail(self, src: Path, *, worker_id: str) -> int:
        """Append worker signal metadata into data/fleet/inbox_signals.jsonl (dedupe by URL)."""
        dest = self.sync_root / "inbox_signals.jsonl"
        dest.parent.mkdir(parents=True, exist_ok=True)
        seen: set[str] = set()
        if dest.exists():
            for line in dest.read_text(encoding="utf-8").splitlines()[-5000:]:
                try:
                    row = json.loads(line)
                    url = str(row.get("url") or "")
                    if url:
                        seen.add(url)
                except Exception:  # noqa: BLE001
                    continue
        appended = 0
        with dest.open("a", encoding="utf-8") as fh:
            for line in src.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(row, dict):
                    continue
                url = str(row.get("url") or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                row["worker_id"] = worker_id
                row["absorbed_at"] = time.time()
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                appended += 1
        return appended

    def _merge_crawl_health(self, src: Path) -> int:
        """
        Worker crawl_health is untrusted: counters are capped, delay is clamped to
        the crawl policy maximum, and cooldown_until is never taken from a worker
        (a cooldown is local to the worker's IP and would block every host fleet-wide).
        """
        dest = self.knowledge_dir / "crawl_health.json"
        incoming = _load_json(src)
        current = _load_json(dest) if dest.exists() else {"hosts": []}
        cur_hosts: dict[str, dict[str, Any]] = {}
        for h in current.get("hosts") or []:
            if isinstance(h, dict):
                host = _normalize_host(h.get("host"))
                if host:
                    cur_hosts[host] = h
        max_delay = max(0.0, _safe_float(self.fleet.max_delay_seconds, 45.0))
        now = time.time()
        changed = 0
        for row in incoming.get("hosts") or []:
            if not isinstance(row, dict):
                continue
            host = _normalize_host(row.get("host"))
            if not host:
                continue
            prev = cur_hosts.get(host)
            base: dict[str, Any] = dict(prev) if prev is not None else {
                "host": host,
                "success_count": 0,
                "fail_count": 0,
                "block_count": 0,
                "delay_seconds": 0.0,
                "cooldown_until": 0.0,
                "last_request_at": 0.0,
                "last_status": None,
            }
            merged = dict(base)
            for key in ("success_count", "fail_count", "block_count"):
                merged[key] = _merge_counter(base.get(key), row.get(key))
            worker_delay = min(max(0.0, _safe_float(row.get("delay_seconds"))), max_delay)
            merged["delay_seconds"] = min(
                max(_safe_float(base.get("delay_seconds")), worker_delay), max_delay
            )
            row_last = _safe_float(row.get("last_request_at"))
            if _safe_float(base.get("last_request_at")) <= row_last <= now:
                merged["last_request_at"] = row_last
                merged["last_status"] = _safe_status(row.get("last_status"))
                if row.get("notes"):
                    merged["notes"] = str(row.get("notes"))[:300]
            cur_hosts[host] = merged
            changed += 1
        payload = {
            "updated_at": time.time(),
            "hosts": [cur_hosts[k] for k in sorted(cur_hosts.keys())],
            "source": "fleet_absorb",
        }
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return changed

    def _merge_parse_rules(self, src: Path, *, worker_id: str, pack_id: str) -> tuple[int, int]:
        """
        Worker site_parse_rules is untrusted: live rules keep their selector,
        method, origin and all other parse settings; only capped counters are
        merged. Rules for hosts without a central rule are stored as suggestions
        for operator review (fleet_rule_suggestions.json, never published).
        Returns (merged_rules, suggested_rules).
        """
        from market_agents.parse_rules import SiteParseRule

        dest = self.knowledge_dir / "site_parse_rules.json"
        incoming = _load_json(src)
        current = _load_json(dest) if dest.exists() else {"rules": []}
        in_rules = incoming.get("rules", incoming if isinstance(incoming, list) else [])
        cur_rules = current.get("rules", current if isinstance(current, list) else [])
        if isinstance(in_rules, dict):
            in_rules = [{**r, "host": r.get("host") or h} for h, r in in_rules.items() if isinstance(r, dict)]
        if isinstance(cur_rules, dict):
            cur_rules = [{**r, "host": r.get("host") or h} for h, r in cur_rules.items() if isinstance(r, dict)]
        if not isinstance(in_rules, list):
            in_rules = []
        if not isinstance(cur_rules, list):
            cur_rules = []
        by_host: dict[str, dict[str, Any]] = {}
        for row in cur_rules:
            if isinstance(row, dict):
                host = _normalize_host(row.get("host"))
                if host:
                    by_host[host] = row
        merged_count = 0
        suggestions: list[dict[str, Any]] = []
        for row in in_rules:
            if not isinstance(row, dict):
                continue
            host = _normalize_host(row.get("host"))
            if not host:
                continue
            prev = by_host.get(host)
            if prev is None:
                try:
                    rule = SiteParseRule.from_dict({**row, "host": host}).to_dict()
                except Exception:  # noqa: BLE001
                    continue
                rule["success_count"] = _merge_counter(0, rule.get("success_count"))
                rule["fail_count"] = _merge_counter(0, rule.get("fail_count"))
                rule["origin"] = "worker"
                suggestions.append(rule)
                continue
            prev["success_count"] = _merge_counter(prev.get("success_count"), row.get("success_count"))
            prev["fail_count"] = _merge_counter(prev.get("fail_count"), row.get("fail_count"))
            merged_count += 1
        payload = {
            "count": len(by_host),
            "rules": [by_host[k] for k in sorted(by_host.keys())],
            "source": "fleet_absorb",
            "updated_at": time.time(),
        }
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if suggestions:
            self._record_rule_suggestions(suggestions, worker_id=worker_id, pack_id=pack_id)
        return merged_count, len(suggestions)

    def _record_rule_suggestions(
        self, rules: list[dict[str, Any]], *, worker_id: str, pack_id: str
    ) -> None:
        dest = self.knowledge_dir / RULE_SUGGESTIONS_FILE
        current = _load_json(dest) if dest.exists() else {}
        rows = current.get("suggestions") or []
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get("rule"), dict):
                key = (str(row.get("worker_id") or ""), str(row["rule"].get("host") or ""))
                by_key[key] = row
        now = time.time()
        for rule in rules:
            by_key[(worker_id, str(rule["host"]))] = {
                "worker_id": worker_id,
                "pack_id": pack_id,
                "received_at": now,
                "rule": rule,
            }
        kept = sorted(by_key.values(), key=lambda r: _safe_float(r.get("received_at")))
        kept = kept[-MAX_RULE_SUGGESTIONS:]
        payload = {
            "updated_at": now,
            "note": "worker parse-rule suggestions; review before adding to site_parse_rules.json",
            "suggestions": kept,
        }
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def status(self) -> dict[str, Any]:
        self.ensure_dirs()
        latest = self.latest_knowledge_pack()
        feedback = self.list_feedback()
        pending = [f for f in feedback if not (Path(f["path"]) / "ABSORBED").exists()]
        return {
            "ok": True,
            "role": self.fleet.role,
            "worker_id": self.worker_id,
            "sync_dir": str(self.sync_root),
            "latest_knowledge": latest.name if latest else None,
            "feedback_total": len(feedback),
            "feedback_pending": len(pending),
            "auto_pull_before_run": self.fleet.auto_pull_before_run,
            "auto_push_after_run": self.fleet.auto_push_after_run,
        }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return raw if isinstance(raw, dict) else {"rules": raw}


def _normalize_host(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    host = value.strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _safe_count(value: Any) -> int:
    try:
        out = int(_safe_float(value))
    except (OverflowError, ValueError):
        return 0
    return max(0, out)


def _safe_status(value: Any) -> int | None:
    if value is None:
        return None
    status = _safe_count(value)
    return status if 100 <= status <= 599 else None


def _merge_counter(current: Any, incoming: Any) -> int:
    added = min(_safe_count(incoming), MAX_FEEDBACK_COUNT_PER_PACK)
    return min(_safe_count(current) + added, MAX_MERGED_COUNT)


def fleet_config_from_app(config: Any) -> FleetConfig:
    fleet = getattr(config.agents, "fleet", None)
    if fleet is None:
        return FleetConfig()
    crawl = getattr(config.agents, "crawl", None)
    return FleetConfig(
        max_delay_seconds=float(getattr(crawl, "max_delay_seconds", 45.0) or 45.0),
        role=str(getattr(fleet, "role", "central") or "central"),
        worker_id=str(getattr(fleet, "worker_id", "") or "") or None,
        sync_dir=str(getattr(fleet, "sync_dir", "data/fleet")),
        auto_pull_before_run=bool(getattr(fleet, "auto_pull_before_run", True)),
        auto_push_after_run=bool(getattr(fleet, "auto_push_after_run", True)),
        publish_known_firms=bool(getattr(fleet, "publish_known_firms", True)),
        publish_relations=bool(getattr(fleet, "publish_relations", True)),
        publish_ontology=bool(getattr(fleet, "publish_ontology", True)),
        publish_firm_profiles=bool(getattr(fleet, "publish_firm_profiles", True)),
        publish_crm_tasks=bool(getattr(fleet, "publish_crm_tasks", False)),
        publish_tenders=bool(getattr(fleet, "publish_tenders", True)),
        publish_pricelists=bool(getattr(fleet, "publish_pricelists", True)),
        publish_literature=bool(getattr(fleet, "publish_literature", True)),
        publish_product_tech=bool(getattr(fleet, "publish_product_tech", True)),
        publish_run_status=bool(getattr(fleet, "publish_run_status", True)),
    )
