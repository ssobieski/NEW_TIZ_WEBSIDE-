"""
Cybersecurity controls for local market agents.

Threats mitigated (see README / SECURITY):
- SSRF via URL fetch (block private/metadata hosts)
- Secret leakage in traces/memory/tool errors
- Path escape (exports, fleet packs)
- Prompt-injection blast radius (tool policy)
- Fleet knowledge pack hijack (path + allowlist + no symlinks)
- R2 key prefix escape
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

# --- SSRF -----------------------------------------------------------------

_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
}

_METADATA_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
}


class SecurityError(RuntimeError):
    """Odrzucenie ze względów bezpieczeństwa."""


def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def host_is_blocked(host: str, *, resolve_dns: bool = True) -> tuple[bool, str]:
    """Zwróć (blocked, reason)."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return True, "empty host"
    if host in _BLOCKED_HOSTNAMES or host in _METADATA_HOSTS:
        return True, f"blocked hostname: {host}"
    # literal IP?
    try:
        ip = ipaddress.ip_address(host)
        if _is_private_ip(ip) or str(ip) in _METADATA_HOSTS:
            return True, f"private/metadata IP: {ip}"
        return False, ""
    except ValueError:
        pass
    if not resolve_dns:
        return False, ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # nie blokuj tylko dlatego że DNS fail — fetch i tak padnie;
        # ale unikamy resolve→private via later check if we can
        return False, ""
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_private_ip(ip) or str(ip) in _METADATA_HOSTS:
            return True, f"DNS resolves to private/metadata IP: {host}→{ip}"
    return False, ""


