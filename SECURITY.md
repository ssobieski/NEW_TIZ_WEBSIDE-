# Cybersecurity — lokalni agenci TIZ

System jest przeznaczony dla **zaufanego operatora lokalnego** (Dell/VPS), nie multi-tenant SaaS.
Poniżej mapa zagrożeń i wdrożone mechanizmy.

## Zagrożenia (skrót)

| ID | Ryzyko | Mechanizm |
|----|--------|-----------|
| T-01 | SSRF (localhost / RFC1918 / cloud metadata) | `validate_fetch_url` w `polite_http` + redirect check |
| T-02 | Prompt injection → tool abuse | Tool policy (`allow_*_tools`, `strict_tool_mode`) |
| T-03 | Hijack fleet knowledge pack | Path tylko pod `fleet/outbox`, allowlista plików, brak symlinków |
| T-05 | R2 key escape | `enforce_r2_key_prefix` |
| T-07 | Wyciek sekretów w traces | `redact_secrets` w traces / tool errors / R2 excerpts |
| T-08 | Poisoning parse rules | `auto_promote_host_skills: false` (default) |

## Konfiguracja (`agents.security` + `agents.crawl`)

```yaml
agents:
  security:
    block_private_networks: true
    allow_write_tools: true
    allow_notion_tools: true
    allow_r2_tools: true
    strict_tool_mode: false   # true = tylko SAFE+FETCH
    redact_traces: true
    fleet_allowlist_only: true
    enforce_r2_prefix: true
    allowed_hosts: []         # niepuste = tylko te hosty
  crawl:
    block_private_networks: true
    resolve_dns_for_ssrf: true
    max_redirects: 3
    max_response_bytes: 15000000
  agentic:
    auto_promote_host_skills: false
```

```bash
python -m market_agents security
python -m market_agents doctor
```

## Hardening rekomendowany operacyjnie

1. vLLM: bind `127.0.0.1` / VPN; unikaj `--trust-remote-code` jeśli niepotrzebne.
2. Notion integration: minimalny share (tylko bazy TIZ).
3. R2 IAM: tylko prefix `monitoring/` read.
4. Fleet share (NFS/rsync): tylko zapis z centrali do `outbox/`, workery read-only.
5. Na produkcji agentic: `strict_tool_mode: true` + przegląd write tools.
6. Nie commituj `.env` / traces z treścią Notion.

## Tool: `security_status`

Agent może sprawdzić aktywną politykę bez ujawniania sekretów.
