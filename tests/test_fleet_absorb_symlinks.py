"""Spec for central fleet absorb: inbox symlinks must not be followed.

These tests fail while the vulnerability is present. A worker-controlled
symlink under the inbox must not be treated as a pack, must not be read
into central knowledge, and must not be used as the path of the ABSORBED
marker. Re-checking the same path and then opening it again still fails:
the directory or file is replaced with a symlink at open time.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from market_agents.fleet import FleetConfig, FleetSync

OUTSIDE_CRAWL_HOST = "OUTSIDE-CRAWL-HOST"
OUTSIDE_RULE_HOST = "OUTSIDE-RULE-HOST"
OUTSIDE_ITEM_URL = "https://outside.example/stolen-item"
OUTSIDE_MANIFEST_NOTE = "OUTSIDE-MANIFEST-NOTE"
LEGIT_HOST = "legit.example"
LEGIT_ITEM_URL = "https://legit.example/item"
BENIGN_HOST = "benign.example"
VICTIM_TEXT = "DO-NOT-OVERWRITE\n"


def _crawl(host: str) -> dict:
    return {
        "hosts": [
            {
                "host": host,
                "success_count": 2,
                "fail_count": 0,
                "block_count": 0,
                "delay_seconds": 1.0,
                "cooldown_until": 0,
                "last_request_at": 1.0,
                "last_status": 200,
            }
        ]
    }


def _rules(host: str) -> dict:
    return {
        "rules": [
            {
                "host": host,
                "css_selector": ".product",
                "success_count": 2,
                "fail_count": 0,
            }
        ]
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _central(tmp: Path) -> tuple[FleetSync, Path, Path]:
    sync = tmp / "fleet"
    data = tmp / "central"
    _write_json(data / "knowledge" / "crawl_health.json", {"hosts": []})
    _write_json(data / "knowledge" / "site_parse_rules.json", {"rules": []})
    central = FleetSync(
        data,
        FleetConfig(role="central", sync_dir=str(sync), worker_id="gpu"),
    )
    central.ensure_dirs()
    return central, sync, data


def _tree_state(root: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames:
            path = Path(dirpath) / name
            if path.is_symlink():
                rows.append((str(path.relative_to(root)), "dirlink:" + os.readlink(path)))
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                rows.append((str(path.relative_to(root)), "link:" + os.readlink(path)))
            elif path.is_file():
                rows.append((str(path.relative_to(root)), path.read_text(encoding="utf-8")))
    return sorted(rows)


def _corpus(data: Path, sync: Path) -> str:
    chunks: list[str] = []
    knowledge = data / "knowledge"
    if knowledge.exists():
        for path in knowledge.rglob("*"):
            if path.is_file() and not path.is_symlink():
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    signals = sync / "inbox_signals.jsonl"
    if signals.exists() and signals.is_file() and not signals.is_symlink():
        chunks.append(signals.read_text(encoding="utf-8", errors="replace"))
    outbox = sync / "outbox"
    if outbox.exists():
        for path in outbox.rglob("*"):
            if path.is_file() and not path.is_symlink():
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def _link_dir(link: Path, target: Path, kind: str) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if kind == "absolute":
        link.symlink_to(target, target_is_directory=True)
        return
    if kind != "relative":
        raise AssertionError(kind)
    link.symlink_to(os.path.relpath(target, start=link.parent), target_is_directory=True)


def _plant_outside(outside: Path) -> None:
    outside.mkdir(parents=True, exist_ok=True)
    _write_json(outside / "crawl_health.json", _crawl(OUTSIDE_CRAWL_HOST))
    _write_json(outside / "site_parse_rules.json", _rules(OUTSIDE_RULE_HOST))
    (outside / "items_tail.jsonl").write_text(
        json.dumps({"url": OUTSIDE_ITEM_URL, "title": "outside"}) + "\n",
        encoding="utf-8",
    )
    _write_json(
        outside / "manifest.json",
        {"notes": OUTSIDE_MANIFEST_NOTE, "pack_id": "evil", "files": []},
    )


def _plant_good_pack(inbox: Path) -> Path:
    pack = inbox / "a-good" / "pack"
    pack.mkdir(parents=True)
    _write_json(pack / "crawl_health.json", _crawl(LEGIT_HOST))
    _write_json(pack / "site_parse_rules.json", _rules(LEGIT_HOST))
    (pack / "items_tail.jsonl").write_text(
        json.dumps({"url": LEGIT_ITEM_URL, "title": "legit"}) + "\n",
        encoding="utf-8",
    )
    _write_json(pack / "manifest.json", {"notes": "legit pack", "pack_id": "good", "files": []})
    return pack


def _lexical(path: object) -> Path:
    raw = os.fsdecode(os.fspath(path))
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return Path(os.path.normpath(candidate))


def _pinned_by_dir_fd(path: object, dir_fd: int | None) -> bool:
    if dir_fd is None:
        return False
    return not Path(os.fsdecode(os.fspath(path))).is_absolute()


def _flags_for_text_mode(mode: str) -> int:
    """Map an io.open mode string onto the flag bits the race looks at.

    Path.read_text and Path.write_text call io.open, which does not go through
    os.open, and they never set O_NOFOLLOW.
    """
    raw = mode or "r"
    plus = "+" in raw
    if "r" in raw and plus:
        flags = os.O_RDWR
    elif any(ch in raw for ch in "wax") and plus:
        flags = os.O_RDWR | os.O_CREAT
    elif "w" in raw:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    elif "a" in raw:
        flags = os.O_WRONLY | os.O_CREAT
    elif "x" in raw:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    else:
        flags = os.O_RDONLY
    if "w" in raw:
        flags |= os.O_TRUNC
    return flags


class _OpenRace:
    """Swap attacker-controlled paths when they are opened by name.

    Both io.open (what Path.read_text and Path.write_text call) and os.open
    are hooked. A relative openat (dir_fd set, path relative) is left alone:
    that fd is the directory the caller already pinned. An absolute path
    ignores dir_fd, so it is still raced.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, on_open) -> None:
        self._real_os_open = os.open
        self._real_io_open = io.open
        self._on_open = on_open
        self.armed = False
        monkeypatch.setattr(os, "open", self._os_open)
        monkeypatch.setattr(io, "open", self._io_open)
        monkeypatch.setattr(builtins, "open", self._io_open)

    def _os_open(self, path, flags, mode=0o777, *, dir_fd=None):
        if self.armed and not isinstance(path, int) and not _pinned_by_dir_fd(path, dir_fd):
            self._on_open(path, flags)
        if dir_fd is None:
            return self._real_os_open(path, flags, mode)
        return self._real_os_open(path, flags, mode, dir_fd=dir_fd)

    def _io_open(
        self,
        file,
        mode="r",
        buffering=-1,
        encoding=None,
        errors=None,
        newline=None,
        closefd=True,
        opener=None,
    ):
        if self.armed and not isinstance(file, int):
            self._on_open(file, _flags_for_text_mode(mode))
        return self._real_io_open(
            file,
            mode,
            buffering,
            encoding,
            errors,
            newline,
            closefd,
            opener,
        )

    def __enter__(self) -> "_OpenRace":
        self.armed = True
        return self

    def __exit__(self, *exc: object) -> None:
        self.armed = False


