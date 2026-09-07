from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from market_agents.firms import KnownFirmsIndex, normalize_firm_name

# Typy relacji w siatce firm TIZ
RELATION_TYPES = (
    "distributor_of",  # A dystrybuuje produkty B
    "dealer_of",  # A jest dealerem / autoryzowanym sprzedawcą B
    "brand_of",  # A jest marką własną / private label B
    "subsidiary_of",  # A jest spółką zależną B
    "oem_group",  # A i B w tej samej grupie kapitałowej / IMC
    "partner_of",  # partnerstwo / joint venture / alliance
    "rebrand_of",  # A to rebrand / dawna nazwa B
    "customer_of",  # A jest klientem B (A kupuje od B)
    "supplier_of",  # A jest dostawcą B (A sprzedaje do B)
    "competes_with",  # A konkuruje z B
)

_VALID = set(RELATION_TYPES)


def _edge_key(
    source: str,
    target: str,
    relation_type: str,
) -> str:
    return f"{normalize_firm_name(source)}|{relation_type}|{normalize_firm_name(target)}"


class FirmRelationsGraph:
    """
    Siatka powiązań firm:
    np. Hoffmann Group --brand_of-- GARANT
        Perschmann --distributor_of-- Sandvik Coromant
    """

    def __init__(self, edges: list[dict[str, Any]] | None = None) -> None:
        self.edges: list[dict[str, Any]] = []
        self._keys: set[str] = set()
        for edge in edges or []:
            self._ingest(edge)

    def _ingest(self, edge: dict[str, Any]) -> bool:
        source = str(edge.get("source") or edge.get("from_company") or "").strip()
        target = str(edge.get("target") or edge.get("to_company") or "").strip()
        relation_type = str(edge.get("relation_type") or edge.get("type") or "").strip()
        if not source or not target or relation_type not in _VALID:
            return False
        key = _edge_key(source, target, relation_type)
        if key in self._keys:
            return False
        row = {
            "source": source,
            "target": target,
            "relation_type": relation_type,
            "evidence": str(edge.get("evidence") or "")[:600],
            "url": str(edge.get("url") or ""),
            "confidence": float(edge.get("confidence") or 0.7),
            "origin": str(edge.get("origin") or "manual"),
            "normalized_source": normalize_firm_name(source),
            "normalized_target": normalize_firm_name(target),
        }
        self.edges.append(row)
        self._keys.add(key)
        return True

    def add_relation(
        self,
        source: str,
        target: str,
        relation_type: str,
        *,
        evidence: str = "",
        url: str = "",
        confidence: float = 0.7,
        origin: str = "manual",
        **_extra: Any,
    ) -> dict[str, Any]:
        if relation_type not in _VALID:
            raise ValueError(
                f"Nieznany relation_type={relation_type}. Dozwolone: {', '.join(RELATION_TYPES)}"
            )
        source = source.strip()
        target = target.strip()
        if not source or not target:
            raise ValueError("source i target są wymagane")
        if normalize_firm_name(source) == normalize_firm_name(target):
            raise ValueError("source i target nie mogą być tą samą firmą")
        edge = {
            "source": source,
            "target": target,
            "relation_type": relation_type,
            "evidence": evidence,
            "url": url,
            "confidence": confidence,
            "origin": origin,
        }
        added = self._ingest(edge)
        return {"ok": True, "added": added, "edge": edge}

    @classmethod
    def load(
        cls,
        data_dir: Path | str | None = None,
        seed_path: Path | str = "config/firm_relations.seed.json",
        *,
        include_seed: bool = True,
        alias_map: dict[str, str] | None = None,
    ) -> FirmRelationsGraph:
        data_dir = Path(data_dir or "data")
        cache = data_dir / "knowledge" / "firm_relations.json"
        seed = Path(seed_path)
        edges: list[dict[str, Any]] = []
        if cache.exists():
            try:
                payload = json.loads(cache.read_text(encoding="utf-8"))
                edges = payload.get("edges", payload if isinstance(payload, list) else [])
            except Exception:  # noqa: BLE001
                edges = []
        graph = cls(edges)
        # seed zawsze dołączany (aktualizacje seed nie giną pod starym cache)
        if include_seed and seed.exists():
            try:
                payload = json.loads(seed.read_text(encoding="utf-8"))
                seed_edges = payload.get("edges", payload if isinstance(payload, list) else [])
                if isinstance(seed_edges, list):
                    aliases = alias_map
                    if aliases is None:
                        try:
                            from market_agents.entity_resolution import load_alias_map

                            aliases = load_alias_map(data_dir)
                        except Exception:  # noqa: BLE001
                            aliases = {}
                    if aliases:
                        from market_agents.entity_resolution import canonicalize_edge_endpoints

                        seed_edges = [canonicalize_edge_endpoints(e, aliases) for e in seed_edges]
                    graph.merge(seed_edges)
            except Exception:  # noqa: BLE001
                pass
        return graph

    def save(self, data_dir: Path | str | None = None) -> Path:
        data_dir = Path(data_dir or "data")
        out = data_dir / "knowledge" / "firm_relations.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "relation_types": list(RELATION_TYPES),
            "count": len(self.edges),
            "edges": self.edges,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    def list_relations(
        self,
        *,
        company: str | None = None,
        relation_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = list(self.edges)
        if company:
            n = normalize_firm_name(company)
            rows = [
                e
                for e in rows
                if n == e["normalized_source"]
                or n == e["normalized_target"]
                or n in e["normalized_source"]
                or n in e["normalized_target"]
            ]
        if relation_type:
            rows = [e for e in rows if e["relation_type"] == relation_type]
        rows.sort(key=lambda e: (-float(e.get("confidence") or 0), e["source"].lower()))
        return rows[:limit]

    def neighborhood(self, company: str, depth: int = 1) -> dict[str, Any]:
        """Graf sąsiedztwa wokół firmy (1–2 hop)."""
        depth = max(1, min(int(depth), 2))
        root = normalize_firm_name(company)
        nodes: set[str] = {company.strip() or company}
        edges_out: list[dict[str, Any]] = []
        frontier = {root}
        name_by_norm = {root: company.strip() or company}

        for _ in range(depth):
            nxt: set[str] = set()
            for edge in self.edges:
                s, t = edge["normalized_source"], edge["normalized_target"]
                if s in frontier or t in frontier:
                    edges_out.append(edge)
                    nodes.add(edge["source"])
                    nodes.add(edge["target"])
                    name_by_norm[s] = edge["source"]
                    name_by_norm[t] = edge["target"]
                    nxt.add(s)
                    nxt.add(t)
            frontier = nxt - frontier
        # dedupe edges
        seen: set[str] = set()
        uniq: list[dict[str, Any]] = []
        for e in edges_out:
            k = _edge_key(e["source"], e["target"], e["relation_type"])
            if k in seen:
                continue
            seen.add(k)
            uniq.append(e)
        return {
            "company": company,
            "nodes": sorted(nodes, key=str.lower),
            "edges": uniq,
            "count_nodes": len(nodes),
            "count_edges": len(uniq),
        }

    def merge(self, other: "FirmRelationsGraph" | Iterable[dict[str, Any]]) -> int:
        added = 0
        edges = other.edges if isinstance(other, FirmRelationsGraph) else list(other)
        for edge in edges:
            before = len(self.edges)
            self._ingest(edge)
            if len(self.edges) > before:
                added += 1
        return added


# Nazwa firmy: (?-i:...) żeby IGNORECASE nie rozmiękczał [A-Z].
# Bez gołej kropki w tokenie — inaczej „Group. GARANT” skleja się w jedną nazwę.
_FIRM = (
    r"(?-i:[A-Z][A-Za-z0-9&+/-]*(?:\.[A-Za-z0-9&+/-]+)*"
    r"(?:\s+[A-Z][A-Za-z0-9&+/-]*(?:\.[A-Za-z0-9&+/-]+)*){0,3})"
)
_FIRM_SHORT = (
    r"(?-i:[A-Z][A-Za-z0-9&+/-]*(?:\.[A-Za-z0-9&+/-]+)*"
    r"(?:\s+[A-Z][A-Za-z0-9&+/-]*(?:\.[A-Za-z0-9&+/-]+)*){0,2})"
)

_RELATION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "distributor_of",
        re.compile(
            rf"(?P<a>{_FIRM})\s+"
            r"(?:is\s+)?(?:an?\s+)?(?:authorized\s+)?(?:distributor|stockist|reseller)\s+"
            r"(?:of|for)\s+"
            rf"(?P<b>{_FIRM})",
            re.IGNORECASE,
        ),
    ),
    (
        "distributor_of",
        re.compile(
            rf"(?P<a>{_FIRM})\s+"
            r"(?:dystrybuuje|dystrybutor)\s+"
            rf"(?P<b>{_FIRM})",
            re.IGNORECASE,
        ),
    ),
    (
        "dealer_of",
        re.compile(
            rf"(?P<a>{_FIRM})\s+"
            r"(?:is\s+)?(?:an?\s+)?(?:authorized\s+)?dealer\s+(?:of|for)\s+"
            rf"(?P<b>{_FIRM})",
            re.IGNORECASE,
        ),
    ),
    (
        "brand_of",
        re.compile(
            rf"(?P<a>{_FIRM_SHORT})\s+"
            r"(?:is\s+)?(?:a\s+)?(?:brand|private\s+label|own\s+brand)\s+of\s+"
            rf"(?P<b>{_FIRM})",
            re.IGNORECASE,
        ),
    ),
    (
        "brand_of",
        re.compile(
            rf"(?P<b>{_FIRM})\s+"
            r"(?:brands?|marki)\s*[:=-]?\s*"
            rf"(?P<a>{_FIRM_SHORT}(?:\s*/\s*{_FIRM_SHORT}){{0,3}})",
            re.IGNORECASE,
        ),
    ),
    (
        "subsidiary_of",
        re.compile(
            rf"(?P<a>{_FIRM})\s+"
            r"(?:is\s+)?(?:a\s+)?(?:subsidiary|daughter\s+company|spółka\s+zależna)\s+(?:of\s+)?"
            rf"(?P<b>{_FIRM})",
            re.IGNORECASE,
        ),
    ),
    (
        "oem_group",
        re.compile(
            rf"(?P<a>{_FIRM})\s+"
            r"(?:and|&)\s+"
            rf"(?P<b>{_FIRM})\s+"
            r"(?:are\s+)?(?:part\s+of|belong\s+to|within)\s+(?:the\s+)?(?:same\s+)?"
            r"(?:group|IMC|holding)",
            re.IGNORECASE,
        ),
    ),
]

