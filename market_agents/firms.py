from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

# Typowe „śmieci” z tytułów newsów — nie są nazwami firm
_STOP_TOKENS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "into",
    "over",
    "that",
    "can",
    "will",
    "its",
    "new",
    "news",
    "first",
    "time",
    "year",
    "old",
    "next",
    "generation",
    "high",
    "launches",
    "launch",
    "unveils",
    "introduces",
    "announces",
    "releases",
    "presents",
    "expands",
    "opens",
    "acquires",
    "partners",
    "exhibits",
    "showcases",
    "secures",
    "invests",
    "promote",
    "promotes",
    "predicts",
    "redefine",
    "revealed",
    "finalists",
    "booster",
    "connecting",
    "world",
    "take",
    "part",
    "adds",
    "makes",
    "debut",
    "european",
    "swiss",
    "luxury",
    "watch",
    "watchmaking",
    "startups",
    "startup",
    "spin",
    "off",
    "tech",
    "diamond",
    "built",
    "chicago",
    "media",
    "pes",
    "ceo",
    "agents",
    "business",
    "ideas",
    "profitable",
    "youth",
    "owner",
    "lessons",
    "hard",
    "contributing",
    "solving",
    "challenges",
    "through",
    "infrastructure",
    "outer",
    "space",
    "cutting",
    "tool",
    "tools",
    "tooling",
    "carbide",
    "machining",
    "milling",
    "drilling",
    "turning",
    "catalog",
    "catalogue",
    "digital",
    "online",
    "pdf",
    "handbook",
    "industry",
    "industries",
    "market",
    "global",
    "group",
    "groupe",
    "company",
    "inc",
    "ltd",
    "llc",
    "gmbh",
    "ag",
    "sa",
    "spa",
    "co",
    "corp",
    "corporation",
    "technologies",
    "technology",
    "solutions",
    "systems",
    "precision",
    "manufacturing",
    "manufacturers",
    "manufacturer",
    "exhibition",
    "fair",
    "show",
    "team",
    "foundation",
    "alloy",
    "composites",
    "amb",
    "emo",
    "imts",
    "jimtof",
    "imtex",
    "fabtech",
    "jec",
    "photos",
    "key",
    "acquisitions",
    "acquisition",
    "enabling",
    "high-speed",
    "high",
    "speed",
    "precision",
    "small",
    "automotive",
    "gears",
    "gear",
    "hobbing",
    "shaping",
    "helical",
    "dry-cut",
    "dry",
    "cut",
    "machine",
    "machines",
    "brand",
    "products",
    "product",
    "cemented",
    "lightweight",
    "scales",
    "support",
    "mississauga",
    "chicago",
    "hypepotamus",
    "mynewsdesk",
    "engineering",
    "insauga",
    "develops",
    "developed",
    "offering",
    "debut",
    "international",
    "expo",
    "axis",
    "system",
    "fully",
    "programmable",
    "nc",
    "modern",
    "shop",
    "berry",
}

# Wydawcy / portale — nie są firmami tooling
_PUBLISHER_NAMES = {
    "hypepotamus",
    "mynewsdesk",
    "pes media",
    "pes",
    "engineering news",
    "plastics technology",
    "modern machine shop",
    "insauga",
    "canadian metalworking",
}

# Off-domain cues — newsy spoza tooling / machining metalowego
_OFF_DOMAIN_CUES = (
    "watchmaking",
    "luxury watch",
    "f1 team",
    "formula 1",
    "outer space",
    "tanzanian",
    "a.i. agents",
    "ai agents",
    "predicts a.i",
    "composites startup booster",
    "hand tools maker",
    "welding debut",
    "grass cutting",
    "lawn",
    "lawnmower",
    "plastics technology",
    "plastic injection",
    "global tooling services",
    "berry introduces",
    "photos:",
    "celebrates 50 years",
)

# Silne sygnały branży skrawającej (metal) — nie „grass cutting tools”
_DOMAIN_CUES = (
    "cutting tools",
    "cemented carbide",
    "solid carbide",
    "carbide products",
    "carbide tools",
    "end mill",
    "indexable",
    "machining",
    "machine tool",
    "machine tools",
    "cnc",
    "hobbing",
    "gear shaping",
    "tokarka",
    "frezowan",
    "wiert",
    "skrawaj",
    "narzędzia skrawające",
    "lightweight machining",
    "mill-turn",
    "werkzeug",
    "zerspan",
    "imts",
    "imtex",
    "emo ",
)


def is_tooling_relevant(text: str) -> bool:
    """True jeśli tekst wygląda na branżę tooling / machining (metal)."""
    t = (text or "").lower()
    if any(cue in t for cue in _OFF_DOMAIN_CUES):
        return False
    if "grass" in t or "lawn" in t:
        return False
    # unikaj czystego „tooling” w plastics / generic services bez machining cue
    if any(cue in t for cue in _DOMAIN_CUES):
        return True
    # słabsze: „cutting tool” tylko bez grass
    if "cutting tool" in t or "carbide" in t:
        return True
    return False