def _replace_dir_with_symlink(pack: Path, outside: Path, state: dict[str, bool]) -> None:
    if state["swapped"] or pack.is_symlink():
        state["swapped"] = True
        return
    held = pack.with_name(pack.name + ".held")
    os.rename(pack, held)
    os.symlink(outside, pack, target_is_directory=True)
    state["swapped"] = True


def _replace_file_with_symlink(path: Path, target: Path) -> None:
    if path.is_symlink():
        return
    if path.exists():
        path.unlink()
    path.symlink_to(target)


@pytest.mark.parametrize("kind", ["absolute", "relative"])
def test_list_feedback_and_status_skip_symlink_packs(tmp_path: Path, kind: str) -> None:
    central, _sync, _data = _central(tmp_path)
    outside = tmp_path / "outside"
    _plant_outside(outside)
    _plant_good_pack(central.inbox)
    evil = central.inbox / "z-evil" / "pack"
    _link_dir(evil, outside, kind)
    before = _tree_state(outside)

    rows = central.list_feedback()
    status = central.status()

    inbox_root = central.inbox.resolve()
    escaped = [
        row["path"]
        for row in rows
        if not Path(row["path"]).resolve().is_relative_to(inbox_root)
    ]
    assert escaped == []
    assert OUTSIDE_MANIFEST_NOTE not in json.dumps(rows)
    assert status["feedback_pending"] == 1
    assert status["feedback_total"] == 1
    assert _tree_state(outside) == before


