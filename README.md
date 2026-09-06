# Lokalni agenci monitoringu rynku

System lokalnych agentów do zbierania i analizowania sygnałów rynkowych z Twojej branży.
Działa na **Ollama** (darmowy lokalny LLM) + darmowych źródłach RSS — bez abonamentów typu Perplexity Agents / zewnętrzni agenci SaaS.

## Co dostajesz

| Agent | Rola |
|-------|------|
| **Collector** | Zbiera newsy z RSS i prostych stron WWW, filtruje po słowach kluczowych |
| **Analyst** | Lokalny LLM ocenia typ sygnału (zagrożenie / szansa / konkurencja / trend) |
| **Reporter** | Składa briefing Markdown + JSON w katalogu `reports/` |
| **Orchestrator** | Uruchamia cały cykl na żądanie lub co N godzin |

Koszt operacyjny: prąd + Twój komputer. Zero opłat za tokeny (przy Ollama).

## Wymagania

1. Python 3.10+
2. [Ollama](https://ollama.com) (opcjonalnie — możesz też `--skip-llm` tylko zbierać źródła)
3. Model, np.:

```bash
ollama pull llama3.2
# albo: mistral / qwen2.5 / gemma2
```

## Instalacja

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Konfiguracja branży

```bash
python -m market_agents init
# edytuj config/industry.yaml — branża, keywords, feedy RSS
```

Gotowy przykład PL (meble):

```bash
cp config/furniture.pl.example.yaml config/industry.yaml
```

Feedy Google News (darmowe):

```text
https://news.google.com/rss/search?q=TWOJE+SLOWA&hl=pl&gl=PL&ceid=PL:pl
```

## Uruchomienie

```bash
# diagnostyka Ollama + config
python -m market_agents doctor

# jeden cykl monitoringu
python -m market_agents run

# tylko zbieranie (bez LLM)
python -m market_agents run --skip-llm

# cyklicznie (co N godzin z YAML)
python -m market_agents schedule
```

Raporty trafiają do `reports/latest.md` oraz `reports/report_YYYYMMDD_HHMMSS.md`.

## Architektura

```text
config/industry.yaml
        │
        ▼
 ┌─────────────┐    RSS/WWW     ┌──────────────┐
 │  Collector  │ ─────────────► │   Storage    │
 └─────────────┘                │ data/*.jsonl │
        │                       └──────────────┘
        ▼
 ┌─────────────┐   Ollama       ┌──────────────┐
 │   Analyst   │ ◄────────────► │ Local LLM    │
 └─────────────┘                └──────────────┘
        │
        ▼
 ┌─────────────┐
 │  Reporter   │ ──► reports/*.md + *.json
 └─────────────┘
```

## Dlaczego lokalnie zamiast zewnętrznych agentów?

- **Koszt**: zewnętrzni agenci (autonomous SaaS) łatwo zjadają $50–500+/mies. przy codziennym monitoringu
- **Dane**: newsy i briefy nie wychodzą do chmury (poza publicznymi URL, które sam pobierasz)
- **Kontrola**: Ty dobierasz źródła, słowa kluczowe, próg relevancji i model

## Rozszerzanie

- Dodaj własne feedy RSS / strony w `sources`
- Podmień model w `llm.model` (większy = lepsza analiza, wolniejszy)
- `provider: openai_compatible` + `base_url` działa z LM Studio, LocalAI, vLLM
- Możesz dopisać kolektor (API branżowe, newsletter IMAP) w `market_agents/collectors/`

## Uwagi prawne

Scrapuj tylko treści publiczne, respektuj `robots.txt` i regulaminy serwisów.
RSS Google News jest wygodnym punktem startu; do produkcji warto dodać własne, wiarygodne źródła branżowe.