def validate_fetch_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    allow_private: bool = False,
    resolve_dns: bool = True,
    max_url_length: int = 2048,
) -> str:
    """
    Waliduj URL przed fetch (SSRF guard).
    Zwraca znormalizowany URL albo rzuca SecurityError.
    """
    url = (url or "").strip()
    if not url:
        raise SecurityError("empty URL")
    if len(url) > max_url_length:
        raise SecurityError("URL too long")
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise SecurityError(f"scheme not allowed: {scheme or '(none)'}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise SecurityError("URL missing host")
    if parsed.username or parsed.password:
        raise SecurityError("URL userinfo not allowed")

    allow = {h.lower().lstrip(".") for h in (allowed_hosts or []) if h}
    if allow:
        ok = host in allow or any(host.endswith("." + h) for h in allow)
        if not ok:
            raise SecurityError(f"host not in allowlist: {host}")

    if not allow_private:
        blocked, reason = host_is_blocked(host, resolve_dns=resolve_dns)
        if blocked:
            raise SecurityError(f"SSRF blocked: {reason}")

    return url


# --- Secrets redaction ----------------------------------------------------

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s\"',]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(secret[_-]?access[_-]?key\s*[:=]\s*)[^\s\"',]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(aws_secret_access_key\s*[:=]\s*)[^\s\"',]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(notion[_-]?token\s*[:=]\s*)(secret_[A-Za-z0-9]+)"), r"\1[REDACTED]"),
    (re.compile(r"\bsecret_[A-Za-z0-9]{20,}\b"), "[REDACTED_NOTION_TOKEN]"),
    (re.compile(r"(?i)(CF_R2_SECRET_ACCESS_KEY|NOTION_TOKEN|OPENAI_API_KEY)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
]


def redact_secrets(text: str, *, max_chars: int | None = None) -> str:
    out = text or ""
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    if max_chars is not None and len(out) > max_chars:
        out = out[: max(0, max_chars)] + "…[truncated]"
    return out


def safe_error(exc: BaseException) -> str:
    return redact_secrets(str(exc), max_chars=400)


# --- Path confinement -----------------------------------------------------

def ensure_under_root(path: Path | str, root: Path | str, *, label: str = "path") -> Path:
    """Wymuś że path jest pod root (po resolve)."""
    target = Path(path).expanduser().resolve()
    base = Path(root).expanduser().resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise SecurityError(f"{label} escapes root {base}: {target}") from exc
    return target


def safe_basename(name: str) -> str:
    name = (name or "").replace("\\", "/").split("/")[-1]
    if not name or name in {".", ".."} or ".." in name:
        raise SecurityError(f"unsafe filename: {name!r}")
    return name


# --- Tool policy (prompt-injection blast radius) --------------------------

# Narzędzia tylko-do-odczytu / lokalne — zawsze dozwolone
SAFE_TOOLS = frozenset(
    {
        "list_candidates",
        "crawl_status",
        "search_memory",
        "score_item",
        "list_catalog_sources",
        "list_media_sources",
        "list_social_sources",
        "list_literature_sources",
        "list_literature",
        "list_available_pricelists",
        "list_product_tech",
        "list_known_firms",
        "get_firm_presentation",
        "check_firm_known",
        "list_relations",
        "firm_neighborhood",
        "list_firm_profiles",
        "get_firm_profile",
        "score_firm_profile",
        "firm_scoreboard",
        "list_prospects",
        "get_prospect_profile",
        "prospect_scoreboard",
        "estimate_tooling_budget",
        "list_parse_rules",
        "list_parsing_skills",
        "match_parsing_skills",
        "get_domain_context",
        "list_ontology",
        "ontology_neighborhood",
        "find_ontology_path",
        "security_status",
        "governance_status",
    }
)

# Fetch / parse zewnętrzny — wymaga SSRF guard (i. tak)
FETCH_TOOLS = frozenset(
    {
        "fetch_and_parse",
        "batch_parse",
        "discover_catalog_assets",
        "fetch_pdf_text",
        "discover_pricelists",
        "discover_product_tech",
        "extract_product_tech_schema",
        "discover_media_links",
        "parse_media_page",
        "extract_fair_exhibitors",
        "discover_social_posts",
        "parse_social_post",
        "discover_literature",
        "discover_new_firms",
        "discover_relations",
        "extract_domain_context",
        "enrich_firm_profile",
        "analyze_prospect",
    }
)

# Zapis do knowledge / reguł — domyślnie wyłączone w strict mode
WRITE_TOOLS = frozenset(
    {
        "remember",
        "extract_market_intel",
        "register_pricelist",
        "register_product_tech",
        "register_literature",
        "add_relation",
        "upsert_parse_rule",
        "rate_parse",
        "learn_parsing_skill",
        "improve_parsing_skill",
        "rate_parsing_skill",
        "promote_host_skill",
        "add_ontology_edge",
        "build_ontology",
        "sync_notion_literature",
        "build_firm_profiles",
        "upsert_firm_profile",
        "register_social_mention",
        "ingest_prospect_seeds",
    }
)

# Notion / R2 — wrażliwe; wyłączalne
SENSITIVE_CLOUD_TOOLS = frozenset(
    {
        "search_notion",
        "fetch_notion_page",
        "search_r2",
        "fetch_r2_object",
    }
)


@dataclass
class SecurityPolicy:
    """Polityka bezpieczeństwa runtime."""

    block_private_networks: bool = True
    resolve_dns: bool = True
    allowed_hosts: list[str] = field(default_factory=list)
    max_redirects: int = 3
    max_response_bytes: int = 15_000_000
    # tool policy
    allow_write_tools: bool = True
    allow_notion_tools: bool = True
    allow_r2_tools: bool = True
    strict_tool_mode: bool = False  # True = tylko SAFE + FETCH (+ opcjonalnie write)
    # redaction
    redact_traces: bool = True
    trace_tool_result_max_chars: int = 4000
    # fleet
    fleet_allowlist_only: bool = True
    # R2
    enforce_r2_prefix: bool = True

    def tool_allowed(self, name: str) -> tuple[bool, str]:
        if name in SAFE_TOOLS or name in FETCH_TOOLS:
            return True, ""
        if name in SENSITIVE_CLOUD_TOOLS:
            if name.startswith("search_notion") or name.startswith("fetch_notion"):
                if not self.allow_notion_tools:
                    return False, "Notion tools disabled by security policy"
            if "r2" in name and not self.allow_r2_tools:
                return False, "R2 tools disabled by security policy"
            if self.strict_tool_mode and name in SENSITIVE_CLOUD_TOOLS:
                if not (self.allow_notion_tools or self.allow_r2_tools):
                    return False, "cloud tools disabled in strict mode"
            return True, ""
        if name in WRITE_TOOLS:
            if not self.allow_write_tools:
                return False, "write tools disabled by security policy"
            if self.strict_tool_mode and not self.allow_write_tools:
                return False, "write tools blocked in strict mode"
            return True, ""
        # unknown tools: allow unless strict
        if self.strict_tool_mode:
            return False, f"tool not allowed in strict mode: {name}"
        return True, ""


def default_security_policy() -> SecurityPolicy:
    return SecurityPolicy()


# --- Fleet pack validation ------------------------------------------------

FLEET_ALLOWED_KNOWLEDGE = frozenset(
    {
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
        "manifest.json",
        "fleet_pulled.json",
    }
)


def resolve_fleet_pack_path(
    claimed_path: str | Path | None,
    *,
    sync_root: Path,
    pack_id: str | None = None,
) -> Path | None:
    """
    Odrzuć absolute path spoza sync_root/outbox.
    Preferuj outbox/<pack_id>.
    """
    sync_root = Path(sync_root).resolve()
    outbox = (sync_root / "outbox" / "knowledge").resolve()
    if pack_id:
        candidate = outbox / safe_basename(pack_id)
        if candidate.is_dir():
            return candidate
    if not claimed_path:
        return None
    raw = Path(str(claimed_path))
    # tylko względne względem sync_root albo już pod outbox
    try:
        if raw.is_absolute():
            resolved = raw.resolve()
            resolved.relative_to(outbox)
            if resolved.is_dir() and not resolved.is_symlink():
                return resolved
            return None
        resolved = (outbox / raw).resolve()
        resolved.relative_to(outbox)
        if resolved.is_dir() and not resolved.is_symlink():
            return resolved
    except (ValueError, OSError, SecurityError):
        return None
    return None


def iter_safe_pack_files(
    pack_dir: Path,
    *,
    allowlist: Iterable[str] | None = None,
) -> list[Path]:
    """Pliki JSON z packa: bez symlinków, tylko basename z allowlisty."""
    allowed = set(allowlist) if allowlist is not None else set(FLEET_ALLOWED_KNOWLEDGE)
    out: list[Path] = []
    pack_dir = Path(pack_dir)
    if not pack_dir.is_dir() or pack_dir.is_symlink():
        raise SecurityError(f"unsafe pack dir: {pack_dir}")
    for src in pack_dir.iterdir():
        if src.name == "manifest.json":
            continue
        if not src.is_file() or src.is_symlink():
            continue
        if src.suffix != ".json":
            continue
        name = safe_basename(src.name)
        if allowed and name not in allowed and name not in {"items_tail.jsonl"}:
            # items_tail nie jest knowledge — skip przy pull knowledge
            if name.endswith(".json") and name not in allowed:
                continue
        if name not in allowed:
            continue
        out.append(src)
    return out


def enforce_r2_key_prefix(key: str, prefix: str) -> str:
    key = (key or "").lstrip("/")
    prefix = (prefix or "").lstrip("/")
    if not prefix:
        return key
    if not key.startswith(prefix):
        raise SecurityError(f"R2 key outside configured prefix: {key}")
    if ".." in key.split("/"):
        raise SecurityError("R2 key path traversal")
    return key


def security_status(policy: SecurityPolicy) -> dict[str, Any]:
    return {
        "ok": True,
        "block_private_networks": policy.block_private_networks,
        "allowed_hosts_count": len(policy.allowed_hosts),
        "max_redirects": policy.max_redirects,
        "allow_write_tools": policy.allow_write_tools,
        "allow_notion_tools": policy.allow_notion_tools,
        "allow_r2_tools": policy.allow_r2_tools,
        "strict_tool_mode": policy.strict_tool_mode,
        "redact_traces": policy.redact_traces,
        "fleet_allowlist_only": policy.fleet_allowlist_only,
        "enforce_r2_prefix": policy.enforce_r2_prefix,
        "controls": [
            "SSRF block private/metadata",
            "secret redaction",
            "tool policy",
            "fleet pack path+allowlist",
            "R2 prefix enforcement",
        ],
    }
