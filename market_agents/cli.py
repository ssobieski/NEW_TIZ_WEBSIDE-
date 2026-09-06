from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import schedule
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from market_agents.agents.orchestrator import Orchestrator
from market_agents.config import default_config_path, load_config
from market_agents.parsing import IntelligentParser as IntelligentParser

app = typer.Typer(
    add_completion=False,
    help="Lokalni agenci monitoringu rynku — agentic parsing na vLLM/A100.",
)
console = Console()


def _resolve_config(config: Optional[Path]) -> Path:
    path = config or default_config_path()
    if not path.exists():
        console.print(
            f"[red]Brak konfiguracji:[/red] {path}\n"
            "Skopiuj [cyan]config/dell_a100.example.yaml[/cyan] → [cyan]config/industry.yaml[/cyan]"
        )
        raise typer.Exit(code=1)
    return path


@app.command("init")
def init_config(
    force: bool = typer.Option(False, "--force", help="Nadpisz istniejący plik"),
    profile: str = typer.Option(
        "tiz",
        "--profile",
        help="tiz (Notion+R2 tooling) | dell | basic | furniture",
    ),
) -> None:
    """Utwórz config/industry.yaml z profilu."""
    mapping = {
        "dell": Path("config/dell_a100.example.yaml"),
        "basic": Path("config/industry.example.yaml"),
        "furniture": Path("config/furniture.pl.example.yaml"),
        "tiz": Path("config/tiz_cutting_tools.example.yaml"),
    }
    src = mapping.get(profile, mapping["dell"])
    dst = Path("config/industry.yaml")
    if not src.exists():
        console.print(f"[red]Brak pliku przykładowego {src}[/red]")
        raise typer.Exit(1)
    if dst.exists() and not force:
        console.print(f"[yellow]{dst} już istnieje.[/yellow] Użyj --force aby nadpisać.")
        raise typer.Exit(0)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    console.print(f"[green]Utworzono[/green] {dst} z profilu [cyan]{profile}[/cyan].")


@app.command("doctor")
def doctor(config: Optional[Path] = typer.Option(None, "--config", "-c")) -> None:
    """Sprawdź konfigurację i połączenie z lokalnym LLM (vLLM/Ollama)."""
    path = _resolve_config(config)
    cfg = load_config(path)
    orch = Orchestrator(cfg)
    health = orch.health()

    table = Table(title="Diagnostyka agentic market agents")
    table.add_column("Element")
    table.add_column("Status")
    table.add_row("Konfiguracja", str(path))
    table.add_row("Branża", cfg.industry.name)
    table.add_row("Agentic", str(cfg.agents.agentic.enabled))
    table.add_row("Max steps", str(cfg.agents.agentic.max_steps))
    table.add_row("Źródła RSS", str(len(cfg.sources.rss)))
    table.add_row("Notion", str(cfg.sources.notion.enabled))
    table.add_row("Cloudflare R2", str(cfg.sources.cloudflare_r2.enabled))
    table.add_row("LLM provider", cfg.llm.provider)
    table.add_row("Model", cfg.llm.model)
    table.add_row("TP (A100)", str(cfg.llm.tensor_parallel_size))
    if health.get("ok"):
        table.add_row("LLM", f"[green]OK[/green] modele: {health.get('models')}")
    else:
        table.add_row(
            "LLM",
            f"[red]OFF[/red] {health.get('error')}\n{health.get('hint', '')}",
        )
    # Notion / R2 health
    if cfg.sources.notion.enabled:
        token = cfg.notion_token()
        table.add_row(
            "NOTION_TOKEN",
            "[green]set[/green]" if token else "[red]missing[/red] (export NOTION_TOKEN=...)",
        )
    if cfg.sources.cloudflare_r2.enabled:
        creds = cfg.r2_credentials()
        ok = all(creds.get(k) for k in ("account_id", "access_key", "secret_key"))
        table.add_row("R2 creds", "[green]set[/green]" if ok else "[red]missing[/red]")
        if ok:
            try:
                from market_agents.collectors.r2 import build_r2_collector

                r2 = build_r2_collector(cfg.sources.cloudflare_r2, creds)
                table.add_row("R2", str(r2.healthcheck()))
            except Exception as exc:  # noqa: BLE001
                table.add_row("R2", f"[red]{exc}[/red]")
    console.print(table)


@app.command("sync-notion")
def sync_notion(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    no_enrich: bool = typer.Option(False, "--no-enrich", help="Bez pobierania pełnego tekstu stron"),
) -> None:
    """Zsynchronizuj istniejącą bazę Notion → lokalny cache wiedzy."""
    from market_agents.sync import KnowledgeSync

    path = _resolve_config(config)
    cfg = load_config(path)
    cfg.sources.notion.enabled = True
    result = KnowledgeSync(cfg).sync_notion(enrich_text=not no_enrich)
    console.print(
        f"[green]Notion sync OK[/green]: {result.items} stron → {result.path}"
    )


