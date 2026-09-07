# Lokalni agenci monitoringu rynku (agentic + vLLM / 4x A100)

System lokalnych **agentów ReAct** do zbierania, inteligentnego parsowania i analizowania
sygnałów rynkowych. Zamiast drogich agentów SaaS (Grok / xAI / cloud LLM) — Twój Dell z
**2 TB RAM + 4x A100** + tanie VPS jako workery.

## Zamiast drogiego Grok

| Grok / cloud | Lokalnie (ten repo) |
|--------------|---------------------|
| Płatne tokeny za każdy cykl scrapu | vLLM na A100 — **0 $/token** |
| Sztab agentów w chmurze | Collector na VPS (`worker --skip-llm`) + ReAct tylko na GPU |
| Brak trwałej ontologii branżowej | `ontology.json` — materiały / maszyny / chłodziwo / procesy |
| Kontekst „w głowie” modelu (drogi) | `get_domain_context` + Notion + knowledge pack floty |

```bash
# VPS — tylko zbieranie (bez LLM)
python -m market_agents worker --every-hours 6

# Dell A100 — rozumienie + ontologia
bash scripts/run_vllm_a100.sh
python -m market_agents ontology --seed
python -m market_agents run --agentic
```

## Co jest w środku

| Element | Rola |
|---------|------|
| **Collector** | RSS / WWW + filtr keywords + deduplikacja |
| **IntelligentParser** | Trafilatura + BeautifulSoup — pełny tekst artykułów |
| **ParsingAgent (ReAct)** | LLM sam wybiera narzędzia: parse, batch_parse, extract_intel, memory |
| **MachiningOntology** | Knowledge graph: materiały ISO, maszyny, chłodziwo, procesy |
| **Analyst / Reporter** | Klasyczny pipeline (fallback) + briefing Markdown/JSON |
| **MarketMemory** | Pamięć między cyklami (JSONL) |
| **vLLM** | Lokalny inference z tool-calling na 4x A100 |

## Hardware (Twój serwer)

```text
Dell + 2TB RAM + 4x A100
        │
        ▼
 scripts/run_vllm_a100.sh   (tensor_parallel_size=4)
        │  OpenAI API :8000
        ▼
 market_agents run --agentic
```

Rekomendowane modele (lokalnie, zero kosztów tokenów):

- `Qwen/Qwen2.5-72B-Instruct-AWQ` — mocny tool-calling, mieści się przy TP=4
- `Qwen/Qwen2.5-32B-Instruct` — szybszy, też bardzo dobry
- `meta-llama/Llama-3.3-70B-Instruct` — FP16/AWQ przy TP=4

## Instalacja

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
# na Dellu z GPU osobno:
# pip install vllm
```

## Konfiguracja (TIZ + znane firmy + e-catalog / PDF / e-shop + Notion)

Fokus: **e-catalogi, PDF, publikacje, digital catalogues, e-shopy** — wg listy **znanych firm**
z Notion (`Katalogi konkurencji — indeks`, ~37 marek: Sandvik, Iscar, Kennametal, MAPAL,
FRAISA, YG-1, OSG, Paul Horn, HAIMER…).

```bash
# profil pod narzędzia skrawające + katalogi konkurencji
python -m market_agents init --profile tiz --force

# sekrety
cp .env.example .env
# NOTION_TOKEN=secret_...
# CF_ACCOUNT_ID / CF_R2_ACCESS_KEY_ID / CF_R2_SECRET_ACCESS_KEY

# w Notion: Share stron TIZ → zaproszenie Internal Integration
export NOTION_TOKEN=secret_...