def normalize_firm_name(name: str) -> str:
    """Normalizacja do porównań: lowercase, bez formy prawnej / znaków specjalnych."""
    text = (name or "").lower().strip()
    text = re.sub(r"[\"'`]", "", text)
    # Polish / DE / EN legal forms before stripping punctuation
    text = re.sub(r"\bsp\.?\s*z\.?\s*o\.?\s*o\.?\b", " ", text)
    text = re.sub(r"\bs\.?\s*k\.?\s*a\.?\b", " ", text)
    text = re.sub(
        r"\b(gmbh|ag|sa|s\.a\.|spa|s\.p\.a\.|ltd|limited|llc|inc|corp|corporation|"
        r"co\.|company|group|tools|tooling|cutting\s+tools?)\b",
        " ",
        text,
    )
    text = re.sub(r"[^a-z0-9ąćęłńóśźż\s-]", " ", text)
    # leftover after punctuation strip: "sp z o o"
    text = re.sub(r"\bsp\s+z\s+o\s+o\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


class KnownFirmsIndex:
    """Indeks znanych firm (seed / cache / Notion / competitors YAML)."""

    def __init__(self, firms: list[dict[str, Any]] | None = None) -> None:
        self.firms: list[dict[str, Any]] = list(firms or [])
        self._norm_names: set[str] = set()
        self._rebuild()

    def _rebuild(self) -> None:
        self._norm_names = set()
        for firm in self.firms:
            name = str(firm.get("company") or firm.get("name") or "").strip()
            if name:
                self._norm_names.add(normalize_firm_name(name))
            # aliasy z brandów katalogów
            brand = str(firm.get("brand") or "").strip()
            if brand:
                self._norm_names.add(normalize_firm_name(brand))

    def add_names(self, names: list[str]) -> None:
        for name in names:
            n = normalize_firm_name(name)
            if n:
                self._norm_names.add(n)
                if not any(
                    normalize_firm_name(str(f.get("company") or "")) == n for f in self.firms
                ):
                    self.firms.append({"company": name.strip()})

    @classmethod
    def load(
        cls,
        data_dir: Path | str | None = None,
        competitors: list[str] | None = None,
        seed_path: Path | str = "config/known_firms.seed.json",
    ) -> KnownFirmsIndex:
        data_dir = Path(data_dir or "data")
        cache = data_dir / "knowledge" / "known_firms.json"
        seed = Path(seed_path)
        firms: list[dict[str, Any]] = []
        if cache.exists():
            try:
                firms = json.loads(cache.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                firms = []
        if not firms and seed.exists():
            try:
                firms = json.loads(seed.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                firms = []
        idx = cls(firms)
        if competitors:
            idx.add_names(competitors)
        return idx

    def is_known(self, name: str, threshold: float = 0.88) -> bool:
        norm = normalize_firm_name(name)
        if not norm or len(norm) < 2:
            return True  # zbyt krótkie — nie traktuj jako nowej firmy
        if norm in self._norm_names:
            return True
        # substring / fuzzy
        for known in self._norm_names:
            if not known:
                continue
            if norm in known or known in norm:
                # unikaj zbyt krótkich substringów
                if min(len(norm), len(known)) >= 4:
                    return True
            if SequenceMatcher(None, norm, known).ratio() >= threshold:
                return True
        return False

    def match(self, name: str, threshold: float = 0.88) -> dict[str, Any] | None:
        norm = normalize_firm_name(name)
        best: tuple[float, dict[str, Any]] | None = None
        for firm in self.firms:
            company = str(firm.get("company") or "")
            kn = normalize_firm_name(company)
            if not kn:
                continue
            if norm == kn:
                return firm
            score = SequenceMatcher(None, norm, kn).ratio()
            if score >= threshold and (best is None or score > best[0]):
                best = (score, firm)
        return best[1] if best else None

    def names(self) -> list[str]:
        return sorted(
            {str(f.get("company")).strip() for f in self.firms if f.get("company")},
            key=str.lower,
        )


def is_plausible_firm_name(name: str) -> bool:
    """Odrzuć czasowniki / title-case śmieci / opisy produktów z headlines."""
    name = re.sub(r"\s+", " ", (name or "").strip(" ,.;:-/"))
    if len(name) < 2 or len(name) > 48:
        return False
    if re.search(r"\d{4}", name):
        return False
    if normalize_firm_name(name) in _PUBLISHER_NAMES:
        return False
    # opisy produktów / procesów, nie brand
    if re.search(
        r"\b(Machine|Machining|Hobbing|Shaping|Enabling|Acquisitions?|Products?|"
        r"Services?|Photos?|System|Gears?|Carbide)\b",
        name,
    ) and not re.search(r"\b(GmbH|AG|Ltd|LLC|Inc|Corp)\b", name):
        if len(name.split()) >= 2:
            return False
    tokens = [t for t in re.split(r"[\s/-]+", name) if t]
    if not tokens or len(tokens) > 3:
        return False
    lower = [t.lower().strip(".,") for t in tokens]
    legal = {"gmbh", "ag", "ltd", "llc", "inc", "corp", "sa", "spa", "co"}
    meaningful = [t for t in lower if t not in _STOP_TOKENS and t not in legal]
    if not meaningful:
        return False
    if lower[0] in _STOP_TOKENS:
        return False
    # ALLCAPS śmieci mediów / akronimy procesów (nie brand)
    junk_acronyms = {"photos", "fmt", "pes", "cnc", "cam", "imts", "imtex", "emo"}
    if len(tokens) == 1 and lower[0] in junk_acronyms:
        return False
    verbish = {
        "invests",
        "secures",
        "predicts",
        "promotes",
        "promote",
        "introduces",
        "launches",
        "unveils",
        "announces",
        "adds",
        "makes",
        "takes",
        "take",
        "will",
        "can",
        "revealed",
        "develops",
        "scales",
    }
    if any(t in verbish for t in lower[1:]):
        return False
    if len(tokens) == 1:
        tok = lower[0]
        if len(tok) < 3:
            return False
        if tok in {"photos", "news", "media", "key"}:
            return False
    return True


def extract_candidate_firm_names(text: str, max_names: int = 12) -> list[str]:
    """
    Heurystyczne wyciąganie nazw firm z tytułu / leadu newsa.
    Preferuj wzorce „Brand launches/introduces” i ALLCAPS brand; mniej Title Case.
    """
    if not text:
        return []
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # odetnij publisher „    PES Media” / trailing source
    cleaned = re.split(r"\s{2,}|  +", cleaned)[0].strip()

    candidates: list[str] = []

    patterns = [
        r"\b([A-Z][\w&./-]*(?:\s+[A-Z][\w&./-]*){0,2})\s+"
        r"(?:GmbH|AG|Ltd|LLC|Inc|S\.A\.|S\.p\.A\.|Corp)\b",
        # Brand before verb
        r"\b([A-Z][A-Za-z0-9&./-]{1,}(?:\s+[A-Z][A-Za-z0-9&./-]{1,}){0,2})\s+"
        r"(?:launches|unveils|introduces|announces|releases|presents|expands|"
        r"opens|acquires|partners|exhibits|showcases|develops)\b",
        # „Startup Toolpath …”
        r"\b(?:Startup|start-up)\s+([A-Z][A-Za-z0-9&./-]{2,})\b",
        # „Brand: a new brand”
        r"\b([A-Z][A-Za-z0-9&./-]{1,}(?:\s+[A-Z]{2,20})?)\s*:\s*a new brand\b",
        # ALLCAPS brand 3–20 (DIAEDGE, MSC) — nie PHOTOS
        r"\b([A-Z]{3,20}(?:-[A-Z0-9]{1,10})?)\b",
        r"\b(?:from|by)\s+([A-Z][\w&./-]*(?:\s+[A-Z][\w&./-]*){0,2})\b",
    ]
    for pat in patterns:
        for m in re.finditer(pat, cleaned, flags=re.IGNORECASE if "new brand" in pat else 0):
            candidates.append(m.group(1).strip(" ,.;:-"))

    # Title Case tylko 2 słowa na początku tytułu (przed dwukropkiem / em dash)
    head = re.split(r"[:—–|-]", cleaned, maxsplit=1)[0]
    for m in re.finditer(
        r"^\s*([A-Z][a-z0-9&./-]{2,}(?:\s+[A-Z][a-z0-9&./-]{2,}){0,1})\b", head
    ):
        candidates.append(m.group(1).strip())

    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        name = re.sub(r"\s+", " ", raw).strip(" ,.;:-/")
        if not is_plausible_firm_name(name):
            continue
        key = normalize_firm_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= max_names:
            break
    return out


def filter_new_firms(
    names: list[str],
    index: KnownFirmsIndex,
    *,
    evidence: str = "",
    url: str = "",
    require_domain: bool = False,
) -> list[dict[str, Any]]:
    """Zwraca tylko firmy spoza indeksu znanego (opcjonalnie: tylko tooling)."""
    if require_domain and evidence and not is_tooling_relevant(evidence):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in names:
        if not is_plausible_firm_name(name):
            continue
        if index.is_known(name):
            continue
        key = normalize_firm_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "company": name,
                "status": "new_candidate",
                "known": False,
                "evidence": (evidence or "")[:400],
                "url": url,
                "normalized": key,
            }
        )
    # Prefer longer brands; drop substrings (FMT ⊂ Walter FMT)
    rows.sort(key=lambda r: len(str(r.get("company") or "")), reverse=True)
    pruned: list[dict[str, Any]] = []
    kept_norms: list[str] = []
    for row in rows:
        n = str(row.get("normalized") or "")
        if any(n != k and n in k for k in kept_norms):
            continue
        pruned.append(row)
        kept_norms.append(n)
    return pruned
