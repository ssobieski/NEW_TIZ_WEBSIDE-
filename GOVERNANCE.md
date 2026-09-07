# AI Governance — Policy-as-Code

Warstwa **AI governance** egzekwuje deklaratywne polityki YAML przed akcjami agenta
(tool call, fetch, knowledge write, fleet publish). Complementuje `SECURITY.md`
(SSRF, redaction, path confinement).

## Architektura

```
LLM / CLI  →  ToolRegistry.call()
                 ├─ GovernanceEngine.authorize_tool()   # policy-as-code
                 ├─ SecurityPolicy.tool_allowed()       # cybersecurity
                 └─ handler(...)
```

- Strategia decyzji: **deny-overrides** (`deny` > `require_approval` > `redact` > `monitor` > `allow`)
- Tryby: `enforce` (blokuje) | `monitor` (tylko audyt)
- Audyt: `data/governance/audit.jsonl` (z redakcją sekretów)

## Policy pack

Domyślny: `config/governance/tiz.policy.yaml`

```yaml
version: 1
id: tiz-market-agents
defaults: { effect: allow, audit: true }
rules:
  - id: worker-deny-write-tools
    priority: 260
    effect: deny
    match: { action: tool_call, role: worker, tool_tier: write }
    reason: "VPS worker nie mutuje knowledge"
```

### Match (wybrane pola)

| Pole | Znaczenie |
|------|-----------|
| `action` | `tool_call` \| `fetch` \| `knowledge_write` \| `fleet_publish` \| `llm_output` |
| `tool` / `tool_in` / `tool_not_in` | nazwa narzędzia |
| `tool_tier` | `safe` \| `fetch` \| `write` \| `cloud` \| `unknown` |
| `role` | `central` \| `worker` \| `both` |
| `url_scheme` / `url_host_suffix` | ograniczenia URL |
| `content_regex` / `content_contains_any` | skan argumentów / treści |
| `max_per_run` | rate limit w ramach runu silnika |

### Effects

| Effect | Enforce |
|--------|---------|
| `allow` | wykonaj |
| `deny` | zablokuj |
| `require_approval` | zablokuj (do czasu ręcznego OK / wyłączenia reguły) |
| `redact` | pozwól + obowiązek redakcji |
| `monitor` | pozwól + audyt |

## Konfiguracja

```yaml
agents:
  governance:
    enabled: true
    mode: enforce          # enforce | monitor
    policy_pack: config/governance/tiz.policy.yaml
    audit_dir: data/governance
    fail_closed: false     # true = brak packa ⇒ deny
    deny_require_approval: true
```

```bash
python -m market_agents governance status
python -m market_agents governance validate
python -m market_agents governance eval --tool remember --role worker
python -m market_agents governance audit --tail 30
```

## Operator approvals

Gdy polityka zwraca `require_approval` (np. `promote_host_skill`), operator może odblokować:

```bash
python -m market_agents governance approve --tool promote_host_skill --by jan --note "OK po review"
python -m market_agents governance approvals
python -m market_agents governance eval --tool promote_host_skill   # powinno allow
python -m market_agents governance revoke --id <grant_id>
```

Granty: `data/governance/approvals.json`.
