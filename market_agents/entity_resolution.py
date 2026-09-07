"""
Golden record / entity resolution dla firm.

Łączy duplikaty profili o tej samej znormalizowanej nazwie (aliasy, literówki)
i przepisuje CRM + relacje na kanoniczną nazwę.
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


def _alias_map_from_merges(merges: list[dict[str, Any]]) -> dict[str, str]:
    """normalized old name → canonical company display name."""
    mapping: dict[str, str] = {}
    for m in merges:
        keep = str(m.get("keep") or "")
        if not keep:
            continue
        mapping[normalize_firm_name(keep)] = keep
        for name in m.get("merge") or []:
            mapping[normalize_firm_name(str(name))] = keep
    return mapping


def rewrite_crm_companies(data_dir: Path | str, alias_map: dict[str, str]) -> int:
    from market_agents.crm_tasks import CrmTaskStore

    if not alias_map:
        return 0
    store = CrmTaskStore.load(data_dir)
    n = 0
    for t in store.tasks:
        key = normalize_firm_name(t.company)
        canon = alias_map.get(key)
        if canon and canon != t.company:
            t.company = canon
            t.updated_at = t.updated_at  # keep; touch via meta
            t.meta = dict(t.meta or {})
            t.meta["canonicalized"] = True
            n += 1
    if n:
        store.save(data_dir)
    return n


def rewrite_relation_endpoints(data_dir: Path | str, alias_map: dict[str, str]) -> int:
    from market_agents.firm_relations import FirmRelationsGraph

    if not alias_map:
        return 0
    # Include seed so endpoints that only exist in seed are canonicalized into cache.
    g = FirmRelationsGraph.load(data_dir, include_seed=True)
    n = 0
    for edge in g.edges:
        for field, norm_field in (("source", "normalized_source"), ("target", "normalized_target")):
            raw = str(edge.get(field) or "")
            canon = alias_map.get(normalize_firm_name(raw))
            if canon and canon != raw:
                edge[field] = canon
                edge[norm_field] = normalize_firm_name(canon)
                n += 1
    if n:
        g.save(data_dir)
    return n


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
    crm_rewritten = 0
    relations_rewritten = 0
    if not dry_run and merges:
        path = str(reg.save(data_dir))
        alias_map = _alias_map_from_merges(merges)
        crm_rewritten = rewrite_crm_companies(data_dir, alias_map)
        relations_rewritten = rewrite_relation_endpoints(data_dir, alias_map)
    return {
        "ok": True,
        "duplicate_groups": len(groups),
        "merges": merges,
        "dry_run": dry_run,
        "path": path,
        "profiles_after": len(reg.profiles),
        "crm_rewritten": crm_rewritten,
        "relations_rewritten": relations_rewritten,
    }


def canonical_name(name: str, registry: FirmProfileRegistry) -> str:
    """Zwróć kanoniczną nazwę jeśli profil istnieje (match / alias)."""
    p = registry.get(name)
    return p.company if p else name.strip()
