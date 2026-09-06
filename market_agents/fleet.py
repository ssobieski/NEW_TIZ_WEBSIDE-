from __future__ import annotations

import json
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
)

FEEDBACK_FILES = (
    "crawl_health.json",
    "site_parse_rules.json",
)


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
        return files

    def publish_knowledge(self, notes: str = "") -> dict[str, Any]:
        """Centrala: spakuj aktualną wiedzę parserów dla VPS."""
        if self.fleet.role not in {"central", "both"}:
            return {"ok": False, "error": "publish_knowledge tylko dla role=central|both"}
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
        latest_path = self.sync_root / "latest.json"
        if latest_path.exists():
            try:
                payload = json.loads(latest_path.read_text(encoding="utf-8"))
                pack = Path(str(payload.get("path") or ""))
                if pack.exists():
                    return pack
                pack_id = str(payload.get("pack_id") or "")
                candidate = self.outbox / pack_id
                if candidate.exists():
                    return candidate
            except Exception:  # noqa: BLE001
                pass
        if not self.outbox.exists():
            return None
        packs = sorted(
            [p for p in self.outbox.iterdir() if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return packs[0] if packs else None

    def pull_knowledge(self) -> dict[str, Any]:
        """Worker: pobierz najnowszy knowledge pack z centrali do lokalnego knowledge/."""
        self.ensure_dirs()
        pack = self.latest_knowledge_pack()
        if pack is None:
            return {"ok": False, "error": "brak knowledge packa w fleet/outbox"}
        copied: list[str] = []
        for src in pack.iterdir():
            if src.name == "manifest.json" or not src.is_file():
                continue
            if src.suffix != ".json":
                continue
            shutil.copy2(src, self.knowledge_dir / src.name)
            copied.append(src.name)
        # zapisz marker lokalny
        marker = {
            "pulled_at": time.time(),
            "pack_id": pack.name,
            "files": copied,
            "worker_id": self.worker_id,
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
        # dołącz lokalne items jeśli są
        items = self.data_dir / "items.jsonl"
        if items.exists():
            # tylko ogon — ostatnie 200 linii
            lines = items.read_text(encoding="utf-8").splitlines()
            tail = "\n".join(lines[-200:]) + ("\n" if lines else "")
            (dest / "items_tail.jsonl").write_text(tail, encoding="utf-8")
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
        - merge crawl_health (max delay, sum counts)
        - merge site_parse_rules (wyższa success_count wygrywa)
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
        for pack_meta in pending:
            pack = Path(str(pack_meta.get("path") or ""))
            if not pack.exists():
                continue
            crawl_src = pack / "crawl_health.json"
            if crawl_src.exists():
                merged_hosts += self._merge_crawl_health(crawl_src)
            rules_src = pack / "site_parse_rules.json"
            if rules_src.exists():
                merged_rules += self._merge_parse_rules(rules_src)
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
        }

    def _merge_crawl_health(self, src: Path) -> int:
        dest = self.knowledge_dir / "crawl_health.json"
        incoming = _load_json(src)
        current = _load_json(dest) if dest.exists() else {"hosts": []}
        cur_hosts = {
            h.get("host"): h
            for h in (current.get("hosts") or [])
            if isinstance(h, dict) and h.get("host")
        }
        changed = 0
        for row in incoming.get("hosts") or []:
            if not isinstance(row, dict) or not row.get("host"):
                continue
            host = str(row["host"])
            prev = cur_hosts.get(host)
            if prev is None:
                cur_hosts[host] = row
                changed += 1
                continue
            merged = dict(prev)
            merged["success_count"] = int(prev.get("success_count") or 0) + int(
                row.get("success_count") or 0
            )
            merged["fail_count"] = int(prev.get("fail_count") or 0) + int(
                row.get("fail_count") or 0
            )
            merged["block_count"] = int(prev.get("block_count") or 0) + int(
                row.get("block_count") or 0
            )
            merged["delay_seconds"] = max(
                float(prev.get("delay_seconds") or 0),
                float(row.get("delay_seconds") or 0),
            )
            merged["cooldown_until"] = max(
                float(prev.get("cooldown_until") or 0),
                float(row.get("cooldown_until") or 0),
            )
            if float(row.get("last_request_at") or 0) >= float(prev.get("last_request_at") or 0):
                merged["last_request_at"] = row.get("last_request_at")
                merged["last_status"] = row.get("last_status")
                if row.get("notes"):
                    merged["notes"] = row.get("notes")
            cur_hosts[host] = merged
            changed += 1
        payload = {
            "updated_at": time.time(),
            "hosts": [cur_hosts[k] for k in sorted(cur_hosts.keys())],
            "source": "fleet_absorb",
        }
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return changed

    def _merge_parse_rules(self, src: Path) -> int:
        dest = self.knowledge_dir / "site_parse_rules.json"
        incoming = _load_json(src)
        current = _load_json(dest) if dest.exists() else {"rules": []}
        in_rules = incoming.get("rules", incoming if isinstance(incoming, list) else [])
        cur_rules = current.get("rules", current if isinstance(current, list) else [])
        by_host: dict[str, dict[str, Any]] = {}
        for row in cur_rules:
            if isinstance(row, dict) and row.get("host"):
                by_host[str(row["host"])] = row
        changed = 0
        for row in in_rules:
            if not isinstance(row, dict) or not row.get("host"):
                continue
            host = str(row["host"])
            prev = by_host.get(host)
            if prev is None:
                by_host[host] = row
                changed += 1
                continue
            prev_score = int(prev.get("success_count") or 0) - int(prev.get("fail_count") or 0)
            new_score = int(row.get("success_count") or 0) - int(row.get("fail_count") or 0)
            if new_score > prev_score or (
                new_score == prev_score
                and int(row.get("success_count") or 0) > int(prev.get("success_count") or 0)
            ):
                merged = dict(prev)
                merged.update({k: v for k, v in row.items() if v not in (None, "", [])})
                merged["success_count"] = max(
                    int(prev.get("success_count") or 0), int(row.get("success_count") or 0)
                )
                merged["fail_count"] = max(
                    int(prev.get("fail_count") or 0), int(row.get("fail_count") or 0)
                )
                by_host[host] = merged
                changed += 1
            else:
                # i tak zsumuj statystyki
                prev["success_count"] = int(prev.get("success_count") or 0) + int(
                    row.get("success_count") or 0
                )
                prev["fail_count"] = int(prev.get("fail_count") or 0) + int(
                    row.get("fail_count") or 0
                )
                by_host[host] = prev
                changed += 1
        payload = {
            "count": len(by_host),
            "rules": [by_host[k] for k in sorted(by_host.keys())],
            "source": "fleet_absorb",
            "updated_at": time.time(),
        }
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return changed

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


def fleet_config_from_app(config: Any) -> FleetConfig:
    fleet = getattr(config.agents, "fleet", None)
    if fleet is None:
        return FleetConfig()
    return FleetConfig(
        role=str(getattr(fleet, "role", "central") or "central"),
        worker_id=str(getattr(fleet, "worker_id", "") or "") or None,
        sync_dir=str(getattr(fleet, "sync_dir", "data/fleet")),
        auto_pull_before_run=bool(getattr(fleet, "auto_pull_before_run", True)),
        auto_push_after_run=bool(getattr(fleet, "auto_push_after_run", True)),
        publish_known_firms=bool(getattr(fleet, "publish_known_firms", True)),
        publish_relations=bool(getattr(fleet, "publish_relations", True)),
    )