# sync listy znanych firm + siatki powiązań + stron Notion
python -m market_agents sync-firms
python -m market_agents sync-relations
python -m market_agents sync-profiles   # karty firm + scorecard
python -m market_agents sync-notion
# po włączeniu cloudflare_r2.enabled: true w YAML:
# python -m market_agents sync-r2
```

Źródła: `sources.catalogs` + `industry.competitors` z indeksu Notion.
Seed offline: `config/known_firms.seed.json`, `config/firm_relations.seed.json`.

## Profilowanie konkurentów / dostawców

Buduje karty firm: tożsamość (nazwa, adres, tel, e-mail, WWW + weryfikacja), sygnały finansowe
z treści publicznych, PR/social, katalogi/cenniki/ulotki/e-shop, sieć dystrybucji/klientów,
tabela ocen.

```bash
python -m market_agents sync-profiles
python -m market_agents profiles --scoreboard
python -m market_agents profiles --company "Sandvik Coromant"
python -m market_agents profiles --role supplier
```

Tooli: `build_firm_profiles`, `list_firm_profiles`, `get_firm_profile`, `firm_scoreboard`,
`upsert_firm_profile`, `enrich_firm_profile`, `register_social_mention`.
Relacje: `customer_of`, `supplier_of`, `competes_with` (+ dystrybutor/dealer/OEM).

## Potencjalni klienci (prospects)

Intelligence zakupowy dla firm, które mogą kupować narzędzia/usługi TIZ:
budżet tooling **4–10% kosztów produkcji** (założenie branżowe), od kogo kupują,
jakość parku maszyn, mapa produktów→procesy/narzędzia, osoby wpływające na zakup.

```bash
python -m market_agents prospects --seed
python -m market_agents prospects --budget --vertical automotive --revenue 50000000
python -m market_agents prospects --company "Acme CNC" --analyze-url https://example.com/ --text-file page.txt
python -m market_agents prospects --scoreboard
```

Założenia: `config/prospect_assumptions.json` · seed: `config/prospects.seed.json`  
Tooli: `analyze_prospect`, `list_prospects`, `get_prospect_profile`, `prospect_scoreboard`,
`estimate_tooling_budget`, `ingest_prospect_seeds`.

## Przetargi / sygnały zakupu sprzętu

Parsuj ogłoszenia przetargów oraz newsy, że firma **kupiła / zamierza kupić / planuje
ogłosić przetarg** na CNC, tokarkę, frezarkę, EDM, tooling itd.

```bash
# parse + zapis (profil prospect + CRM follow-up)
python -m market_agents tenders --text-file config/tender_sample.txt --ingest

# lista / scoreboard
python -m market_agents tenders --scoreboard
python -m market_agents tenders --intent announced_tender
```

Intenty: `announced_tender` · `planned_tender` · `intends_to_buy` · `purchased` · `awarded`  
Artefakt: `data/knowledge/tenders.json`  
Tooli agentowe: `parse_tender`, `ingest_tender`, `list_tenders`, `tender_scoreboard`.

## MVP do testów (offline)

Szybka ścieżka bez GPU / Notion / R2 / sieci — seed → profiles → prospect → CRM →
suppliers → digest → export → fleet pack.

```bash
# jednorazowy smoke (pytest + bootstrap + worker pull)
bash scripts/mvp_smoke.sh

# albo ręcznie
python -m market_agents init --profile mvp --force
python -m market_agents mvp bootstrap --reset
python -m market_agents mvp status
python -m market_agents profiles --scoreboard --config config/mvp.example.yaml
python -m market_agents prospects --scoreboard --config config/mvp.example.yaml
python -m market_agents crm list --config config/mvp.example.yaml
```

Config: `config/mvp.example.yaml` · sample prospect: `config/mvp_prospect_sample.txt`  
Artefakty: `data/mvp/` (knowledge, metrics, export, fleet).

## Architecture hardening (P0–P6)

1. ParsingAgent procedura obejmuje **profiles + prospects + CRM tasks**
2. Fleet publish: ontology, firm_profiles, pricelists, literature, product_tech, crm_tasks
3. Po `run`: post-enrich profiles + suppliers + CRM + digest (**tylko centrala**; worker skip);
   centrala z `auto_push_after_run` publikuje knowledge pack dla VPS
4. `governance approve|revoke|approvals` — operator allowlist
5. Testy: schema↔handler parity, orchestrator smoke, fleet publish, architecture gaps
6. CRM lokalny (`crm` CLI) + split `market_agents/tooling/` (shim `tools.py`)

```bash
python -m market_agents governance approve --tool promote_host_skill --by ops
python -m market_agents crm sync-prospects
python -m market_agents crm list
python -m market_agents crm push-notion --dry-run
python -m market_agents suppliers --refresh
python -m market_agents resolve-firms --dry-run
python -m market_agents metrics
python -m market_agents digest
python -m market_agents export-knowledge --out reports/knowledge_export
```

## Dostawcy (suppliers)

Scorecard kanału dystrybucji / dealerów / OEM tooling (obok prospects):

```bash
python -m market_agents suppliers --scoreboard
python -m market_agents suppliers --refresh
```

Tooli: `list_suppliers`, `supplier_scoreboard`, `score_suppliers`.
Golden record: `resolve-firms` / tool `resolve_firm_duplicates` (domyślnie dry-run).
Digest zmian: `digest` / tool `refresh_change_digest`.
Eksport: `export-knowledge` / tool `export_knowledge_pack`.

## Uruchomienie na Dellu

```bash
# terminal 1 — vLLM na 4x A100
bash scripts/run_vllm_a100.sh