@pytest.mark.parametrize("kind", ["absolute", "relative"])
def test_absorb_skips_pack_dir_symlink_and_does_not_republish_it(
    tmp_path: Path, kind: str
) -> None:
    central, sync, data = _central(tmp_path)
    outside = tmp_path / "outside"
    _plant_outside(outside)
    _plant_good_pack(central.inbox)
    _link_dir(central.inbox / "z-evil" / "pack", outside, kind)
    before = _tree_state(outside)

    absorbed = central.absorb_feedback()
    published = central.publish_knowledge(notes="after absorb")
    corpus = _corpus(data, sync)

    assert absorbed["ok"] is True
    assert _tree_state(outside) == before
    assert not (outside / "ABSORBED").exists()
    assert OUTSIDE_CRAWL_HOST not in corpus
    assert OUTSIDE_RULE_HOST not in corpus
    assert OUTSIDE_ITEM_URL not in corpus
    assert OUTSIDE_MANIFEST_NOTE not in corpus
    assert LEGIT_HOST in corpus
    assert LEGIT_ITEM_URL in corpus
    assert published["ok"] is True
    published_crawl = Path(published["path"]) / "crawl_health.json"
    published_text = published_crawl.read_text(encoding="utf-8")
    assert LEGIT_HOST in published_text
    assert OUTSIDE_CRAWL_HOST not in published_text


@pytest.mark.parametrize("kind", ["absolute", "relative"])
def test_absorb_skips_worker_dir_symlink(tmp_path: Path, kind: str) -> None:
    central, sync, data = _central(tmp_path)
    outside = tmp_path / "outside-worker"
    pack = outside / "pack"
    pack.mkdir(parents=True)
    _write_json(pack / "crawl_health.json", _crawl(OUTSIDE_CRAWL_HOST))
    _write_json(
        pack / "manifest.json",
        {"notes": OUTSIDE_MANIFEST_NOTE, "pack_id": "evil", "files": []},
    )
    _link_dir(central.inbox / "z-evil", outside, kind)
    _plant_good_pack(central.inbox)
    before = _tree_state(outside)

    rows = central.list_feedback()
    absorbed = central.absorb_feedback()

    assert all(
        Path(row["path"]).resolve().is_relative_to(central.inbox.resolve()) for row in rows
    )
    assert OUTSIDE_MANIFEST_NOTE not in json.dumps(rows)
    assert absorbed["ok"] is True
    assert _tree_state(outside) == before
    assert not (pack / "ABSORBED").exists()
    assert OUTSIDE_CRAWL_HOST not in _corpus(data, sync)
    assert LEGIT_HOST in _corpus(data, sync)


