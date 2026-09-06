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

app = typer.Typer(
    add_completion=False,
    help="Lokalni agenci monitoringu rynku (Ollama + RSS/WWW).",
)
console = Console()


def _resolve_config(config: Optional[Path]) -> Path:
    path = config or default_config_path()
    if not path.exists():
        console.print(
            f"[red]Brak konfiguracji:[/red] {path}\n"
            "Skopiuj [cyan]config/industry.example.yaml[/cyan] → [cyan]config/industry.yaml[/cyan]"
        )
        raise typer.Exit(code=1)
    return path


@app.command("init")
def init_config(
    force: bool = typer.Option(False, "--force", help="Nadpisz istniejący plik"),
) -> None:
    """Utwórz config/industry.yaml z przykładu."""
    src = Path("config/industry.example.yaml")
    dst = Path("config/industry.yaml")
    if not src.exists():
        console.print("[red]Brak pliku przykładowego config/industry.example.yaml[/red]")
        raise typer.Exit(1)
    if dst.exists() and not force:
        console.print(f"[yellow]{dst} już istnieje.[/yellow] Użyj --force aby nadpisać.")
        raise typer.Exit(0)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    console.print(f"[green]Utworzono[/green] {dst} — uzupełnij branżę, słowa kluczowe i feedy RSS.")


@app.command("doctor")
def doctor(config: Optional[Path] = typer.Option(None, "--config", "-c")) -> None:
    """Sprawdź konfigurację i połączenie z lokalnym LLM."""
    path = _resolve_config(config)
    cfg = load_config(path)
    orch = Orchestrator(cfg)
    health = orch.health()

    table = Table(title="Diagnostyka lokalnych agentów")
    table.add_column("Element")
    table.add_column("Status")
    table.add_row("Konfiguracja", str(path))
    table.add_row("Branża", cfg.industry.name)
    table.add_row("Źródła RSS", str(len(cfg.sources.rss)))
    table.add_row("Źródła WWW", str(len(cfg.sources.web)))
    table.add_row("LLM provider", cfg.llm.provider)
    table.add_row("Model", cfg.llm.model)
    if health.get("ok"):
        table.add_row("LLM", f"[green]OK[/green] modele: {health.get('models')}")
    else:
        table.add_row(
            "LLM",
            f"[red]OFF[/red] {health.get('error')}\n"
            "Zainstaluj Ollamę: https://ollama.com  następnie: ollama pull " + cfg.llm.model,
        )
    console.print(table)


@app.command("run")
def run_once(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    skip_llm: bool = typer.Option(
        False, "--skip-llm", help="Tylko zbieranie + raport bez analizy LLM"
    ),
    show: bool = typer.Option(True, "--show/--no-show", help="Pokaż raport w terminalu"),
) -> None:
    """Jednorazowy cykl monitoringu rynku."""
    path = _resolve_config(config)
    orch = Orchestrator.from_path(path)
    console.print(Panel.fit(f"Start monitoringu: [bold]{orch.config.industry.name}[/bold]"))
    result = orch.run(skip_llm=skip_llm)
    console.print(
        f"[green]Gotowe.[/green] Nowych sygnałów: {result.new_items}\n"
        f"Raport: {result.md_path}"
    )
    if show:
        console.print(Markdown(result.report.summary_markdown))


@app.command("schedule")
def run_schedule(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    skip_llm: bool = typer.Option(False, "--skip-llm"),
) -> None:
    """Uruchamia monitoring cyklicznie (domyślnie co N godzin z configu)."""
    path = _resolve_config(config)
    orch = Orchestrator.from_path(path)
    hours = orch.config.schedule.every_hours

    def job() -> None:
        console.print("[cyan]Cykl monitoringu…[/cyan]")
        result = orch.run(skip_llm=skip_llm)
        console.print(f"Nowe sygnały: {result.new_items} → {result.md_path}")

    schedule.every(hours).hours.do(job)
    console.print(f"Harmonogram co {hours}h. Ctrl+C aby przerwać. Pierwszy przebieg zaraz.")
    job()
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    app()