@app.command("sync-firms")
def sync_firms(config: Optional[Path] = typer.Option(None, "--config", "-c")) -> None:
    """Zsynchronizuj listę znanych firm z Notion (Katalogi konkurencji — indeks)."""
    from market_agents.sync import KnowledgeSync

    path = _resolve_config(config)
    cfg = load_config(path)
    cfg.sources.notion.enabled = True
    result = KnowledgeSync(cfg).sync_known_firms()
    companies = (result.details or {}).get("companies") or []
    console.print(
        f"[green]Firmy sync OK[/green]: {result.items} → {result.path}\n"
        f"Podpowiedzi katalogów: {(result.details or {}).get('catalog_hints')}"
    )
    if companies:
        console.print("Firmy: " + ", ".join(companies[:25]) + ("…" if len(companies) > 25 else ""))


@app.command("sync-relations")
def sync_relations(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    no_notion_scan: bool = typer.Option(
        False,
        "--no-notion-scan",
        help="Tylko seed/cache — bez heurystyk z notion_pages.jsonl",
    ),
) -> None:
    """Zbuduj siatkę powiązań firm (dystrybutor / marka / grupa OEM)."""
    from market_agents.sync import KnowledgeSync

    path = _resolve_config(config)
    cfg = load_config(path)
    result = KnowledgeSync(cfg).sync_relations(scan_notion_cache=not no_notion_scan)
    by_type = (result.details or {}).get("by_type") or {}
    console.print(
        f"[green]Relations sync OK[/green]: {result.items} krawędzi → {result.path}\n"
        f"Typy: {by_type} | extracted: {(result.details or {}).get('extracted_from_notion')}"
    )


@app.command("sync-r2")
def sync_r2(config: Optional[Path] = typer.Option(None, "--config", "-c")) -> None:
    """Zsynchronizuj archiwum Cloudflare R2 → lokalny cache."""
    from market_agents.sync import KnowledgeSync

    path = _resolve_config(config)
    cfg = load_config(path)
    cfg.sources.cloudflare_r2.enabled = True
    result = KnowledgeSync(cfg).sync_r2()
    console.print(f"[green]R2 sync OK[/green]: {result.items} obiektów → {result.path}")


@app.command("sync-all")
def sync_all(config: Optional[Path] = typer.Option(None, "--config", "-c")) -> None:
    """Sync Notion + R2 (wg configu)."""
    from market_agents.sync import KnowledgeSync

    path = _resolve_config(config)
    cfg = load_config(path)
    results = KnowledgeSync(cfg).sync_all()
    if not results:
        console.print("[yellow]Nic do sync — włącz notion/cloudflare_r2 w YAML.[/yellow]")
        raise typer.Exit(0)
    for r in results:
        console.print(f"[green]{r.source}[/green]: {r.items} → {r.path}")


@app.command("parse")
def parse_url(
    url: str = typer.Argument(..., help="URL do inteligentnego parsowania"),
) -> None:
    """Szybki test inteligentnego parsera (bez LLM)."""
    parser = IntelligentParser()
    doc = parser.parse_url(url)
    if not doc.ok:
        console.print(f"[red]Parse failed:[/red] {doc.error}")
        raise typer.Exit(1)
    console.print(
        Panel.fit(
            f"[bold]{doc.title}[/bold]\n"
            f"method={doc.parse_method} chars={len(doc.text)} links={len(doc.links)}"
        )
    )
    console.print(doc.excerpt)


@app.command("run")
def run_once(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    skip_llm: bool = typer.Option(
        False, "--skip-llm", help="Tylko zbieranie + raport bez analizy LLM"
    ),
    agentic: Optional[bool] = typer.Option(
        None,
        "--agentic/--pipeline",
        help="Wymuś tryb agentic ReAct lub klasyczny pipeline",
    ),
    show: bool = typer.Option(True, "--show/--no-show", help="Pokaż raport w terminalu"),
) -> None:
    """Cykl monitoringu: agentic parsing (domyślnie) lub pipeline."""
    path = _resolve_config(config)
    orch = Orchestrator.from_path(path)
    mode_label = (
        "agentic"
        if (agentic if agentic is not None else orch.config.agents.agentic.enabled)
        else "pipeline"
    )
    console.print(
        Panel.fit(
            f"Start monitoringu ([cyan]{mode_label}[/cyan]): "
            f"[bold]{orch.config.industry.name}[/bold]"
        )
    )
    result = orch.run(skip_llm=skip_llm, agentic=agentic)
    console.print(
        f"[green]Gotowe.[/green] Tryb: {result.mode} | sygnałów: {result.new_items}\n"
        f"Raport: {result.md_path}"
    )
    if result.trace_path:
        console.print(f"Trace agenta: {result.trace_path}")
    if show:
        console.print(Markdown(result.report.summary_markdown))


@app.command("schedule")
def run_schedule(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    skip_llm: bool = typer.Option(False, "--skip-llm"),
    agentic: Optional[bool] = typer.Option(None, "--agentic/--pipeline"),
) -> None:
    """Uruchamia monitoring cyklicznie (domyślnie co N godzin z configu)."""
    path = _resolve_config(config)
    orch = Orchestrator.from_path(path)
    hours = orch.config.schedule.every_hours

    def job() -> None:
        console.print("[cyan]Cykl monitoringu…[/cyan]")
        result = orch.run(skip_llm=skip_llm, agentic=agentic)
        console.print(f"[{result.mode}] nowe: {result.new_items} → {result.md_path}")

    schedule.every(hours).hours.do(job)
    console.print(f"Harmonogram co {hours}h. Ctrl+C aby przerwać. Pierwszy przebieg zaraz.")
    job()
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    app()
