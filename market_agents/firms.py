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
    "new",
    "news",
    "launches",
    "launch",
    "unveils",
    "introduces",
    "announces",
    "releases",
    "presents",
    "expands",
    "opens",
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
    "market",
    "global",
    "group",
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
    "amb",
    "emo",
    "imts",
    "jimtof",
}


def normalize_firm_name(name: str) -> str:
    """Normalizacja do porównań: lowercase, bez formy prawnej / znaków specjalnych."""
    text = (name or "").lower().strip()
    text = re.sub(r"[\"'`]", "", text)
    text = re.sub(
        r"\b(gmbh|ag|sa|s\.a\.|spa|s\.p\.a\.|ltd|limited|llc|inc|corp|corporation|"
        r"co\.|company|group|tools|tooling|cutting\s+tools?)\b",
        " ",
        text,
    )
    text = re.sub(r"[^a-z0-9ąćęłńóśźż\s-]", " ", text)
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


def extract_candidate_firm_names(text: str, max_names: int = 12) -> list[str]:
    """
    Heurystyczne wyciąganie nazw firm z tytułu / leadu newsa.
    Nie jest NER-em LLM — szybki filtr przed agentem.
    """
    if not text:
        return []
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    candidates: list[str] = []

    # 1) Wzorce: "X launches/unveils/introduces..."
    patterns = [
        r"\b([A-Z][\w&./-]*(?:\s+[A-Z][\w&./-]*){0,3})\s+"
        r"(?:launches|unveils|introduces|announces|releases|presents|expands|"
        r"opens|acquires|partners|exhibits|showcases)\b",
        r"\b(?:from|by|at)\s+([A-Z][\w&./-]*(?:\s+[A-Z][\w&./-]*){0,3})\b",
        r"\b([A-Z][\w&./-]*(?:\s+[A-Z][\w&./-]*){0,2})\s+"
        r"(?:GmbH|AG|Ltd|LLC|Inc|S\.A\.|S\.p\.A\.|Corp)\b",
        # ALLCAPS brand tokens 2–20 chars
        r"\b([A-Z]{2,20}(?:-[A-Z0-9]{1,10})?)\b",
    ]
    for pat in patterns:
        for m in re.finditer(pat, cleaned):
            candidates.append(m.group(1).strip(" ,.;:-"))

    # 2) Ciągi Title Case (2–4 słowa)
    for m in re.finditer(
        r"\b([A-Z][a-z0-9&./-]{1,}(?:\s+[A-Z][a-z0-9&./-]{1,}){0,3})\b", cleaned
    ):
        candidates.append(m.group(1).strip())

    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        name = re.sub(r"\s+", " ", raw).strip(" ,.;:-/")
        if len(name) < 2 or len(name) > 60:
            continue
        tokens = [t.lower() for t in re.split(r"[\s/-]+", name) if t]
        if not tokens:
            continue
        if all(t in _STOP_TOKENS for t in tokens):
            continue
        # odrzuć jeśli większość to stopwords
        if sum(1 for t in tokens if t in _STOP_TOKENS) >= max(1, len(tokens) - 0):
            if len(tokens) == 1 and tokens[0] in _STOP_TOKENS:
                continue
        # odrzuć czysto pospolite frazy
        if name.lower() in _STOP_TOKENS:
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
) -> list[dict[str, Any]]:
    """Zwraca tylko firmy spoza indeksu znanego."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in names:
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
    return rows