_NAME_STOP = {
    "the",
    "and",
    "of",
    "for",
    "with",
    "from",
    "is",
    "a",
    "an",
    "group",
    "company",
    "authorized",
    "distributor",
    "dealer",
    "brand",
    "subsidiary",
}


def extract_relation_candidates(
    text: str,
    *,
    url: str = "",
    known: KnownFirmsIndex | None = None,
    max_relations: int = 20,
) -> list[dict[str, Any]]:
    """Heurystyczna ekstrakcja relacji firm z tekstu (news / Notion / katalog)."""
    if not text:
        return []
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # dziel na zdania — ogranicza sklejanie nazw przez kropki
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    if not sentences:
        sentences = [cleaned]
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    for sentence in sentences:
        for relation_type, pattern in _RELATION_PATTERNS:
            for m in pattern.finditer(sentence):
                a = m.group("a").strip(" ,.;:/-")
                b = m.group("b").strip(" ,.;:/-")
                # brand_of z listą "GARANT / HOLEX"
                if relation_type == "brand_of" and "/" in a:
                    brands = [x.strip() for x in a.split("/") if x.strip()]
                    for brand in brands:
                        _push_relation(
                            found,
                            seen,
                            source=brand,
                            target=b,
                            relation_type="brand_of",
                            evidence=m.group(0)[:300],
                            url=url,
                            known=known,
                        )
                    continue
                _push_relation(
                    found,
                    seen,
                    source=a,
                    target=b,
                    relation_type=relation_type,
                    evidence=m.group(0)[:300],
                    url=url,
                    known=known,
                )
                if len(found) >= max_relations:
                    return found
    return found[:max_relations]