def test_symlink_crawl_health_is_not_merged(tmp_path: Path) -> None:
    central, sync, data = _central(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "crawl_health.json"
    _write_json(secret, _crawl(OUTSIDE_CRAWL_HOST))
    pack = central.inbox / "vps" / "pack"
    pack.mkdir(parents=True)
    (pack / "crawl_health.json").symlink_to(secret)
    _write_json(pack / "site_parse_rules.json", _rules(LEGIT_HOST))
    before = _tree_state(outside)

    central.absorb_feedback()

    corpus = _corpus(data, sync)
    assert _tree_state(outside) == before
    assert OUTSIDE_CRAWL_HOST not in corpus
    assert LEGIT_HOST in corpus


def test_relative_symlink_parse_rules_are_not_merged(tmp_path: Path) -> None:
    central, sync, data = _central(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "site_parse_rules.json"
    _write_json(secret, _rules(OUTSIDE_RULE_HOST))
    pack = central.inbox / "vps" / "pack"
    pack.mkdir(parents=True)
    link = pack / "site_parse_rules.json"
    link.symlink_to(os.path.relpath(secret, start=pack))
    _write_json(pack / "crawl_health.json", _crawl(LEGIT_HOST))
    before = _tree_state(outside)

    central.absorb_feedback()

    corpus = _corpus(data, sync)
    assert _tree_state(outside) == before
    assert OUTSIDE_RULE_HOST not in corpus
    assert LEGIT_HOST in corpus


def test_symlink_items_tail_is_not_appended(tmp_path: Path) -> None:
    central, sync, data = _central(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "items_tail.jsonl"
    secret.write_text(
        json.dumps({"url": OUTSIDE_ITEM_URL, "title": "outside"}) + "\n",
        encoding="utf-8",
    )
    pack = central.inbox / "vps" / "pack"
    pack.mkdir(parents=True)
    (pack / "items_tail.jsonl").symlink_to(secret)
    _write_json(pack / "crawl_health.json", _crawl(LEGIT_HOST))
    before = _tree_state(outside)

    result = central.absorb_feedback()

    assert _tree_state(outside) == before
    assert OUTSIDE_ITEM_URL not in _corpus(data, sync)
    assert result["signals_appended"] == 0
    assert LEGIT_HOST in _corpus(data, sync)


def test_symlink_manifest_is_not_loaded(tmp_path: Path) -> None:
    central, _sync, _data = _central(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "manifest.json"
    _write_json(secret, {"notes": OUTSIDE_MANIFEST_NOTE, "pack_id": "evil", "files": []})
    pack = central.inbox / "vps" / "pack"
    pack.mkdir(parents=True)
    (pack / "manifest.json").symlink_to(secret)
    _write_json(pack / "crawl_health.json", _crawl(LEGIT_HOST))
    before = _tree_state(outside)

    rows = central.list_feedback()
    central.status()

    assert OUTSIDE_MANIFEST_NOTE not in json.dumps(rows)
    assert _tree_state(outside) == before
    assert any(Path(row["path"]).name == "pack" for row in rows)


def test_dangling_absorbed_symlink_does_not_create_target(tmp_path: Path) -> None:
    central, sync, data = _central(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "created.txt"
    pack = central.inbox / "vps" / "pack"
    pack.mkdir(parents=True)
    _write_json(pack / "crawl_health.json", _crawl(LEGIT_HOST))
    (pack / "ABSORBED").symlink_to(victim)
    before = _tree_state(outside)

    central.absorb_feedback()

    assert _tree_state(outside) == before
    assert not victim.exists()
    assert LEGIT_HOST in _corpus(data, sync)


def test_toctou_pack_dir_swapped_to_symlink_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pack is a real directory at stat time and a symlink at path open."""
    central, sync, data = _central(tmp_path)
    outside = tmp_path / "outside"
    _plant_outside(outside)
    pack = central.inbox / "vps" / "pack"
    pack.mkdir(parents=True)
    _write_json(pack / "crawl_health.json", _crawl(BENIGN_HOST))
    _write_json(pack / "manifest.json", {"notes": "benign", "pack_id": "benign", "files": []})
    pack = pack.resolve()
    outside = outside.resolve()
    state = {"swapped": False}
    before = _tree_state(outside)

    def on_open(path: object, flags: int) -> None:
        current = _lexical(path)
        if current == pack and (flags & os.O_NOFOLLOW):
            return
        if current == pack or str(current).startswith(str(pack) + os.sep):
            _replace_dir_with_symlink(pack, outside, state)

    with _OpenRace(monkeypatch, on_open):
        central.absorb_feedback()

    corpus = _corpus(data, sync)
    assert _tree_state(outside) == before
    assert not (outside / "ABSORBED").exists()
    assert OUTSIDE_CRAWL_HOST not in corpus
    assert OUTSIDE_RULE_HOST not in corpus
    assert OUTSIDE_ITEM_URL not in corpus
    assert OUTSIDE_MANIFEST_NOTE not in corpus


def test_toctou_pack_file_and_absorbed_swapped_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final path component is replaced with a symlink when opened by name."""
    central, sync, data = _central(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "crawl_health.json"
    _write_json(secret, _crawl(OUTSIDE_CRAWL_HOST))
    victim = outside / "victim.txt"
    victim.write_text(VICTIM_TEXT, encoding="utf-8")
    pack = central.inbox / "vps" / "pack"
    pack.mkdir(parents=True)
    crawl = pack / "crawl_health.json"
    _write_json(crawl, _crawl(BENIGN_HOST))
    absorbed = pack / "ABSORBED"
    crawl = crawl.resolve()
    absorbed = absorbed.resolve()
    secret = secret.resolve()
    victim = victim.resolve()
    before = _tree_state(outside)

    def on_open(path: object, flags: int) -> None:
        if flags & os.O_NOFOLLOW:
            return
        current = _lexical(path)
        if current == crawl:
            _replace_file_with_symlink(crawl, secret)
        writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        if current == absorbed and writing:
            _replace_file_with_symlink(absorbed, victim)

    with _OpenRace(monkeypatch, on_open):
        central.absorb_feedback()

    corpus = _corpus(data, sync)
    assert victim.read_text(encoding="utf-8") == VICTIM_TEXT
    assert _tree_state(outside) == before
    assert OUTSIDE_CRAWL_HOST not in corpus


def _push_rsync_flags(script: str) -> list[str]:
    match = re.search(r"(?ms)^[ \t]*push\)\s*(.*?)^[ \t]*;;", script)
    assert match is not None, "push case missing from fleet rsync script"
    command_lines: list[str] = []
    capturing = False
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not capturing:
            if re.search(r"\brsync\b", stripped):
                capturing = True
                command_lines.append(stripped.removesuffix("\\").strip())
                if not stripped.endswith("\\"):
                    break
            continue
        command_lines.append(stripped.removesuffix("\\").strip())
        if not stripped.endswith("\\"):
            break
    tokens = shlex.split(" ".join(command_lines))
    assert tokens and tokens[0] == "rsync"
    flags: list[str] = []
    for token in tokens[1:]:
        if token.startswith("-"):
            flags.append(token)
            continue
        break
    assert flags, "push rsync has no flags"
    return flags


def _symlink_escapes(root: Path) -> list[str]:
    root_resolved = root.resolve()
    escaped: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames) + list(filenames):
            path = Path(dirpath) / name
            if not path.is_symlink():
                continue
            try:
                path.resolve().relative_to(root_resolved)
            except ValueError:
                escaped.append(f"{path} -> {os.readlink(path)}")
    return escaped


def test_fleet_push_rsync_does_not_install_escaping_symlinks(tmp_path: Path) -> None:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "fleet_rsync_vpn.sh"
    flags = _push_rsync_flags(script_path.read_text(encoding="utf-8"))
    rsync_bin = "rsync"
    src = tmp_path / "inbox"
    dst = tmp_path / "central-inbox"
    outside = tmp_path / "outside"
    worker = src / "vps"
    worker.mkdir(parents=True)
    dst.mkdir()
    outside.mkdir()
    (outside / "crawl_health.json").write_text("{}\n", encoding="utf-8")
    (worker / "absolute-pack").symlink_to(outside, target_is_directory=True)
    (worker / "relative-pack").symlink_to("../../outside", target_is_directory=True)

    completed = subprocess.run(
        [rsync_bin, *flags, f"{src}/", f"{dst}/"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert _symlink_escapes(dst) == []