# terminal 2 — agenci
python -m market_agents doctor
python -m market_agents run --agentic
python -m market_agents schedule
```

Tooli: `list_known_firms`, `get_firm_presentation`, `discover_new_firms`, `check_firm_known`,
`list_relations`, `add_relation`, `discover_relations`, `firm_neighborhood`,
`list_media_sources`, `discover_media_links`, `parse_media_page`, `extract_fair_exhibitors`,
`list_parsing_skills`, `match_parsing_skills`, `learn_parsing_skill`, `improve_parsing_skill`,
`rate_parsing_skill`, `promote_host_skill`,
`list_parse_rules`, `upsert_parse_rule`, `rate_parse`,
`list_catalog_sources`, `discover_catalog_assets`, `fetch_pdf_text`
(+ Notion/R2: `search_notion`, `fetch_notion_page`, `search_r2`, `fetch_r2_object`).

Parsing nowych firm: `sources.firm_discovery` (Google News RSS) → ekstrakcja nazw →
filtr vs known_firms → kandydaci `new_firm` dla agenta.

Media branżowe (targi / czasopisma / portale): `sources.media` w profilu TIZ
(EMO, AMB, IMTS, CTE, MMS, MM Maschinenmarkt…). Agent: `list_media_sources` →
`discover_media_links` / `extract_fair_exhibitors` → `check_firm_known` na wystawcach.

Siatka powiązań (kto jest dystrybutorem / marką / spółką w grupie OEM):
```bash
python -m market_agents sync-relations
# seed: config/firm_relations.seed.json → data/knowledge/firm_relations.json
```
Typy: `distributor_of`, `dealer_of`, `brand_of`, `subsidiary_of`, `oem_group`,
`partner_of`, `rebrand_of`. Agent buduje graf np. GARANT → brand_of → Hoffmann Group,
Hoffmann Group → distributor_of → Sandvik Coromant.



## Notion = katalog i przedstawienie firm

Twoja baza Notion (`Katalogi konkurencji — indeks`) jest źródłem prawdy:
które firmy istnieją i jak je przedstawiasz.

```bash
export NOTION_TOKEN=secret_...
python -m market_agents sync-firms
```

Sync zbiera właściwości (Company, Country, Site URL, Downloads, Product focus)
oraz **przedstawienie**:
- właściwość `Presentation` / `Przedstawienie` / `Opis`, albo
- treść strony firmy w Notion (bloki)

Agent używa `list_known_firms` + `get_firm_presentation` zanim scrapuje WWW znanej marki.

## Media społecznościowe

Parsowanie **publicznych** profili firm (YouTube / LinkedIn / X) — bez logowania.
YouTube najlepiej z `channel_id` (oficjalny RSS). LinkedIn/X często ograniczają HTML.

```bash
python -m market_agents social
python -m market_agents social --platform youtube
python -m market_agents run --agentic
```

Tooli: `list_social_sources`, `discover_social_posts`, `parse_social_post`  
Konfiguracja: `sources.social` w profilu TIZ.

## Literatura: książki, artykuły, wideo

Korpus jak w Notion **Pozycje** (`Type=book|paper|video`): odkrywanie hubów WWW + sync z Notion.
Lokalnie: `book` | `article` | `video` | `proceedings` | `whitepaper` (`paper`→`article`).

```bash
python -m market_agents literature --sources
python -m market_agents literature --kind article
python -m market_agents run --agentic
```

Rejestr: `data/knowledge/literature.json`  
Tooli: `list_literature_sources`, `discover_literature`, `list_literature`, `register_literature`, `sync_notion_literature`  
Config: `sources.literature` + `sources.notion.literature_database`.

## Ontologia / knowledge graph database

Boty muszą rozumieć **materiały, maszyny, chłodziwo i procesy** — nie tylko nazwy firm.
Lokalny graf (`data/knowledge/ontology.json`) to trwała **ontology DB** (zamiast Grok).

Każdy zebrany sygnał dostaje automatycznie `analysis.domain_context` (bez LLM):
materiały ISO · maszyny · chłodziwo · procesy · parametry (schemat). Briefing ma sekcję
**Kontekst technologiczny**.

## Cybersecurity

SSRF guard (blokada localhost/RFC1918/metadata), tool policy (Notion/R2/write),
redakcja sekretów w traces, fleet pack allowlista. Szczegóły: [SECURITY.md](SECURITY.md).

## AI Governance (policy-as-code)

Silnik `GovernanceEngine` egzekwuje YAML (`config/governance/tiz.policy.yaml`) przed
tool callami: deny-overrides, role worker, require_approval (promote/upsert), rate limits,
zakaz CutData (vc/fz). Docs: [GOVERNANCE.md](GOVERNANCE.md).

```bash
python -m market_agents security
python -m market_agents governance status
python -m market_agents governance eval --tool promote_host_skill
```

Encje: `firm` · `material` (ISO P/M/K/N/S/H) · `machine` · `coolant` · `process` · `tool_family` · `parameter` · `standard` · `literature` · `concept`

```bash
# zbuduj graf z seed + known_firms + firm_relations + literature
python -m market_agents sync-ontology
python -m market_agents ontology --build
python -m market_agents ontology --type firm
python -m market_agents ontology --node process:milling
python -m market_agents ontology --from process:milling --to material:iso-s
python -m market_agents ontology --export data/knowledge/ontology.graphml
```

Tooli: `build_ontology`, `get_domain_context`, `extract_domain_context`, `list_ontology`,
`ontology_neighborhood`, `find_ontology_path`, `add_ontology_edge`  
Seed: `config/machining_ontology.seed.json`  
Eksport: GraphML / JSON-LD

## Informacje techniczne o produktach

Agenty zbierają **źródła tech** konkurencji (cutting data, handbooki, application guides, ISO 13399)
i wyciągają **schemat pól** (vc / fz / ap / ae, grupy materiałowe, chłodzenie) — bez kopiowania
tabel wartości do CutData (zgodnie z polityką Notion TIZ).

```bash
python -m market_agents run --agentic
python -m market_agents product-tech
python -m market_agents product-tech --kind cutting_data --brand Seco
```

Rejestr: `data/knowledge/product_tech.json`  
Tooli: `discover_product_tech`, `list_product_tech`, `register_product_tech`, `extract_product_tech_schema`.

## Dostępne cenniki

Agenty szukają **publicznych / semi-publicznych cenników** konkurencji
(price list / Preisliste / cennik PDF) osobno od katalogów produktowych.

```bash
# skan hubów downloads (agent lub CLI po cyklu)
python -m market_agents run --agentic
python -m market_agents pricelists
python -m market_agents pricelists --brand Sandvik --access public
```

Rejestr: `data/knowledge/available_pricelists.json`  
Tooli: `discover_pricelists`, `list_available_pricelists`, `register_pricelist`.

## Samorozwijający się parser + wspólne skills + anty-ban crawl

Dwie warstwy uczenia + adaptacyjny fetcher (bez bulk / bez banów):

1. **Reguły per-host** (`data/knowledge/site_parse_rules.json`)
2. **Wspólne skills** (`data/knowledge/parsing_skills.json`)
3. **Polite adaptive crawl** (`data/knowledge/crawl_health.json`)
   - 1 request naraz na host, global concurrency ≤ 2
   - delay 1.5–45s z jitterem; rośnie po wolnych odpowiedziach
   - 429/503 → Retry-After + cooldown; 403 → dłuższy cooldown
   - opcjonalnie `robots.txt`
   - `batch_parse` nie robi bulk: ten sam host = sekwencyjnie
   - tool `crawl_status` pokazuje zdrowie hostów

```bash
# agents.crawl.* + agents.agentic.parallel_fetches: 2 (domyślnie)
python -m market_agents parse "https://example.com/news/article"
```

Tylko zbieranie (RSS + katalogi, bez LLM):

```bash
python -m market_agents run --skip-llm --pipeline
```

## Flota: VPS parsują, GPU ulepsza

Kilka lekkich VPS zbiera i parsuje (bez LLM). Centrala (Dell + A100) uczy
`parsing_skills` / `site_parse_rules` i wypycha ulepszenia z powrotem na edge.

```text
  VPS-1 / VPS-2 / …          data/fleet (NFS / rsync / R2 mirror)         Dell + 4×A100
  ─────────────────          ───────────────────────────────────         ──────────────
  fleet-pull  ←────────────  outbox/knowledge/<pack>/  ←──────────────  fleet-publish
  worker (skip-llm)                                                        run --agentic
  fleet-push  ────────────→  inbox/<worker_id>/<pack>/  ──────────────→  fleet-absorb
                                                                             └─ republish