def _push_relation(
    found: list[dict[str, Any]],
    seen: set[str],
    *,
    source: str,
    target: str,
    relation_type: str,
    evidence: str,
    url: str,
    known: KnownFirmsIndex | None,
) -> None:
    source = source.strip(" ,.;:/-")
    target = target.strip(" ,.;:/-")
    if len(source) < 2 or len(target) < 2:
        return
    if source.lower() in _NAME_STOP or target.lower() in _NAME_STOP:
        return
    # odrzuć nazwy złożone tylko ze stopwords
    if all(t.lower() in _NAME_STOP for t in re.split(r"[\s/-]+", source) if t):
        return
    if all(t.lower() in _NAME_STOP for t in re.split(r"[\s/-]+", target) if t):
        return
    if normalize_firm_name(source) == normalize_firm_name(target):
        return
    # jeśli mamy indeks — spróbuj zmapować na kanoniczne nazwy
    if known is not None:
        ms = known.match(source)
        mt = known.match(target)
        if ms and ms.get("company"):
            source = str(ms["company"])
        if mt and mt.get("company"):
            target = str(mt["company"])
    key = _edge_key(source, target, relation_type)
    if key in seen:
        return
    seen.add(key)
    # pewność wyższa gdy obie strony znane
    conf = 0.55
    if known is not None:
        conf = 0.75 if known.is_known(source) and known.is_known(target) else 0.6
    found.append(
        {
            "source": source,
            "target": target,
            "relation_type": relation_type,
            "evidence": evidence,
            "url": url,
            "confidence": conf,
            "origin": "extracted",
            "status": "relation_candidate",
        }
    )


def similar_company(a: str, b: str, threshold: float = 0.88) -> bool:
    return SequenceMatcher(None, normalize_firm_name(a), normalize_firm_name(b)).ratio() >= threshold
