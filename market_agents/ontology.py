"""
Graph knowledge / ontology dla TIZ (obróbka skrawaniem).

Lokalna baza kontekstu zamiast drogiego „sztabu” na Grok/xAI:
agenty rozumieją materiały, maszyny, chłodziwo i procesy technologiczne
oraz powiązania między nimi (i firmami).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ENTITY_TYPES = (
    "firm",
    "material",
    "machine",
    "coolant",
    "process",
    "tool_family",
    "standard",
    "parameter",
    "literature",
    "concept",
)

EDGE_TYPES = (
    "suitable_for_material",  # tool/process → material
    "used_on_machine",  # process/tool → machine
    "requires_coolant",  # process/tool → coolant
    "process_is",  # operation alias → process
    "tool_for_process",  # tool_family → process
    "competes_with",  # firm → firm
    "makes_tool",  # firm → tool_family
    "governed_by",  # entity → standard
    "mentions",  # literature/doc → entity
    "related_to",  # generic
    "has_parameter",  # process/tool → parameter (schema only)
    "applies_to_group",  # material subclass → ISO group
    # firm network (z firm_relations)
    "distributor_of",
    "dealer_of",
    "brand_of",
    "subsidiary_of",
    "oem_group",
    "partner_of",
    "rebrand_of",
    "focuses_on",  # firm → process/tool_family/material
)

_VALID_ENTITIES = set(ENTITY_TYPES)
_VALID_EDGES = set(EDGE_TYPES)


def _slug(text: str, prefix: str = "") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = s[:64] or "node"
    return f"{prefix}{s}" if prefix else s


@dataclass
class OntologyNode:
    id: str
    type: str
    label: str
    aliases: list[str] = field(default_factory=list)
    props: dict[str, Any] = field(default_factory=dict)
    origin: str = "seed"
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OntologyNode":
        etype = str(raw.get("type") or "concept")
        if etype not in _VALID_ENTITIES:
            etype = "concept"
        label = str(raw.get("label") or raw.get("id") or "node")
        nid = str(raw.get("id") or _slug(label, f"{etype}:"))
        return cls(
            id=nid,
            type=etype,
            label=label,
            aliases=[str(a) for a in (raw.get("aliases") or []) if a],
            props=dict(raw.get("props") or {}),
            origin=str(raw.get("origin") or "manual"),
            updated_at=float(raw.get("updated_at") or time.time()),
        )


@dataclass
class OntologyEdge:
    source: str
    target: str
    relation: str
    evidence: str = ""
    confidence: float = 0.8
    origin: str = "seed"
    props: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.source}|{self.relation}|{self.target}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OntologyEdge | None":
        source = str(raw.get("source") or "").strip()
        target = str(raw.get("target") or "").strip()
        relation = str(raw.get("relation") or raw.get("relation_type") or "").strip()
        if not source or not target or relation not in _VALID_EDGES:
            return None
        return cls(
            source=source,
            target=target,
            relation=relation,
            evidence=str(raw.get("evidence") or "")[:600],
            confidence=float(raw.get("confidence") or 0.8),
            origin=str(raw.get("origin") or "manual"),
            props=dict(raw.get("props") or {}),
        )


# --- Domain detectors (context for bots) ---------------------------------

_MATERIAL_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("material:iso-p", "ISO P (steels)", re.compile(r"\b(?:ISO\s*)?P(?:\d{2})?\b|\bsteel|stal(?:e|i)?\b|carbon\s*steel|alloy\s*steel", re.I)),
    ("material:iso-m", "ISO M (stainless)", re.compile(r"\b(?:ISO\s*)?M(?:\d{2})?\b|\bstainless|nierdzewn|inox|austenitic", re.I)),
    ("material:iso-k", "ISO K (cast iron)", re.compile(r"\b(?:ISO\s*)?K(?:\d{2})?\b|\bcast\s*iron|żeliw|gg\d+|gjl|gjs", re.I)),
    ("material:iso-n", "ISO N (non-ferrous)", re.compile(r"\b(?:ISO\s*)?N(?:\d{2})?\b|\baluminum|aluminium|aluminium|copper|brass|mosiądz|miedź|non[\s-]?ferrous", re.I)),
    ("material:iso-s", "ISO S (HRSA / titanium)", re.compile(r"\b(?:ISO\s*)?S(?:\d{2})?\b|\btitanium|tytan|inconel|hastelloy|hrsa|superalloy|nickel\s*alloy", re.I)),
    ("material:iso-h", "ISO H (hardened)", re.compile(r"\b(?:ISO\s*)?H(?:\d{2})?\b|\bhardened|hartowan|tool\s*steel|hrc\s*\d+", re.I)),
]

_MACHINE_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("machine:cnc-lathe", "CNC lathe / tokarka", re.compile(r"\b(?:cnc\s*)?lathe|tokark|turning\s*center|drehmaschine|swiss[\s-]?type", re.I)),
    ("machine:machining-center", "Machining center / frezarka", re.compile(r"\bmachining\s*center|fr[aä]smaschine|frezark|vmc|hmc|5[\s-]?axis|pi[eę]cioosi", re.I)),
    ("machine:drill-tap", "Drill/tap center", re.compile(r"\bdrill[\s/-]?tap|wiercarko|tapping\s*center", re.I)),
    ("machine:grinder", "Grinder", re.compile(r"\bgrinder|szlifierk|grinding\s*machine", re.I)),
    ("machine:multi-task", "Multi-task / mill-turn", re.compile(r"\bmill[\s-]?turn|multi[\s-]?task|turn[\s-]?mill|wielozadani", re.I)),
]

_COOLANT_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("coolant:emulsion", "Emulsion / flood", re.compile(r"\bemulsion|flood\s*coolant|nassbearbeitung|ch[lł]odziwo\s*emuls|soluble\s*oil", re.I)),
    ("coolant:mql", "MQL / minimal quantity", re.compile(r"\bmql|minimal\s*quantity|minimalne\s*smarowanie|mikroschmierung|near[\s-]?dry", re.I)),
    ("coolant:dry", "Dry machining", re.compile(r"\bdry\s*machining|trockenbearbeitung|obr[oó]bka\s*na\s*sucho|without\s*coolant", re.I)),
    ("coolant:oil", "Straight oil", re.compile(r"\bstraight\s*oil|cutting\s*oil|olej\s*skrawaj|neat\s*oil", re.I)),
    ("coolant:through-tool", "Through-tool coolant", re.compile(r"\bthrough[\s-]?tool|internal\s*coolant|ch[lł]odzenie\s*wewn[eę]trzn|iks\b|coolant\s*through", re.I)),
]

_PROCESS_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("process:turning", "Turning / toczenie", re.compile(r"\bturning|toczenie|drehen|lathe\s*work", re.I)),
    ("process:milling", "Milling / frezowanie", re.compile(r"\bmilling|frezowanie|fr[aä]sen|end\s*milling|face\s*milling", re.I)),
    ("process:drilling", "Drilling / wiercenie", re.compile(r"\bdrilling|wiercenie|bohren", re.I)),
    ("process:threading", "Threading / gwintowanie", re.compile(r"\bthreading|gwintowanie|gewinde|tapping|tap\b", re.I)),
    ("process:reaming", "Reaming / rozwiertanie", re.compile(r"\breaming|rozwiert|reiben", re.I)),
    ("process:boring", "Boring / wytaczanie", re.compile(r"\bboring|wytaczanie|ausdrehen", re.I)),
    ("process:parting", "Parting / odcinanie", re.compile(r"\bparting|odcinanie|grooving|rowkowanie|abstechen", re.I)),
    ("process:hsm", "High-speed machining", re.compile(r"\bhsm|high[\s-]?speed\s*machining|wysokopr[eę]dko[sś]ciow", re.I)),
]

_PARAM_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("parameter:vc", "vc — cutting speed", re.compile(r"\bv[_ ]?c\b|cutting\s*speed|schnittgeschwindigkeit|pr[eę]dko[sś][cć]\s*skrawania", re.I)),
    ("parameter:fz", "fz — feed per tooth", re.compile(r"\bf[_ ]?z\b|feed\s*per\s*tooth|zahnvorschub|posuw\s*na\s*ostrze", re.I)),
    ("parameter:ap", "ap — depth of cut", re.compile(r"\ba[_ ]?p\b|depth\s*of\s*cut|schnittiefe|g[lł][eę]boko[sś][cć]\s*skrawania", re.I)),
    ("parameter:ae", "ae — width of cut", re.compile(r"\ba[_ ]?e\b|width\s*of\s*cut|schnittbreite|szeroko[sś][cć]\s*skrawania", re.I)),
    ("parameter:fn", "fn — feed per rev", re.compile(r"\bf[_ ]?n\b|feed\s*per\s*rev|posuw\s*na\s*obr[oó]t", re.I)),
]


def extract_domain_context(text: str) -> dict[str, Any]:
    """
    Wyodrębnij kontekst technologiczny z tekstu:
    materiały, maszyny, chłodziwo, procesy, parametry (tylko obecność schematu).
    """
    raw = text or ""
    materials = _match_group(_MATERIAL_PATTERNS, raw)
    machines = _match_group(_MACHINE_PATTERNS, raw)
    coolants = _match_group(_COOLANT_PATTERNS, raw)
    processes = _match_group(_PROCESS_PATTERNS, raw)
    parameters = _match_group(_PARAM_PATTERNS, raw)
    return {
        "ok": True,
        "materials": materials,
        "machines": machines,
        "coolants": coolants,
        "processes": processes,
        "parameters": parameters,
        "summary": _context_summary(materials, machines, coolants, processes, parameters),
        "policy": (
            "Kontekst technologiczny do rozumienia dokumentów — "
            "nie kopiować tabel wartości vc/fz do CutData."
        ),
    }


def _match_group(
    patterns: list[tuple[str, str, re.Pattern[str]]], text: str
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for nid, label, pat in patterns:
        if nid in seen:
            continue
        if pat.search(text):
            seen.add(nid)
            out.append({"id": nid, "label": label})
    return out


def _context_summary(
    materials: list[dict[str, str]],
    machines: list[dict[str, str]],
    coolants: list[dict[str, str]],
    processes: list[dict[str, str]],
    parameters: list[dict[str, str]],
) -> str:
    parts = []
    if materials:
        parts.append("materiały: " + ", ".join(m["label"] for m in materials))
    if machines:
        parts.append("maszyny: " + ", ".join(m["label"] for m in machines))
    if coolants:
        parts.append("chłodziwo: " + ", ".join(m["label"] for m in coolants))
    if processes:
        parts.append("procesy: " + ", ".join(m["label"] for m in processes))
    if parameters:
        parts.append("parametry (schemat): " + ", ".join(m["id"].split(":")[-1] for m in parameters))
    return "; ".join(parts) if parts else "brak wykrytego kontekstu technologicznego"


def firm_node_id(company: str) -> str:
    return _slug(company, "firm:")


_FOCUS_MAP: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"drill|wiert", re.I), "tool_family:drill", "makes_tool"),
    (re.compile(r"tap|gwint|thread", re.I), "tool_family:tap", "makes_tool"),
    (re.compile(r"end\s*mill|frez", re.I), "tool_family:end-mill", "makes_tool"),
    (re.compile(r"insert|płytk|wendeschneid", re.I), "tool_family:insert", "makes_tool"),
    (re.compile(r"turning|toczen|lathe", re.I), "tool_family:turning-tool", "makes_tool"),
    (re.compile(r"holder|oprawk", re.I), "tool_family:tool-holder", "makes_tool"),
    (re.compile(r"deep[\s-]?hole", re.I), "process:drilling", "focuses_on"),
    (re.compile(r"milling|frezowan", re.I), "process:milling", "focuses_on"),
    (re.compile(r"turning|toczen", re.I), "process:turning", "focuses_on"),
    (re.compile(r"diamond|cbn|pkd|pcd", re.I), "material:iso-h", "focuses_on"),
    (re.compile(r"carbide|węglik", re.I), "material:iso-p", "focuses_on"),
]


class MachiningOntology:
    """Lokalny knowledge graph database (data/knowledge/ontology.json)."""

    FILENAME = "ontology.json"

    def __init__(
        self,
        nodes: dict[str, OntologyNode] | None = None,
        edges: list[OntologyEdge] | None = None,
    ) -> None:
        self.nodes: dict[str, OntologyNode] = dict(nodes or {})
        self.edges: list[OntologyEdge] = []
        self._keys: set[str] = set()
        for edge in edges or []:
            self._add_edge_obj(edge)

    @classmethod
    def path_for(cls, data_dir: Path | str) -> Path:
        return Path(data_dir) / "knowledge" / cls.FILENAME

    @classmethod
    def load(cls, data_dir: Path | str, seed_path: Path | str | None = None) -> "MachiningOntology":
        path = cls.path_for(data_dir)
        graph = cls()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                for row in raw.get("nodes") or []:
                    if isinstance(row, dict):
                        node = OntologyNode.from_dict(row)
                        graph.nodes[node.id] = node
                for row in raw.get("edges") or []:
                    if isinstance(row, dict):
                        edge = OntologyEdge.from_dict(row)
                        if edge:
                            graph._add_edge_obj(edge)
            except Exception:  # noqa: BLE001
                pass
        seed = Path(seed_path) if seed_path else Path("config/machining_ontology.seed.json")
        if seed.exists() and (not graph.nodes or len(graph.nodes) < 5):
            graph.merge_seed(seed)
        return graph

    def merge_seed(self, seed_path: Path | str) -> int:
        path = Path(seed_path)
        if not path.exists():
            return 0
        raw = json.loads(path.read_text(encoding="utf-8"))
        before_n = len(self.nodes)
        before_e = len(self.edges)
        for row in raw.get("nodes") or []:
            if isinstance(row, dict):
                node = OntologyNode.from_dict({**row, "origin": row.get("origin") or "seed"})
                if node.id not in self.nodes:
                    self.nodes[node.id] = node
                else:
                    # uzupełnij aliasy / props z seeda
                    existing = self.nodes[node.id]
                    existing.aliases = list(dict.fromkeys([*existing.aliases, *node.aliases]))
                    for k, v in node.props.items():
                        existing.props.setdefault(k, v)
        for row in raw.get("edges") or []:
            if isinstance(row, dict):
                edge = OntologyEdge.from_dict({**row, "origin": row.get("origin") or "seed"})
                if edge:
                    self._add_edge_obj(edge)
        return (len(self.nodes) - before_n) + (len(self.edges) - before_e)

    def build_from_knowledge(
        self,
        data_dir: Path | str,
        *,
        seed_path: str | Path = "config/machining_ontology.seed.json",
        firms_seed: str | Path = "config/known_firms.seed.json",
        relations_seed: str | Path = "config/firm_relations.seed.json",
    ) -> dict[str, Any]:
        """
        Zbuduj / odśwież knowledge graph z:
        seed ontologii + known_firms + firm_relations + literature + product_tech.
        """
        data_dir = Path(data_dir)
        stats = {
            "seed": 0,
            "firms": 0,
            "firm_edges": 0,
            "focus_edges": 0,
            "literature": 0,
            "product_tech": 0,
        }
        stats["seed"] = self.merge_seed(seed_path)

        # Known firms → firm nodes + focuses_on / makes_tool z product_focus
        firms_path = data_dir / "knowledge" / "known_firms.json"
        firms_rows: list[dict[str, Any]] = []
        for candidate in (firms_path, Path(firms_seed)):
            if candidate.exists():
                try:
                    raw = json.loads(candidate.read_text(encoding="utf-8"))
                    if isinstance(raw, list):
                        firms_rows = raw
                    elif isinstance(raw, dict):
                        firms_rows = list(raw.get("firms") or raw.get("items") or [])
                    break
                except Exception:  # noqa: BLE001
                    continue
        for row in firms_rows:
            if not isinstance(row, dict):
                continue
            company = str(row.get("company") or row.get("name") or "").strip()
            if not company:
                continue
            nid = firm_node_id(company)
            before = nid in self.nodes
            self.upsert_node(
                entity_type="firm",
                label=company,
                node_id=nid,
                origin="known_firms",
                props={
                    k: row.get(k)
                    for k in ("country", "site_url", "downloads", "product_focus", "notion_url")
                    if row.get(k)
                },
            )
            if not before:
                stats["firms"] += 1
            focus = str(row.get("product_focus") or "")
            for pat, target, rel in _FOCUS_MAP:
                if pat.search(focus) and target in self.nodes:
                    if self.add_edge(
                        nid, target, rel,
                        evidence=f"product_focus: {focus}",
                        confidence=0.7,
                        origin="known_firms",
                    ).get("added"):
                        stats["focus_edges"] += 1

        # Firm relations → ontology edges
        from market_agents.firm_relations import FirmRelationsGraph, RELATION_TYPES

        rel_graph = FirmRelationsGraph.load(data_dir=data_dir, seed_path=relations_seed)
        for edge in rel_graph.edges:
            src = firm_node_id(str(edge.get("source") or ""))
            tgt = firm_node_id(str(edge.get("target") or ""))
            rtype = str(edge.get("relation_type") or "")
            if not src or not tgt or rtype not in RELATION_TYPES:
                continue
            self.upsert_node(
                entity_type="firm",
                label=str(edge.get("source")),
                node_id=src,
                origin="firm_relations",
            )
            self.upsert_node(
                entity_type="firm",
                label=str(edge.get("target")),
                node_id=tgt,
                origin="firm_relations",
            )
            mapped = rtype if rtype in _VALID_EDGES else "related_to"
            if self.add_edge(
                src,
                tgt,
                mapped,
                evidence=str(edge.get("evidence") or ""),
                confidence=float(edge.get("confidence") or 0.7),
                origin="firm_relations",
            ).get("added"):
                stats["firm_edges"] += 1

        # Literature → mentions topics as concepts / processes
        lit_path = data_dir / "knowledge" / "literature.json"
        if lit_path.exists():
            try:
                lit = json.loads(lit_path.read_text(encoding="utf-8"))
                for item in lit.get("items") or []:
                    if not isinstance(item, dict) or not item.get("url"):
                        continue
                    lid = _slug(str(item.get("id") or item.get("url")), "literature:")
                    self.upsert_node(
                        entity_type="literature",
                        label=str(item.get("title") or lid)[:200],
                        node_id=lid,
                        origin="literature",
                        props={"url": item.get("url"), "kind": item.get("kind")},
                    )
                    stats["literature"] += 1
                    for topic in item.get("topics") or []:
                        topic_id = {
                            "cutting_dynamics": "process:hsm",
                            "tool_design": "tool_family:end-mill",
                            "parameters": "parameter:vc",
                            "coolant": "coolant:emulsion",
                            "iso13399": "standard:iso13399",
                            "cam": "concept:cam",
                            "surface": "concept:surface-integrity",
                            "ai": "concept:ai-machining",
                        }.get(str(topic))
                        if topic_id:
                            if topic_id.startswith("concept:") and topic_id not in self.nodes:
                                self.upsert_node(
                                    entity_type="concept",
                                    label=str(topic),
                                    node_id=topic_id,
                                    origin="literature",
                                )
                            self.add_edge(
                                lid, topic_id, "mentions",
                                evidence=str(topic),
                                confidence=0.6,
                                origin="literature",
                            )
            except Exception:  # noqa: BLE001
                pass

        # Product tech brands → firm + focuses_on process/cutting_data concept
        tech_path = data_dir / "knowledge" / "product_tech.json"
        if tech_path.exists():
            try:
                tech = json.loads(tech_path.read_text(encoding="utf-8"))
                for item in tech.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    brand = str(item.get("brand") or "").strip()
                    if not brand:
                        continue
                    fid = firm_node_id(brand)
                    self.upsert_node(
                        entity_type="firm",
                        label=brand,
                        node_id=fid,
                        origin="product_tech",
                    )
                    kind = str(item.get("kind") or "")
                    target = {
                        "cutting_data": "parameter:vc",
                        "handbook": "concept:handbook",
                        "iso13399": "standard:iso13399",
                        "application_guide": "concept:application-guide",
                    }.get(kind)
                    if target:
                        if target.startswith("concept:") and target not in self.nodes:
                            self.upsert_node(
                                entity_type="concept",
                                label=kind,
                                node_id=target,
                                origin="product_tech",
                            )
                        if self.add_edge(
                            fid, target, "focuses_on",
                            evidence=str(item.get("url") or kind),
                            confidence=0.65,
                            origin="product_tech",
                        ).get("added"):
                            stats["product_tech"] += 1
            except Exception:  # noqa: BLE001
                pass

        path = self.save(data_dir)
        return {
            "ok": True,
            "path": str(path),
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "stats": stats,
            "by_type": self.domain_brief().get("by_type"),
        }

    def find_path(
        self,
        source: str,
        target: str,
        *,
        max_depth: int = 5,
    ) -> dict[str, Any]:
        """Najkrótsza ścieżka w grafie (BFS, nieskierowany do eksploracji kontekstu)."""
        src = self._resolve_id(source)
        tgt = self._resolve_id(target)
        if not src or not tgt:
            return {
                "ok": False,
                "error": f"Nie znaleziono: source={source!r} target={target!r}",
            }
        if src == tgt:
            return {"ok": True, "path": [src], "edges": [], "length": 0, "readable": src}

        adj: dict[str, list[tuple[str, OntologyEdge]]] = {}
        for edge in self.edges:
            adj.setdefault(edge.source, []).append((edge.target, edge))
            adj.setdefault(edge.target, []).append((edge.source, edge))

        from collections import deque

        queue: deque[tuple[str, int]] = deque([(src, 0)])
        prev: dict[str, tuple[str, OntologyEdge] | None] = {src: None}
        found = False
        while queue:
            cur, depth = queue.popleft()
            if cur == tgt:
                found = True
                break
            if depth >= max_depth:
                continue
            for nxt, edge in adj.get(cur, []):
                if nxt not in prev:
                    prev[nxt] = (cur, edge)
                    queue.append((nxt, depth + 1))

        if not found or tgt not in prev:
            return {"ok": False, "error": "Brak ścieżki", "source": src, "target": tgt}

        nodes_path = [tgt]
        edges_path: list[dict[str, Any]] = []
        cur = tgt
        while cur != src:
            pair = prev[cur]
            assert pair is not None
            parent, edge = pair
            edges_path.append(edge.to_dict())
            nodes_path.append(parent)
            cur = parent
        nodes_path.reverse()
        edges_path.reverse()
        labels = [
            self.nodes[n].label if n in self.nodes else n for n in nodes_path
        ]
        return {
            "ok": True,
            "source": src,
            "target": tgt,
            "path": nodes_path,
            "labels": labels,
            "edges": edges_path,
            "length": len(edges_path),
            "readable": " → ".join(labels),
        }

    def export_graphml(self, path: Path | str) -> Path:
        """Eksport GraphML (Gephi / yEd / Neo4j tools)."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
            '<key id="label" for="node" attr.name="label" attr.type="string"/>',
            '<key id="type" for="node" attr.name="type" attr.type="string"/>',
            '<key id="relation" for="edge" attr.name="relation" attr.type="string"/>',
            '<graph id="TIZ" edgedefault="directed">',
        ]
        for node in self.nodes.values():
            safe_id = node.id.replace('"', "")
            safe_label = node.label.replace("&", "&amp;").replace("<", "&lt;").replace('"', "'")
            lines.append(
                f'<node id="{safe_id}">'
                f'<data key="label">{safe_label}</data>'
                f'<data key="type">{node.type}</data>'
                f"</node>"
            )
        for i, edge in enumerate(self.edges):
            lines.append(
                f'<edge id="e{i}" source="{edge.source}" target="{edge.target}">'
                f'<data key="relation">{edge.relation}</data>'
                f"</edge>"
            )
        lines.append("</graph>")
        lines.append("</graphml>")
        out.write_text("\n".join(lines), encoding="utf-8")
        return out

    def export_jsonld(self, path: Path | str) -> Path:
        """Eksport JSON-LD (lekka ontologia Linked Data)."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        graph = []
        for node in self.nodes.values():
            graph.append(
                {
                    "@id": f"tiz:{node.id}",
                    "@type": f"tiz:{node.type}",
                    "name": node.label,
                    "alternateName": node.aliases,
                    **{k: v for k, v in node.props.items() if v is not None},
                }
            )
        for edge in self.edges:
            graph.append(
                {
                    "@id": f"tiz:edge:{edge.key()}",
                    "@type": f"tiz:{edge.relation}",
                    "source": f"tiz:{edge.source}",
                    "target": f"tiz:{edge.target}",
                    "confidence": edge.confidence,
                    "evidence": edge.evidence,
                }
            )
        payload = {
            "@context": {
                "tiz": "https://tiz.local/ontology/",
                "name": "http://schema.org/name",
                "alternateName": "http://schema.org/alternateName",
            },
            "@graph": graph,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    def save(self, data_dir: Path | str) -> Path:
        path = self.path_for(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "entity_types": list(ENTITY_TYPES),
            "edge_types": list(EDGE_TYPES),
            "nodes": [n.to_dict() for n in sorted(self.nodes.values(), key=lambda x: (x.type, x.label))],
            "edges": [e.to_dict() for e in self.edges],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def upsert_node(
        self,
        *,
        entity_type: str,
        label: str,
        node_id: str | None = None,
        aliases: list[str] | None = None,
        props: dict[str, Any] | None = None,
        origin: str = "manual",
    ) -> OntologyNode:
        etype = entity_type if entity_type in _VALID_ENTITIES else "concept"
        nid = (node_id or _slug(label, f"{etype}:")).strip()
        existing = self.nodes.get(nid)
        if existing:
            if aliases:
                existing.aliases = list(dict.fromkeys([*existing.aliases, *aliases]))
            if props:
                existing.props.update(props)
            existing.updated_at = time.time()
            return existing
        node = OntologyNode(
            id=nid,
            type=etype,
            label=label,
            aliases=list(aliases or []),
            props=dict(props or {}),
            origin=origin,
        )
        self.nodes[nid] = node
        return node

    def add_edge(
        self,
        source: str,
        target: str,
        relation: str,
        *,
        evidence: str = "",
        confidence: float = 0.8,
        origin: str = "manual",
        props: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if relation not in _VALID_EDGES:
            raise ValueError(f"Nieznany relation={relation}. Dozwolone: {', '.join(EDGE_TYPES)}")
        source = source.strip()
        target = target.strip()
        if not source or not target:
            raise ValueError("source i target są wymagane")
        # auto-create stub nodes if missing
        if source not in self.nodes:
            self.upsert_node(
                entity_type=source.split(":")[0] if ":" in source else "concept",
                label=source.split(":")[-1],
                node_id=source,
                origin=origin,
            )
        if target not in self.nodes:
            self.upsert_node(
                entity_type=target.split(":")[0] if ":" in target else "concept",
                label=target.split(":")[-1],
                node_id=target,
                origin=origin,
            )
        edge = OntologyEdge(
            source=source,
            target=target,
            relation=relation,
            evidence=evidence,
            confidence=confidence,
            origin=origin,
            props=dict(props or {}),
        )
        added = self._add_edge_obj(edge)
        return {"ok": True, "added": added, "edge": edge.to_dict()}

    def _add_edge_obj(self, edge: OntologyEdge) -> bool:
        key = edge.key()
        if key in self._keys:
            return False
        self.edges.append(edge)
        self._keys.add(key)
        return True

    def list_nodes(
        self,
        entity_type: str | None = None,
        q: str | None = None,
        limit: int = 100,
    ) -> list[OntologyNode]:
        et = (entity_type or "").strip().lower()
        qq = (q or "").strip().lower()
        out: list[OntologyNode] = []
        for node in sorted(self.nodes.values(), key=lambda x: (x.type, x.label)):
            if et and node.type != et:
                continue
            if qq:
                blob = f"{node.id} {node.label} {' '.join(node.aliases)}".lower()
                if qq not in blob:
                    continue
            out.append(node)
            if len(out) >= limit:
                break
        return out

    def neighborhood(self, node_id_or_label: str, depth: int = 1) -> dict[str, Any]:
        nid = self._resolve_id(node_id_or_label)
        if not nid:
            return {"ok": False, "error": f"Nie znaleziono: {node_id_or_label}"}
        frontier = {nid}
        seen = {nid}
        collected_edges: list[OntologyEdge] = []
        for _ in range(max(1, depth)):
            nxt: set[str] = set()
            for edge in self.edges:
                if edge.source in frontier or edge.target in frontier:
                    collected_edges.append(edge)
                    for end in (edge.source, edge.target):
                        if end not in seen:
                            seen.add(end)
                            nxt.add(end)
            frontier = nxt
            if not frontier:
                break
        nodes = [self.nodes[i].to_dict() for i in sorted(seen) if i in self.nodes]
        # dedupe edges
        uniq: dict[str, OntologyEdge] = {}
        for e in collected_edges:
            uniq[e.key()] = e
        return {
            "ok": True,
            "center": self.nodes[nid].to_dict() if nid in self.nodes else {"id": nid},
            "depth": depth,
            "nodes": nodes,
            "edges": [e.to_dict() for e in uniq.values()],
            "counts": {"nodes": len(nodes), "edges": len(uniq)},
        }

    def ingest_context(
        self,
        context: dict[str, Any],
        *,
        source_url: str = "",
        origin: str = "extracted",
    ) -> dict[str, Any]:
        """Wgraj wynik extract_domain_context do grafu + krawędzie related_to między wykrytymi."""
        added_nodes = 0
        added_edges = 0
        ids: list[str] = []
        for group, relation_hint in (
            ("materials", None),
            ("machines", None),
            ("coolants", None),
            ("processes", None),
            ("parameters", None),
        ):
            for row in context.get(group) or []:
                if not isinstance(row, dict):
                    continue
                nid = str(row.get("id") or "")
                label = str(row.get("label") or nid)
                if not nid:
                    continue
                etype = nid.split(":")[0] if ":" in nid else "concept"
                before = nid in self.nodes
                self.upsert_node(
                    entity_type=etype,
                    label=label,
                    node_id=nid,
                    origin=origin,
                    props={"from_url": source_url} if source_url else {},
                )
                if not before:
                    added_nodes += 1
                ids.append(nid)
        # link process → material / machine / coolant when co-mentioned
        processes = [i for i in ids if i.startswith("process:")]
        materials = [i for i in ids if i.startswith("material:")]
        machines = [i for i in ids if i.startswith("machine:")]
        coolants = [i for i in ids if i.startswith("coolant:")]
        params = [i for i in ids if i.startswith("parameter:")]
        for proc in processes:
            for mat in materials:
                if self.add_edge(
                    proc, mat, "suitable_for_material",
                    evidence=context.get("summary") or "",
                    confidence=0.55,
                    origin=origin,
                ).get("added"):
                    added_edges += 1
            for mac in machines:
                if self.add_edge(
                    proc, mac, "used_on_machine",
                    evidence=context.get("summary") or "",
                    confidence=0.55,
                    origin=origin,
                ).get("added"):
                    added_edges += 1
            for cool in coolants:
                if self.add_edge(
                    proc, cool, "requires_coolant",
                    evidence=context.get("summary") or "",
                    confidence=0.55,
                    origin=origin,
                ).get("added"):
                    added_edges += 1
            for param in params:
                if self.add_edge(
                    proc, param, "has_parameter",
                    evidence="schema mention only",
                    confidence=0.5,
                    origin=origin,
                ).get("added"):
                    added_edges += 1
        return {
            "ok": True,
            "added_nodes": added_nodes,
            "added_edges": added_edges,
            "entity_ids": ids,
            "summary": context.get("summary"),
        }

    def domain_brief(self) -> dict[str, Any]:
        """Skrót ontologii dla promptu agenta (tani lokalny kontekst zamiast Grok)."""
        by_type: dict[str, int] = {}
        for n in self.nodes.values():
            by_type[n.type] = by_type.get(n.type, 0) + 1
        materials = [n.label for n in self.list_nodes(entity_type="material", limit=20)]
        machines = [n.label for n in self.list_nodes(entity_type="machine", limit=15)]
        coolants = [n.label for n in self.list_nodes(entity_type="coolant", limit=10)]
        processes = [n.label for n in self.list_nodes(entity_type="process", limit=20)]
        return {
            "ok": True,
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "by_type": by_type,
            "materials": materials,
            "machines": machines,
            "coolants": coolants,
            "processes": processes,
            "hint": (
                "Używaj tych encji jako kontekstu przy parsowaniu katalogów/handbooków. "
                "Nie kopiuj tabel vc/fz do CutData — tylko schemat pól i powiązania."
            ),
        }

    def _resolve_id(self, ref: str) -> str | None:
        ref = (ref or "").strip()
        if not ref:
            return None
        if ref in self.nodes:
            return ref
        low = ref.lower()
        for node in self.nodes.values():
            if node.label.lower() == low or low in [a.lower() for a in node.aliases]:
                return node.id
            if node.id.endswith(":" + _slug(ref)) or node.id == _slug(ref):
                return node.id
        # partial
        for node in self.nodes.values():
            if low in node.label.lower() or low in node.id:
                return node.id
        return None
