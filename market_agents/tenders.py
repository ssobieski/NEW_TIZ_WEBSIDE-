"""
Przetargi i sygnały zakupowe sprzętu.

Parsuje:
- ogłoszenia przetargów (BZP / TED / portale) na maszyny / tooling
- informacje, że firma kupiła / zamierza kupić / planuje ogłosić przetarg
  na określony sprzęt (CNC, tokarka, frezarka, szlifierka, EDM, …)

Zapis: data/knowledge/tenders.json
Opcjonalnie: aktualizacja firm_profiles + zadania CRM follow-up.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from market_agents.firms import normalize_firm_name
from market_agents.prospects import _parse_money_to_eur

Intent = Literal[
    "announced_tender",  # ogłoszony przetarg
    "planned_tender",  # planuje ogłosić / przygotowuje przetarg
    "intends_to_buy",  # zamierza kupić / poszukuje
    "purchased",  # kupiła / zakupiła / odebrała
    "awarded",  # rozstrzygnięty / udzielono zamówienia
    "other",
]

TENDER_INTENTS: tuple[str, ...] = (
    "announced_tender",
    "planned_tender",
    "intends_to_buy",
    "purchased",
    "awarded",
    "other",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --- intent patterns (PL / EN / DE) -----------------------------------------

_INTENT_PATTERNS: list[tuple[str, Intent, float]] = [
    (
        r"(?i)\b(?:planuje|przygotowuje|w\s+przygotowaniu).{0,40}?"
        r"(?:przetarg|postępowanie|zakup)"
        r"|\bzamierza\s+ogłosić\s+(?:przetarg|postępowanie)",
        "planned_tender",
        0.95,
    ),
    (
        r"(?i)\b(?:plans?\s+to\s+(?:launch|announce|issue)\s+a?\s*tender|"
        r"upcoming\s+tender|tender\s+planned)\b",
        "planned_tender",
        0.9,
    ),
    (
        r"(?i)\b(?:ogłoszono|ogłasza|ogłoszenie)\s+(?:przetarg|postępowanie|zamówieni)",
        "announced_tender",
        0.9,
    ),
    (
        r"(?i)\b(?:przetarg|zapytanie\s+ofertowe|ZO|RFP|RFQ|ITT)\b.{0,40}?"
        r"(?:\bna\b|\bdla\b|dotycząc)",
        "announced_tender",
        0.85,
    ),
    (
        r"(?i)\b(?:tender|call\s+for\s+(?:tenders|bids)|invitation\s+to\s+tender)\b",
        "announced_tender",
        0.85,
    ),
    (
        r"(?i)\b(?:ausschreibung|vergabeverfahren)\b",
        "announced_tender",
        0.8,
    ),
    (
        r"(?i)\b(?:zamierza\s+kupić|planuje\s+zakup|poszukuje|szuka\s+(?:dostawc|maszyn)|"
        r"inwestycj[aą]\s+w|rozważa\s+zakup)\b",
        "intends_to_buy",
        0.8,
    ),
    (
        r"(?i)\b(?:intends?\s+to\s+buy|looking\s+to\s+(?:purchase|buy|acquire)|"
        r"plans?\s+to\s+(?:purchase|buy|invest\s+in)|seeking\s+(?:a\s+)?(?:supplier|machine))\b",
        "intends_to_buy",
        0.8,
    ),
    (
        r"(?i)\b(?:zakupi(?:ł|ła|ło|li)|kupi(?:ł|ła|ło|li)|naby(?:ł|ła|ło|li)|"
        r"odebra(?:ł|ła|ło|li)|zainstalowa(?:ł|ła|ło|li)|"
        r"wdroży(?:ł|ła|ło|li)|uruchomi(?:ł|ła|ło|li))\b",
        "purchased",
        0.85,
    ),
    (
        r"(?i)\b(?:purchased|bought|acquired|installed|commissioned|took\s+delivery)\b",
        "purchased",
        0.8,
    ),
    (
        r"(?i)\b(?:udzielono\s+zamówienia|wyłoniono\s+wykonawc|rozstrzygnięto\s+przetarg|"
        r"umowa\s+została\s+zawar|award(?:ed)?\s+(?:of\s+)?(?:contract|tender))\b",
        "awarded",
        0.85,
    ),
]
# --- equipment catalog (machining-focused) ----------------------------------

_EQUIPMENT_PATTERNS: list[tuple[str, str, str]] = [
    # (regex, kind, label)
    (r"(?i)\b(?:centrum\s+obr[oó]bcze|machining\s+center|bearbeitungszentrum|VMC|HMC)\b", "cnc_mill", "centrum obróbcze / machining center"),
    (r"(?i)\b(?:tokark\w*|turning\s+cent(?:er|re)|drehmaschine|CNC\s+lathe)\b", "cnc_lathe", "tokarka CNC / turning center"),
    (r"(?i)\b(?:frezark\w*|milling\s+machine|fräsmaschine)\b", "cnc_mill", "frezarka / milling machine"),
    (r"(?i)\b(?:szlifierk\w*|grinding\s+machine|schleifmaschine)\b", "grinder", "szlifierka / grinding machine"),
    (r"(?i)\b(?:elektrodrążark\w*|EDM|wire\s+EDM|senkerodieren)\b", "edm", "EDM / elektrodrążarka"),
    (r"(?i)\b(?:pi[łl][aą]\s+(?:ta[śs]mow|tarczow)|bandsaw|Kreissäge)\b", "saw", "piła / saw"),
    (r"(?i)\b(?:prasa\s+krawędziow|press\s+brake|Abkantpresse)\b", "press_brake", "prasa krawędziowa"),
    (r"(?i)\b(?:wycinark\w*\s+laserow|laser\s+cutter|Laserschneid)\b", "laser_cutter", "wycinarka laserowa"),
    (r"(?i)\b(?:wycinark\w*\s+plazmow|plasma\s+cutter)\b", "plasma_cutter", "wycinarka plazmowa"),
    (r"(?i)\b(?:wycinark\w*\s+wodn|waterjet)\b", "waterjet", "wycinarka wodna"),
    (r"(?i)\b(?:wiertark\w*|drilling\s+machine|Bohrmaschine)\b", "drill", "wiertarka"),
    (r"(?i)\b(?:5[\s\-]?osiow\w*|5[\s\-]?axis)\b", "5_axis", "obrabiarka 5-osiowa"),
    (r"(?i)\b(?:CNC|obrabiark\w*\s+CNC|CNC\s+machine)\b", "cnc_generic", "obrabiarka CNC"),
    (r"(?i)\b(?:robot\s+(?:przemysłowy|spawalniczy|paletyz)|industrial\s+robot)\b", "robot", "robot przemysłowy"),
    (r"(?i)\b(?:CMM|współrzędnościow\w*\s+maszyn\w*\s+pomiar|coordinate\s+measuring)\b", "cmm", "CMM / maszyna pomiarowa"),
    (r"(?i)\b(?:narzędzi\w*\s+skrawając|cutting\s+tools?|Zerspanungswerkzeuge)\b", "cutting_tools", "narzędzia skrawające"),
    (r"(?i)\b(?:oprawk\w*|toolholder|Werkzeughalter|HAIMER|shrink\s+fit)\b", "toolholders", "oprawki / toolholders"),
    (r"(?i)\b(?:magazyn\s+narzędzi|tool\s+magazine|Werkzeugmagazin)\b", "tool_magazine", "magazyn narzędzi"),
]

_COMPANY_RE = re.compile(
    r"(?i)(?:firma|spółka|zakład|przedsiębiorstwo|company|unternehmen)\s+"
    r"([A-ZĄĆĘŁŃÓŚŹŻ][\wĄĆĘŁŃÓŚŹŻąćęłńóśźż&.,\- ]{2,60}?)(?:"
    r"\s+(?:Sp\.\s*z\s*o\.?o\.?|S\.A\.|S\.K\.A\.|GmbH|AG|Ltd\.?|Inc\.?|AB|Oy)|"
    r"(?=\s+(?:ogłosi|zakupi|kupi|planuje|zamierza|poszukuje|nabyła|bought|purchased|announced))|"
    r"(?=[,.]))"
)

_COMPANY_PREFIX_RE = re.compile(
    r"(?i)^([A-ZĄĆĘŁŃÓŚŹŻ][\wĄĆĘŁŃÓŚŹŻąćęłńóśźż&.,\- ]{2,50}?"
    r"(?:\s+(?:Sp\.\s*z\s*o\.?o\.?|S\.A\.|GmbH|AG|Ltd\.?|Inc\.?|AB))?)"
    r"\s+(?:ogłosi|zakupi|kupi|planuje|zamierza|poszukuje|nabyła|bought|purchased|announced|intends)"
)

_VALUE_RE = re.compile(
    r"(?i)(?:wartość|value|budget|budżet|szacunkowa\s+wartość|estimated\s+value)"
    r".{0,30}?"
    r"((?:[€$£]|EUR|USD|PLN|SEK|CHF)?\s?\d[\d\s.,]*)\s*"
    r"(mrd|mld|bn|billion|mln|million|m\.?|tys\.?|k)?"
    r"(?:\s*(EUR|USD|PLN|SEK|CHF))?"
)

_DEADLINE_RE = re.compile(
    r"(?i)(?:termin\s+składania(?:\s+ofert)?|termin\s+otwarcia|deadline|"
    r"submission\s+deadline|oferty\s+do)\s*:?\s*"
    r"(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}|\d{4}-\d{2}-\d{2})"
)

_CPV_RE = re.compile(r"(?i)\bCPV\s*:?\s*(\d{8}(?:-\d)?)")

_REF_RE = re.compile(
    r"(?i)(?:nr\s+(?:przetargu|postępowania|referencyjny)|reference|BZP|TED)\s*[:=#]?\s*"
    r"([A-Z0-9][\w\-/.]{4,40})"
)


@dataclass
class TenderSignal:
    id: str
    company: str
    intent: Intent
    equipment: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""
    summary: str = ""
    value_eur: float | None = None
    value_text: str = ""
    deadline: str = ""
    cpv: list[str] = field(default_factory=list)
    reference: str = ""
    url: str = ""
    source: str = ""
    confidence: float = 0.5
    evidence: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TenderSignal:
        intent = str(raw.get("intent") or "other")
        if intent not in TENDER_INTENTS:
            intent = "other"
        return cls(
            id=str(raw.get("id") or _signal_id(raw.get("company"), raw.get("url"), raw.get("title"))),
            company=str(raw.get("company") or "").strip(),
            intent=intent,  # type: ignore[arg-type]
            equipment=list(raw.get("equipment") or []),
            title=str(raw.get("title") or "")[:300],
            summary=str(raw.get("summary") or "")[:2000],
            value_eur=(
                float(raw["value_eur"]) if raw.get("value_eur") is not None else None
            ),
            value_text=str(raw.get("value_text") or "")[:160],
            deadline=str(raw.get("deadline") or ""),
            cpv=list(raw.get("cpv") or []),
            reference=str(raw.get("reference") or "")[:80],
            url=str(raw.get("url") or ""),
            source=str(raw.get("source") or ""),
            confidence=float(raw.get("confidence") or 0.5),
            evidence=str(raw.get("evidence") or "")[:600],
            meta=dict(raw.get("meta") or {}),
            created_at=str(raw.get("created_at") or utc_now_iso()),
            updated_at=str(raw.get("updated_at") or utc_now_iso()),
        )


def _signal_id(company: Any, url: Any, title: Any) -> str:
    raw = f"{company or ''}|{url or ''}|{title or ''}".lower().strip()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]  # noqa: S324


def detect_intent(text: str) -> tuple[Intent, float, str]:
    best: tuple[Intent, float, str] = ("other", 0.0, "")
    for pattern, intent, score in _INTENT_PATTERNS:
        m = re.search(pattern, text or "")
        if m and score > best[1]:
            best = (intent, score, m.group(0)[:160])
    return best


def extract_equipment(text: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pattern, kind, label in _EQUIPMENT_PATTERNS:
        for m in re.finditer(pattern, text or ""):
            key = kind
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "kind": kind,
                    "label": label,
                    "match": m.group(0)[:80],
                }
            )
    return found


def extract_company_hint(text: str, fallback: str | None = None) -> str:
    m = _COMPANY_PREFIX_RE.search(text or "")
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip(" .,")
    m = _COMPANY_RE.search(text or "")
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip(" .,")
    # first line heuristic for short news leads
    first = (text or "").strip().split("\n", 1)[0]
    m2 = re.match(
        r"^([A-ZĄĆĘŁŃÓŚŹŻ][\wĄĆĘŁŃÓŚŹŻąćęłńóśźż&.,\- ]{2,50})"
        r"(?:\s+Sp\.\s*z\s*o\.?o\.?|\s+S\.A\.|\s+GmbH)?",
        first,
    )
    if m2 and len(m2.group(1).split()) <= 6:
        return m2.group(0).strip(" .,")
    if fallback and fallback.strip():
        return fallback.strip()
    return ""


def extract_value(text: str) -> tuple[float | None, str]:
    m = _VALUE_RE.search(text or "")
    if not m:
        return None, ""
    amount = _parse_money_to_eur(m.group(1), m.group(2), m.group(3))
    return amount, m.group(0).strip()[:160]


def extract_deadline(text: str) -> str:
    m = _DEADLINE_RE.search(text or "")
    return m.group(1) if m else ""


def extract_cpv(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(1) for m in _CPV_RE.finditer(text or "")))[:8]


def extract_reference(text: str) -> str:
    m = _REF_RE.search(text or "")
    return m.group(1) if m else ""


def parse_tender_text(
    text: str,
    *,
    company: str | None = None,
    title: str = "",
    url: str = "",
    source: str = "",
) -> dict[str, Any]:
    """Heuristic parse — no LLM required.

    Multi-paragraph texts: score each block and keep the best match for
    the requested company (or the highest-confidence tender signal).
    """
    blob = f"{title}\n{text or ''}".strip()
    blocks = [b.strip() for b in re.split(r"\n\s*\n", blob) if b.strip()]
    if len(blocks) <= 1:
        return _parse_tender_block(
            blob, company=company, title=title, url=url, source=source
        )

    candidates = [
        _parse_tender_block(b, company=company, title=title, url=url, source=source)
        for b in blocks
    ]
    company_norm = normalize_firm_name(company or "")
    if company_norm:
        for idx, c in enumerate(candidates):
            if company_norm in normalize_firm_name(c.get("company") or ""):
                chosen = dict(c)
                chosen["related_signals"] = [
                    {
                        "company": o.get("company"),
                        "intent": o.get("intent"),
                        "equipment": [e.get("kind") for e in (o.get("equipment") or [])[:4]],
                        "confidence": o.get("confidence"),
                    }
                    for j, o in enumerate(candidates)
                    if j != idx and o.get("tender_relevant")
                ][:5]
                return chosen
    candidates.sort(
        key=lambda c: (
            1 if c.get("tender_relevant") else 0,
            float(c.get("confidence") or 0),
            1 if c.get("intent") != "other" else 0,
        ),
        reverse=True,
    )
    best = candidates[0]
    # Keep sibling signals for operators / agents
    best["related_signals"] = [
        {
            "company": c.get("company"),
            "intent": c.get("intent"),
            "equipment": [e.get("kind") for e in (c.get("equipment") or [])[:4]],
            "confidence": c.get("confidence"),
        }
        for c in candidates
        if c is not best and c.get("tender_relevant")
    ][:5]
    return best


def _parse_tender_block(
    blob: str,
    *,
    company: str | None = None,
    title: str = "",
    url: str = "",
    source: str = "",
) -> dict[str, Any]:
    intent, conf, evidence = detect_intent(blob)
    equipment = extract_equipment(blob)
    extracted = extract_company_hint(blob, fallback=None)
    # Explicit company always wins; keep extract in meta when it disagrees.
    extracted_company = None
    if company and company.strip():
        company_name = company.strip()
        if extracted and normalize_firm_name(extracted) not in {
            normalize_firm_name(company),
            "",
        } and normalize_firm_name(company) not in normalize_firm_name(extracted):
            extracted_company = extracted
    else:
        company_name = extracted
    value_eur, value_text = extract_value(blob)
    deadline = extract_deadline(blob)
    cpv = extract_cpv(blob)
    reference = extract_reference(blob)

    # Soft-gate generic "bought/installed" without machining/equipment context
    _machining_ctx = bool(
        equipment
        or cpv
        or re.search(
            r"(?i)\b(?:cnc|obrabiark|maszyn\w*|machine\s+tool|tokark|frezar|"
            r"przetarg|tender|machining|cutting\s+tool|oprawk|toolholder)\b",
            blob,
        )
    )
    if intent == "purchased" and not _machining_ctx:
        intent = "other"
        conf = min(conf, 0.35)
        evidence = (evidence or "") + " [no machining context]"

    if equipment:
        conf = min(1.0, conf + 0.05)
    if company_name:
        conf = min(1.0, conf + 0.05)
    if intent == "other" and equipment and re.search(
        r"(?i)\b(?:przetarg|tender|zakup|purchase|buy|inwestycj)\b", blob
    ):
        intent = "intends_to_buy"
        conf = max(conf, 0.55)
        evidence = evidence or "equipment + purchase/tender context"

    summary = (title or blob[:240]).strip()
    out: dict[str, Any] = {
        "ok": True,
        "company": company_name,
        "intent": intent,
        "confidence": round(conf, 3),
        "equipment": equipment,
        "value_eur": value_eur,
        "value_text": value_text,
        "deadline": deadline,
        "cpv": cpv,
        "reference": reference,
        "title": (title or summary)[:300],
        "summary": summary[:2000],
        "evidence": evidence,
        "url": url,
        "source": source,
        # Intent required — equipment alone is not a tender/purchase signal
        "tender_relevant": intent != "other",
    }
    if extracted_company:
        out["extracted_company"] = extracted_company
    return out


def parse_tender_html(
    html: str,
    *,
    company: str | None = None,
    title: str = "",
    url: str = "",
    source: str = "",
) -> dict[str, Any]:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", html or "")
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return parse_tender_text(
        text, company=company, title=title, url=url, source=source
    )


class TenderRegistry:
    def __init__(self, signals: list[TenderSignal] | None = None) -> None:
        self.signals: list[TenderSignal] = list(signals or [])

    @classmethod
    def load(cls, data_dir: Path | str | None = None) -> TenderRegistry:
        path = Path(data_dir or "data") / "knowledge" / "tenders.json"
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return cls()
        rows = raw.get("signals") if isinstance(raw, dict) else raw
        out: list[TenderSignal] = []
        for r in rows or []:
            if isinstance(r, dict):
                try:
                    out.append(TenderSignal.from_dict(r))
                except Exception:  # noqa: BLE001
                    continue
        return cls(out)

    def save(self, data_dir: Path | str | None = None) -> Path:
        data_dir = Path(data_dir or "data")
        path = data_dir / "knowledge" / "tenders.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "count": len(self.signals),
            "updated_at": utc_now_iso(),
            "signals": [s.to_dict() for s in self.signals],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def upsert(self, signal: TenderSignal) -> TenderSignal:
        key = signal.id
        for i, existing in enumerate(self.signals):
            if existing.id == key or (
                normalize_firm_name(existing.company) == normalize_firm_name(signal.company)
                and existing.url
                and existing.url == signal.url
            ):
                # Field-wise merge: never wipe non-empty fields with empties
                base = existing.to_dict()
                incoming = signal.to_dict()
                merged_raw: dict[str, Any] = dict(base)
                for field_name, new_val in incoming.items():
                    if field_name in {"id", "created_at"}:
                        continue
                    if new_val is None or new_val == "" or new_val == []:
                        continue
                    if field_name == "confidence":
                        merged_raw[field_name] = max(
                            float(base.get("confidence") or 0),
                            float(new_val or 0),
                        )
                        continue
                    if field_name == "equipment":
                        continue  # merged below
                    merged_raw[field_name] = new_val
                merged = TenderSignal.from_dict(merged_raw)
                merged.id = existing.id
                merged.created_at = existing.created_at
                merged.updated_at = utc_now_iso()
                # Prefer richer equipment set
                seen = {e.get("kind") for e in (existing.equipment or [])}
                eq = list(existing.equipment or [])
                for e in signal.equipment or []:
                    if e.get("kind") not in seen:
                        eq.append(e)
                        seen.add(e.get("kind"))
                merged.equipment = eq
                self.signals[i] = merged
                return merged
        self.signals.append(signal)
        return signal

    def list(
        self,
        *,
        q: str | None = None,
        intent: str | None = None,
        limit: int = 50,
    ) -> list[TenderSignal]:
        rows = list(self.signals)
        if intent:
            rows = [r for r in rows if r.intent == intent]
        if q:
            nq = normalize_firm_name(q)
            rows = [
                r
                for r in rows
                if nq in normalize_firm_name(r.company)
                or nq in normalize_firm_name(r.title)
                or any(nq in normalize_firm_name(str(e.get("label") or "")) for e in r.equipment)
            ]
        rows.sort(key=lambda r: (r.updated_at, r.confidence), reverse=True)
        return rows[:limit]

    def scoreboard(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = sorted(self.signals, key=lambda r: (r.confidence, r.updated_at), reverse=True)
        out = []
        for r in rows[:limit]:
            out.append(
                {
                    "company": r.company,
                    "intent": r.intent,
                    "equipment": [e.get("kind") for e in (r.equipment or [])[:6]],
                    "value_eur": r.value_eur,
                    "deadline": r.deadline,
                    "confidence": r.confidence,
                    "title": r.title[:120],
                    "url": r.url,
                    "id": r.id,
                }
            )
        return out


def ingest_tender_analysis(
    analysis: dict[str, Any],
    data_dir: Path | str,
    *,
    create_crm: bool = True,
    update_profile: bool = True,
) -> dict[str, Any]:
    """Persist parsed signal; optionally touch firm profile + CRM."""
    if not analysis.get("tender_relevant"):
        return {"ok": False, "error": "not tender-relevant", "analysis": analysis}

    company = str(analysis.get("company") or "").strip()
    if not company:
        return {"ok": False, "error": "company required", "analysis": analysis}

    signal = TenderSignal(
        id=_signal_id(company, analysis.get("url"), analysis.get("title")),
        company=company,
        intent=analysis.get("intent") or "other",  # type: ignore[arg-type]
        equipment=list(analysis.get("equipment") or []),
        title=str(analysis.get("title") or ""),
        summary=str(analysis.get("summary") or ""),
        value_eur=analysis.get("value_eur"),
        value_text=str(analysis.get("value_text") or ""),
        deadline=str(analysis.get("deadline") or ""),
        cpv=list(analysis.get("cpv") or []),
        reference=str(analysis.get("reference") or ""),
        url=str(analysis.get("url") or ""),
        source=str(analysis.get("source") or "parse"),
        confidence=float(analysis.get("confidence") or 0.5),
        evidence=str(analysis.get("evidence") or ""),
    )
    reg = TenderRegistry.load(data_dir)
    saved = reg.upsert(signal)
    path = reg.save(data_dir)

    profile_meta = None
    if update_profile:
        profile_meta = _attach_to_profile(saved, data_dir)

    crm_meta = None
    if create_crm and saved.intent in {
        "announced_tender",
        "planned_tender",
        "intends_to_buy",
        "awarded",
        "purchased",
    }:
        crm_meta = _maybe_create_crm(saved, data_dir)

    return {
        "ok": True,
        "signal": saved.to_dict(),
        "path": str(path),
        "profile": profile_meta,
        "crm": crm_meta,
    }


def _attach_to_profile(signal: TenderSignal, data_dir: Path | str) -> dict[str, Any]:
    from market_agents.firm_profiles import FirmProfile, FirmProfileRegistry, firm_id_from_name

    reg = FirmProfileRegistry.load(data_dir)
    p = reg.get(signal.company)
    if p is None:
        p = FirmProfile(
            id=firm_id_from_name(signal.company),
            company=signal.company,
            roles=["prospect"],
            origin="tender_signal",
            sources=["tenders"],
        )
    if "prospect" not in (p.roles or []):
        p.roles = list(dict.fromkeys([*(p.roles or []), "prospect"]))
    tender_note = {
        "intent": signal.intent,
        "equipment": [e.get("kind") for e in signal.equipment[:8]],
        "value_eur": signal.value_eur,
        "deadline": signal.deadline,
        "url": signal.url,
        "id": signal.id,
        "updated_at": utc_now_iso(),
    }
    prospect = dict(p.prospect or {})
    tenders = list(prospect.get("tenders") or [])
    tenders = [t for t in tenders if t.get("id") != signal.id]
    tenders.insert(0, tender_note)
    prospect["tenders"] = tenders[:20]
    prospect["latest_tender_intent"] = signal.intent
    p.prospect = prospect
    if "tenders" not in (p.sources or []):
        p.sources = list(dict.fromkeys([*(p.sources or []), "tenders"]))
    eq_labels = ", ".join(e.get("label") or e.get("kind") or "" for e in signal.equipment[:3])
    snip = f"[{signal.intent}] {eq_labels or signal.title}".strip()
    note_tag = f"[tender:{signal.id}]"
    tagged = f"{note_tag} {snip}"
    lines = [ln for ln in (p.notes or "").split("\n") if ln.strip() and note_tag not in ln and snip not in ln]
    lines.append(tagged)
    p.notes = "\n".join(lines).strip()[:4000]
    reg.upsert(p)
    reg.save(data_dir)
    return {"company": p.company, "id": p.id, "tenders": len(tenders)}


def _maybe_create_crm(signal: TenderSignal, data_dir: Path | str) -> dict[str, Any]:
    from market_agents.crm_tasks import CrmTaskStore

    store = CrmTaskStore.load(data_dir)
    eq = ", ".join(e.get("label") or e.get("kind") or "" for e in signal.equipment[:3]) or "sprzęt"
    intent_label = {
        "announced_tender": "Przetarg ogłoszony",
        "planned_tender": "Planowany przetarg",
        "intends_to_buy": "Zamiar zakupu",
        "purchased": "Zakup zrealizowany",
        "awarded": "Przetarg rozstrzygnięty",
    }.get(signal.intent, "Sygnał zakupowy")
    title = f"{intent_label}: {eq}"[:160]
    reason = (
        f"{signal.intent} — {eq}"
        + (f"; wartość≈{int(signal.value_eur)} EUR" if signal.value_eur else "")
        + (f"; termin={signal.deadline}" if signal.deadline else "")
    )[:800]
    priority = (
        "hot"
        if signal.intent in {"announced_tender", "planned_tender"}
        else ("high" if signal.intent == "intends_to_buy" else "medium")
    )
    score = {
        "announced_tender": 85.0,
        "planned_tender": 80.0,
        "intends_to_buy": 75.0,
        "awarded": 60.0,
        "purchased": 55.0,
    }.get(signal.intent, 50.0)
    task, created = store.create(
        company=signal.company,
        title=title,
        reason=reason,
        opportunity_score=score,
        priority=priority,  # type: ignore[arg-type]
        source="tender",
        dedupe_by_company=True,
        meta={"tender_id": signal.id, "intent": signal.intent, "url": signal.url},
    )
    store.save(data_dir)
    return {
        "created": created,
        "task_id": task.id,
        "title": task.title,
        "priority": task.priority,
        "sources": (task.meta or {}).get("sources") or [task.source],
    }
