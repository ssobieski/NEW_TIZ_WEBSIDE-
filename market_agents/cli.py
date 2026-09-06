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
        "dell",
        "--profile",
        help="dell (4x A100 / vLLM) | basic | furniture",
    ),
) -> None:
    """Utwórz config/industry.yaml z profilu."""
    mapping = {
        "dell": Path("config/dell_a100.example.yaml"),
        "basic": Path("config/industry.example.yaml"),
        "furniture": Path("config/furniture.pl.example.yaml"),
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
    console.print(table)


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
