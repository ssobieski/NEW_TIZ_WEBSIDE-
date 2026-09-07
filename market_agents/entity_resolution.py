"""
Golden record / entity resolution dla firm.

Łączy duplikaty profili o tej samej znormalizowanej nazwie (aliasy, literówki).
"""

from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from market_agents.firm_profiles import FirmProfile, FirmProfileRegistry
from market_agents.firms import normalize_firm_name


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_firm_name(a), normalize_firm_name(b)).ratio()


def find_duplicate_groups(
    profiles: list[FirmProfile],
    *,
    threshold: float = 0.92,
) -> list[list[FirmProfile]]:
    """Grupuj profile prawdopodobnie tej samej firmy."""
    unused = list(profiles)
    groups: list[list[FirmProfile]] = []
    while unused:
        seed = unused.pop(0)
        group = [seed]
        rest: list[FirmProfile] = []
        for other in unused:
            names = [seed.company, *seed.aliases]
            onames = [other.company, *other.aliases]
            hit = False
            for n in names:
                for o in onames:
                    if normalize_firm_name(n) == normalize_firm_name(o) or _similarity(n, o) >= threshold:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                group.append(other)
            else:
                rest.append(other)
        unused = rest
        if len(group) > 1:
            groups.append(group)
    return groups


def merge_profiles(primary: FirmProfile, *others: FirmProfile) -> FirmProfile:
    """Scal others do primary (reużywa FirmProfileRegistry._merge)."""
    reg = FirmProfileRegistry()
    cur = primary
    for o in others:
        cur = FirmProfileRegistry._merge(cur, o)
        # keep stable id of primary
        cur.id = primary.id
        cur.aliases = list(
            dict.fromkeys(
                cur.aliases
                + [o.company]
                + o.aliases
                + ([primary.company] if primary.company != cur.company else [])
            )
        )
        # drop self-alias
        cur.aliases = [a for a in cur.aliases if normalize_firm_name(a) != normalize_firm_name(cur.company)]
    return cur


def resolve_golden_records(
    data_dir: Path | str,
    *,
    threshold: float = 0.92,
    dry_run: bool = False,
) -> dict[str, Any]:
    reg = FirmProfileRegistry.load(data_dir)
    groups = find_duplicate_groups(list(reg.profiles.values()), threshold=threshold)
    merges: list[dict[str, Any]] = []
    for group in groups:
        # prefer profile with most sources / websites
        group_sorted = sorted(
            group,
            key=lambda p: (
                len(p.sources or []),
                len(p.websites or []),
                len(p.emails or []),
                -len(p.id),
            ),
            reverse=True,
        )
        primary = group_sorted[0]
        others = group_sorted[1:]
        merges.append(
            {
                "keep": primary.company,
                "keep_id": primary.id,
                "merge": [o.company for o in others],
                "merge_ids": [o.id for o in others],
            }
        )
        if dry_run:
            continue
        merged = merge_profiles(primary, *others)
        # remove duplicates
        for o in others:
            reg.profiles.pop(o.id, None)
        reg.profiles[merged.id] = merged
        # re-score via upsert
        reg.upsert(merged)

    path = None
    if not dry_run and merges:
        path = str(reg.save(data_dir))
    return {
        "ok": True,
        "duplicate_groups": len(groups),
        "merges": merges,
        "dry_run": dry_run,
        "path": path,
        "profiles_after": len(reg.profiles),
    }


def canonical_name(name: str, registry: FirmProfileRegistry) -> str:
    """Zwróć kanoniczną nazwę jeśli profil istnieje (match / alias)."""
    p = registry.get(name)
    return p.company if p else name.strip()