```

**Centrala (GPU):**

```bash
python -m market_agents run --agentic
python -m market_agents fleet-absorb --republish
# albo ręcznie: fleet-publish
python -m market_agents fleet-status
```

**Każdy VPS:**

```bash
cp config/tiz_worker.example.yaml config/industry.yaml
# ustaw agents.fleet.worker_id: vps-1  (unikalny per maszyna)
# zsynchronizuj katalog data/fleet z centralą (NFS / rsync / mirror)

python -m market_agents fleet-pull
python -m market_agents worker --every-hours 6
```

Sync katalogu (przykład rsync z crona na VPS):

```bash
rsync -az user@gpu-host:/opt/tiz/data/fleet/ ./data/fleet/
# … worker cycle …
rsync -az ./data/fleet/inbox/ user@gpu-host:/opt/tiz/data/fleet/inbox/
```

CLI: `fleet-status`, `fleet-publish`, `fleet-pull`, `fleet-push`, `fleet-absorb`, `worker`.

## Jak działa agentic parsing

```text
RSS/WWW + catalogs + media (targi/czasopisma/portale) → kandydaci
            │
            ▼
   ParsingAgent (ReAct, max_steps)
     ├─ list_known_firms / list_relations / firm_neighborhood
     ├─ list_parsing_skills / match_parsing_skills / learn_parsing_skill
     ├─ list_media_sources / discover_media_links / extract_fair_exhibitors
     ├─ parse_media_page / discover_new_firms / discover_relations / add_relation
     ├─ list_catalog_sources / discover_catalog_assets / fetch_pdf_text
     ├─ list_candidates
     ├─ search_notion / search_r2 / search_memory
     ├─ batch_parse / fetch_and_parse
     ├─ upsert_parse_rule / rate_parse / improve_parsing_skill / promote_host_skill
     ├─ extract_market_intel  (new_firm | relation | competitor)
     └─ remember
            │
            ▼
   reports/latest.md + data/agent_traces/trace_*.json
```

## Dlaczego nie zewnętrzni agenci?

- **Koszt**: SaaS agentic łatwo zjada setki $/mies. przy ciągłym monitoringu
- **GPU**: masz 4x A100 — wykorzystaj je lokalnie
- **Dane**: briefy i treść zostają na serwerze (poza publicznymi URL, które sam pobierasz)
- **Kontrola**: Ty ustawiasz źródła, pytania fokusowe, próg relevancji i model

## Rozszerzanie

- Dodaj e-catalog / PDF / e-shop w `sources.catalogs`
- Dodaj feedy / strony w `sources.rss` / `sources.web`
- Dopisz narzędzie w `market_agents/tools.py`
- Zwiększ `agents.agentic.max_steps` / `max_deep_parses` przy mocniejszym modelu

## Uwagi prawne

Scrapuj tylko treści publiczne, respektuj `robots.txt` i regulaminy.
RSS Google News to wygodny start; do produkcji dodaj własne źródła branżowe.
