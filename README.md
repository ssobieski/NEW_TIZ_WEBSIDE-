# Lokalni agenci monitoringu rynku (agentic + vLLM / 4x A100)

System lokalnych **agentów ReAct** do zbierania, inteligentnego parsowania i analizowania
sygnałów rynkowych. Zamiast drogich agentów SaaS — Twój Dell z **2 TB RAM + 4x A100**.

## Co jest w środku

| Element | Rola |
|---------|------|
| **Collector** | RSS / WWW + filtr keywords + deduplikacja |
| **IntelligentParser** | Trafilatura + BeautifulSoup — pełny tekst artykułów |
| **ParsingAgent (ReAct)** | LLM sam wybiera narzędzia: parse, batch_parse, extract_intel, memory |
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

# sync listy znanych firm + stron Notion
python -m market_agents sync-firms
python -m market_agents sync-notion
# po włączeniu cloudflare_r2.enabled: true w YAML:
# python -m market_agents sync-r2
```

Źródła: `sources.catalogs` + `industry.competitors` z indeksu Notion.
Seed offline: `config/known_firms.seed.json`.

## Uruchomienie na Dellu

```bash
# terminal 1 — vLLM na 4x A100
bash scripts/run_vllm_a100.sh

# terminal 2 — agenci
python -m market_agents doctor
python -m market_agents run --agentic
python -m market_agents schedule
```

Tooli: `list_known_firms`, `list_catalog_sources`, `discover_catalog_assets`, `fetch_pdf_text`
(+ Notion/R2: `search_notion`, `fetch_notion_page`, `search_r2`, `fetch_r2_object`).

Szybki test parsera (bez LLM):

```bash
python -m market_agents parse "https://example.com/news/article"
```

Tylko zbieranie (RSS + katalogi, bez LLM):

```bash
python -m market_agents run --skip-llm --pipeline
```

## Jak działa agentic parsing

```text
RSS/WWW + catalogs (PDF/ecatalog/eshop) → kandydaci
            │
            ▼
   ParsingAgent (ReAct, max_steps)
     ├─ list_catalog_sources / discover_catalog_assets / fetch_pdf_text
     ├─ list_candidates
     ├─ search_notion / search_r2 / search_memory
     ├─ batch_parse / fetch_and_parse
     ├─ extract_market_intel
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
