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
    sec = getattr(cfg.agents, "security", None)
    if sec is not None:
        table.add_row(
            "Security SSRF",
            "[green]block private[/green]"
            if getattr(sec, "block_private_networks", True)
            else "[yellow]private allowed[/yellow]",
        )
        table.add_row(
            "Security tools",
            f"write={getattr(sec, 'allow_write_tools', True)} "
            f"notion={getattr(sec, 'allow_notion_tools', True)} "
            f"r2={getattr(sec, 'allow_r2_tools', True)} "
            f"strict={getattr(sec, 'strict_tool_mode', False)}",
        )
        table.add_row(
            "Trace redaction",
            "[green]on[/green]" if getattr(sec, "redact_traces", True) else "[yellow]off[/yellow]",
        )
    gov = getattr(cfg.agents, "governance", None)
    if gov is not None:
        table.add_row(
            "AI governance",
            f"{'on' if getattr(gov, 'enabled', True) else 'off'} "
            f"mode={getattr(gov, 'mode', 'enforce')} "
            f"pack={getattr(gov, 'policy_pack', '')}",
        )
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


@app.command("sync-profiles")
def sync_profiles(config: Optional[Path] = typer.Option(None, "--config", "-c")) -> None:
    """Zbuduj profile firm (tożsamość, sieć, katalogi/cenniki, scorecard) z ekosystemu wiedzy."""
    from market_agents.firm_profiles import FirmProfileRegistry

    path = _resolve_config(config)
    cfg = load_config(path)
    reg = FirmProfileRegistry.load(cfg.data_path)
    result = reg.build_from_ecosystem(
        cfg.data_path,
        competitors=list(cfg.industry.competitors or []),
        default_role="competitor",
    )
    console.print(
        f"[green]Profiles OK[/green]: {result['count']} → {result['path']}"
    )
    top = result.get("scoreboard_top") or []
    if top:
        table = Table(title="Scoreboard (top)")
        table.add_column("Firma")
        table.add_column("Overall", justify="right")
        table.add_column("Sieć", justify="right")
        table.add_column("E-shop/cenniki", justify="right")
        table.add_column("PR", justify="right")
        for row in top[:15]:
            table.add_row(
                str(row.get("company")),
                str(row.get("overall")),
                str(row.get("distribution_reach")),
                str(row.get("digital_commerce")),
                str(row.get("pr_presence")),
            )
        console.print(table)


@app.command("profiles")
def profiles_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    company: Optional[str] = typer.Option(None, "--company", help="Pełny profil jednej firmy"),
    role: Optional[str] = typer.Option(
        None, "--role", help="competitor|supplier|distributor|dealer|customer|partner"
    ),
    q: Optional[str] = typer.Option(None, "--q"),
    scoreboard: bool = typer.Option(False, "--scoreboard", help="Tabela ocen"),
    limit: int = typer.Option(25, "--limit"),
) -> None:
    """Profilowanie konkurentów/dostawców — kontakty, sieć, assets, scorecard."""
    from market_agents.firm_profiles import FirmProfileRegistry

    path = _resolve_config(config)
    cfg = load_config(path)
    reg = FirmProfileRegistry.load(cfg.data_path)
    if not reg.profiles:
        console.print(
            "[yellow]Brak profili.[/yellow] Uruchom: python -m market_agents sync-profiles"
        )
        raise typer.Exit(0)

    if company:
        p = reg.get(company)
        if not p:
            console.print(f"[red]Brak profilu:[/red] {company}")
            raise typer.Exit(1)
        console.print_json(data=p.to_dict())
        return

    if scoreboard:
        table = Table(title="Firm scoreboard")
        for col in (
            "Firma",
            "Role",
            "Overall",
            "Tożsamość",
            "Trust",
            "Dystrybucja",
            "Digital",
            "PR",
            "Finanse",
        ):
            table.add_column(col, justify="right" if col != "Firma" and col != "Role" else "left")
        for row in reg.scoreboard(limit=limit):
            table.add_row(
                str(row["company"]),
                ",".join(row.get("roles") or []),
                str(row.get("overall")),
                str(row.get("completeness")),
                str(row.get("identity_trust")),
                str(row.get("distribution_reach")),
                str(row.get("digital_commerce")),
                str(row.get("pr_presence")),
                str(row.get("financial_transparency")),
            )
        console.print(table)
        return

    rows = reg.list(role=role, q=q, sort="overall", limit=limit)
    table = Table(title=f"Firm profiles ({len(rows)})")
    table.add_column("Firma")
    table.add_column("Role")
    table.add_column("Kraj")
    table.add_column("Overall", justify="right")
    table.add_column("WWW")
    for p in rows:
        web = (p.websites or [{}])[0].get("url") if p.websites else ""
        table.add_row(
            p.company,
            ",".join(p.roles),
            p.country or "",
            str((p.scores or {}).get("overall", "")),
            str(web or "")[:48],
        )
    console.print(table)


@app.command("prospects")
def prospects_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    company: Optional[str] = typer.Option(None, "--company"),
    analyze_url: Optional[str] = typer.Option(
        None, "--analyze-url", help="Pobierz WWW i zbuduj intel zakupowy"
    ),
    text_file: Optional[Path] = typer.Option(
        None, "--text-file", help="Analiza z pliku tekstowego (bez fetch)"
    ),
    scoreboard: bool = typer.Option(False, "--scoreboard"),
    seed: bool = typer.Option(False, "--seed", help="Wczytaj config/prospects.seed.json"),
    vertical: Optional[str] = typer.Option(None, "--vertical", help="Dla --budget"),
    revenue: Optional[float] = typer.Option(None, "--revenue", help="Przychód EUR (szacunek budżetu)"),
    budget: bool = typer.Option(False, "--budget", help="Policz budżet tooling 4–10%"),
    limit: int = typer.Option(25, "--limit"),
) -> None:
    """Potencjalni klienci — budżet tooling, park maszyn, dostawcy, decydenci, scorecard."""
    from market_agents.prospects import (
        ProspectRegistry,
        analyze_prospect_html,
        analyze_prospect_text,
        estimate_tooling_budget,
        load_vertical_assumptions,
    )
    from market_agents.polite_http import build_fetcher_from_config

    path = _resolve_config(config)
    cfg = load_config(path)
    pr = ProspectRegistry.load(cfg.data_path)

    if seed:
        console.print(pr.ingest_seeds(cfg.data_path))
        return

    if budget:
        vert = vertical or "unknown"
        out = estimate_tooling_budget(
            vertical=vert,
            revenue_eur=revenue,
            assumptions=load_vertical_assumptions(),
        )
        console.print_json(data=out)
        return

    if analyze_url or text_file or (company and (analyze_url or text_file)):
        name = company or "Unknown prospect"
        assumptions = load_vertical_assumptions()
        if text_file:
            raw = Path(text_file).read_text(encoding="utf-8")
            analysis = analyze_prospect_text(
                raw, company=name, website=analyze_url or "", assumptions=assumptions
            )
        else:
            assert analyze_url
            fetcher = build_fetcher_from_config(cfg)
            try:
                resp = fetcher.get(analyze_url)
                html = resp.text or ""
                final = str(resp.url) if resp.url else analyze_url
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]Fetch failed:[/red] {exc}")
                raise typer.Exit(1)
            analysis = analyze_prospect_html(
                html, company=name, base_url=final, assumptions=assumptions
            )
        profile = pr.upsert_from_analysis(name, analysis, cfg.data_path)
        console.print(
            f"[green]Prospect OK[/green]: {profile.company} "
            f"opportunity={((profile.prospect or {}).get('opportunity') or {}).get('overall')} "
            f"vertical={(profile.prospect or {}).get('vertical')} "
            f"quality={(profile.prospect or {}).get('quality_tier')}"
        )
        console.print_json(data=profile.prospect)
        return

    if company:
        p = pr.profiles.get(company)
        if not p or not p.prospect:
            console.print(f"[red]Brak prospect:[/red] {company}")
            raise typer.Exit(1)
        console.print_json(data={"company": p.company, "prospect": p.prospect, "scores": p.scores})
        return

    if scoreboard or True:  # default: scoreboard list
        rows = pr.scoreboard(limit=limit)
        if not rows:
            console.print(
                "[yellow]Brak prospectów.[/yellow] "
                "Użyj: prospects --seed  albo  prospects --company X --analyze-url URL"
            )
            raise typer.Exit(0)
        table = Table(title="Prospect opportunity scoreboard")
        table.add_column("Firma")
        table.add_column("Branża")
        table.add_column("Jakość")
        table.add_column("Opportunity", justify="right")
        table.add_column("Budżet mid EUR", justify="right")
        table.add_column("Dostawcy", justify="right")
        table.add_column("Procesy")
        for row in rows:
            table.add_row(
                str(row.get("company")),
                str(row.get("vertical") or ""),
                str(row.get("quality_tier") or ""),
                str(row.get("opportunity") or 0),
                str(int(row["budget_mid_eur"])) if row.get("budget_mid_eur") else "—",
                str(row.get("buys_from_count") or 0),
                ",".join(row.get("processes") or [])[:40],
            )
        console.print(table)


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


@app.command("security")
def security_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Pokaż politykę cybersecurity (SSRF, tool policy, fleet, redaction)."""
    from market_agents.security import SecurityPolicy, security_status

    path = _resolve_config(config)
    cfg = load_config(path)
    sec = getattr(cfg.agents, "security", None)
    policy = SecurityPolicy(
        block_private_networks=bool(getattr(sec, "block_private_networks", True)),
        allowed_hosts=list(getattr(sec, "allowed_hosts", None) or []),
        allow_write_tools=bool(getattr(sec, "allow_write_tools", True)),
        allow_notion_tools=bool(getattr(sec, "allow_notion_tools", True)),
        allow_r2_tools=bool(getattr(sec, "allow_r2_tools", True)),
        strict_tool_mode=bool(getattr(sec, "strict_tool_mode", False)),
        redact_traces=bool(getattr(sec, "redact_traces", True)),
        fleet_allowlist_only=bool(getattr(sec, "fleet_allowlist_only", True)),
        enforce_r2_prefix=bool(getattr(sec, "enforce_r2_prefix", True)),
    )
    st = security_status(policy)
    table = Table(title="Cybersecurity controls")
    table.add_column("Control")
    table.add_column("Value")
    for k, v in st.items():
        if k in {"ok", "controls"}:
            continue
        table.add_row(k, str(v))
    console.print(table)
    console.print("Controls: " + ", ".join(st.get("controls") or []))
    console.print(
        "Szczegóły: SECURITY.md — SSRF, prompt-injection, fleet pack, secrets redaction."
    )


@app.command("governance")
def governance_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    action: str = typer.Argument(
        "status",
        help="status | validate | eval | audit | approve | revoke | approvals",
    ),
    tool: Optional[str] = typer.Option(None, "--tool", help="Nazwa toola (eval/approve)"),
    role: Optional[str] = typer.Option(None, "--role", help="central|worker (dla eval)"),
    url: Optional[str] = typer.Option(None, "--url", help="URL (dla eval fetch)"),
    tail: int = typer.Option(20, "--tail", help="Liczba wpisów audit"),
    by: str = typer.Option("operator", "--by", help="Kto zatwierdza (approve)"),
    note: str = typer.Option("", "--note", help="Notatka approve"),
    company: Optional[str] = typer.Option(None, "--company", help="Scope approve"),
    host: Optional[str] = typer.Option(None, "--host", help="Scope approve (host)"),
    grant_id: Optional[str] = typer.Option(None, "--id", help="ID grantu (revoke)"),
    expires: Optional[str] = typer.Option(None, "--expires", help="ISO expiry approve"),
) -> None:
    """AI governance — Policy-as-Code + operator approvals."""
    from market_agents.approvals import ApprovalStore
    from market_agents.governance import (
        GovernanceEngine,
        PolicyRequest,
        load_policy_pack,
        validate_policy_pack,
    )

    path = _resolve_config(config)
    cfg = load_config(path)
    engine = GovernanceEngine.from_config(cfg)
    act = (action or "status").lower().strip()
    audit_dir = Path(getattr(cfg.agents.governance, "audit_dir", "data/governance"))

    if act == "status":
        st = engine.status()
        store = ApprovalStore.load(audit_dir)
        st["active_approvals"] = len(store.list_active())
        table = Table(title="AI Governance (policy-as-code)")
        table.add_column("Key")
        table.add_column("Value")
        for k, v in st.items():
            table.add_row(k, str(v))
        console.print(table)
        console.print("Docs: GOVERNANCE.md — approve: governance approve --tool …")
        return

    if act == "validate":
        pack_path = getattr(cfg.agents.governance, "policy_pack", None)
        if not pack_path:
            console.print("[red]Brak policy_pack w config[/red]")
            raise typer.Exit(1)
        try:
            pack = load_policy_pack(pack_path)
            warns = validate_policy_pack(pack)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Invalid policy pack:[/red] {exc}")
            raise typer.Exit(1)
        console.print(
            f"[green]OK[/green] policy={pack.id} rules={len(pack.rules)} path={pack.source_path}"
        )
        for w in warns:
            console.print(f"[yellow]warn[/yellow] {w}")
        return

    if act == "eval":
        if tool:
            req = PolicyRequest(
                action="tool_call",
                tool=tool,
                role=role or engine.role,
                url=url,
                args={"url": url, "company": company, "host_or_url": host} if True else {},
            )
            # clean empty args
            req.args = {k: v for k, v in (req.args or {}).items() if v}
        elif url:
            req = PolicyRequest(action="fetch", url=url, role=role or engine.role)
        else:
            console.print("Podaj --tool i/lub --url")
            raise typer.Exit(1)
        decision = engine.evaluate(req)
        console.print_json(data=decision.to_dict())
        if not decision.allowed:
            raise typer.Exit(2)
        return

    if act == "audit":
        rows = engine.recent_audit(limit=tail)
        if not rows:
            console.print("Brak wpisów audit (data/governance/audit.jsonl)")
            return
        for row in rows:
            console.print_json(data=row)
        return

    if act == "approve":
        if not tool:
            console.print("Podaj --tool (np. promote_host_skill)")
            raise typer.Exit(1)
        store = ApprovalStore.load(audit_dir)
        grant = store.approve(
            tool,
            approved_by=by,
            note=note,
            company=company,
            host=host,
            expires_at=expires,
        )
        store.save(audit_dir)
        console.print(f"[green]Approved[/green] id={grant.id} tool={grant.tool}")
        console.print_json(data=grant.to_dict())
        return

    if act == "revoke":
        if not grant_id:
            console.print("Podaj --id grantu")
            raise typer.Exit(1)
        store = ApprovalStore.load(audit_dir)
        ok = store.revoke(grant_id)
        store.save(audit_dir)
        if not ok:
            console.print(f"[red]Nie znaleziono[/red] {grant_id}")
            raise typer.Exit(1)
        console.print(f"[yellow]Revoked[/yellow] {grant_id}")
        return

    if act in {"approvals", "list-approvals"}:
        store = ApprovalStore.load(audit_dir)
        rows = store.list_active()
        if not rows:
            console.print("Brak aktywnych approvals")
            return
        table = Table(title="Active approvals")
        table.add_column("ID")
        table.add_column("Tool")
        table.add_column("By")
        table.add_column("Company")
        table.add_column("Expires")
        for g in rows:
            table.add_row(g.id, g.tool, g.approved_by, g.company or "", g.expires_at or "∞")
        console.print(table)
        return

    console.print(
        f"Nieznana akcja: {action} (status|validate|eval|audit|approve|revoke|approvals)"
    )
    raise typer.Exit(1)


@app.command("crm")
def crm_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    action: str = typer.Argument("list", help="list | sync-prospects | done | push-notion"),
    company: Optional[str] = typer.Option(None, "--company"),
    task_id: Optional[str] = typer.Option(None, "--id"),
    min_opp: float = typer.Option(60.0, "--min-opportunity"),
    limit: int = typer.Option(30, "--limit"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    all_priorities: bool = typer.Option(
        False, "--all-priorities", help="push-notion: nie tylko hot/high"
    ),
) -> None:
    """Lokalna kolejka CRM / follow-up (hot prospects)."""
    from market_agents.crm_tasks import (
        CrmTaskStore,
        create_tasks_from_prospect_scoreboard,
        push_crm_tasks_to_notion,
    )

    path = _resolve_config(config)
    cfg = load_config(path)
    act = (action or "list").lower().strip()
    if act == "sync-prospects":
        result = create_tasks_from_prospect_scoreboard(
            cfg.data_path, min_opportunity=min_opp, limit=limit
        )
        console.print(result)
        return
    if act == "push-notion":
        result = push_crm_tasks_to_notion(
            cfg,
            only_hot=not all_priorities,
            limit=limit,
            dry_run=dry_run,
        )
        console.print(result)
        return
    if act == "done":
        if not task_id:
            console.print("Podaj --id")
            raise typer.Exit(1)
        store = CrmTaskStore.load(cfg.data_path)
        t = store.set_status(task_id, "done")
        if not t:
            console.print("[red]Brak taska[/red]")
            raise typer.Exit(1)
        store.save(cfg.data_path)
        console.print(f"[green]Done[/green] {t.id} {t.company}")
        return
    store = CrmTaskStore.load(cfg.data_path)
    rows = store.list(status="open", company=company, limit=limit)
    table = Table(title=f"CRM open tasks ({len(rows)})")
    table.add_column("ID")
    table.add_column("Pri")
    table.add_column("Company")
    table.add_column("Opp", justify="right")
    table.add_column("Notion")
    table.add_column("Title")
    for t in rows:
        table.add_row(
            t.id,
            t.priority,
            t.company,
            str(int(t.opportunity_score)) if t.opportunity_score is not None else "—",
            "yes" if t.notion_url else "—",
            t.title[:40],
        )
    console.print(table)


@app.command("suppliers")
def suppliers_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    scoreboard: bool = typer.Option(True, "--scoreboard/--no-scoreboard"),
    refresh: bool = typer.Option(False, "--refresh", help="Przelicz i zapisz scorecard"),
    q: Optional[str] = typer.Option(None, "--q", help="Filtr nazwy"),
    limit: int = typer.Option(25, "--limit"),
) -> None:
    """Dostawcy / dystrybutorzy — scorecard kanału i pokrycia marek."""
    from market_agents.suppliers import SupplierRegistry

    path = _resolve_config(config)
    cfg = load_config(path)
    reg = SupplierRegistry.load(cfg.data_path)
    if refresh:
        console.print(reg.refresh_and_save(cfg.data_path))
        return
    rows = reg.scoreboard(limit=limit) if scoreboard else []
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in str(r.get("company") or "").lower()]
    if not rows:
        # still list without forcing scoreboard flag
        for p in reg.list_suppliers(q=q, limit=limit):
            rows.append(
                {
                    "company": p.company,
                    "roles": p.roles,
                    "country": p.country,
                    "supplier_overall": (p.scores or {}).get("supplier_overall"),
                    "brand_coverage": None,
                    "channel_reach": None,
                    "customers": len((p.network or {}).get("customers") or []),
                    "brands": [],
                }
            )
    if not rows:
        console.print(
            "[yellow]Brak dostawców.[/yellow] Uruchom sync-profiles / relacje, potem suppliers --refresh"
        )
        raise typer.Exit(0)
    table = Table(title="Supplier scoreboard")
    table.add_column("Firma")
    table.add_column("Role")
    table.add_column("Score", justify="right")
    table.add_column("Kanał", justify="right")
    table.add_column("Klienci", justify="right")
    table.add_column("Kraj")
    for row in rows:
        table.add_row(
            str(row.get("company")),
            ",".join(row.get("roles") or [])[:28],
            str(row.get("supplier_overall") or "—"),
            str(row.get("channel_reach") if row.get("channel_reach") is not None else "—"),
            str(row.get("customers") or 0),
            str(row.get("country") or "—"),
        )
    console.print(table)


@app.command("resolve-firms")
def resolve_firms_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Domyślnie tylko podgląd"),
    threshold: float = typer.Option(0.92, "--threshold"),
) -> None:
    """Golden record — znajdź / scal duplikaty firm."""
    from market_agents.entity_resolution import resolve_golden_records

    path = _resolve_config(config)
    cfg = load_config(path)
    result = resolve_golden_records(
        cfg.data_path, threshold=threshold, dry_run=dry_run
    )
    console.print(result)


@app.command("metrics")
def metrics_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    limit: int = typer.Option(15, "--limit"),
) -> None:
    """Ostatnie metryki runów (data/metrics/runs.jsonl)."""
    from market_agents.metrics import recent_metrics

    path = _resolve_config(config)
    cfg = load_config(path)
    rows = recent_metrics(cfg.data_path, limit=limit)
    if not rows:
        console.print("[yellow]Brak metryk — uruchom run.[/yellow]")
        raise typer.Exit(0)
    table = Table(title="Run metrics")
    table.add_column("ts")
    table.add_column("mode")
    table.add_column("new", justify="right")
    table.add_column("profiles", justify="right")
    table.add_column("supp", justify="right")
    table.add_column("prosp", justify="right")
    table.add_column("crm_open", justify="right")
    table.add_column("notion", justify="right")
    for row in reversed(rows):
        counts = row.get("knowledge_counts") or {}
        table.add_row(
            str(row.get("ts") or "")[:19],
            str(row.get("mode") or ""),
            str(row.get("new_items") or 0),
            str(counts.get("firm_profiles") or 0),
            str(counts.get("suppliers") or 0),
            str(counts.get("prospects") or 0),
            str(counts.get("crm_tasks_open") or 0),
            str(counts.get("crm_with_notion") or 0),
        )
    console.print(table)


@app.command("digest")
def digest_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    refresh: bool = typer.Option(True, "--refresh/--show", help="Przelicz vs pokaż ostatni"),
    min_delta: float = typer.Option(5.0, "--min-delta"),
    limit: int = typer.Option(25, "--limit"),
) -> None:
    """Zmiany scoreboardów między runami (bez LLM)."""
    from market_agents.change_digest import refresh_digest

    path = _resolve_config(config)
    cfg = load_config(path)
    if refresh:
        result = refresh_digest(cfg.data_path, min_delta=min_delta, limit=limit)
        md_path = Path(result["markdown_path"])
        console.print(md_path.read_text(encoding="utf-8"))
        console.print(f"[dim]{md_path}[/dim]")
        return
    md = Path(cfg.data_path) / "metrics" / "digest_latest.md"
    if not md.is_file():
        console.print("[yellow]Brak digest — uruchom digest --refresh lub run.[/yellow]")
        raise typer.Exit(0)
    console.print(md.read_text(encoding="utf-8"))


@app.command("export-knowledge")
def export_knowledge_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    out: Path = typer.Option(Path("reports/knowledge_export"), "--out", help="Katalog wyjściowy"),
    limit: int = typer.Option(30, "--limit"),
) -> None:
    """Eksport operatorski: scoreboards + CRM + digest → Markdown/JSON."""
    from market_agents.knowledge_export import export_knowledge_pack

    path = _resolve_config(config)
    cfg = load_config(path)
    result = export_knowledge_pack(cfg.data_path, out, limit=limit)
    console.print(result)


@app.command("social")
def social_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    platform: Optional[str] = typer.Option(None, "--platform", help="youtube|linkedin|x|facebook|instagram"),
) -> None:
    """Pokaż skonfigurowane publiczne źródła social media."""
    path = _resolve_config(config)
    cfg = load_config(path)
    from market_agents.collectors.social import detect_platform

    rows = cfg.sources.social
    if platform:
        plat = platform.lower().replace("twitter", "x")
        rows = [
            s
            for s in rows
            if (s.platform or detect_platform(s.url) or "").lower().replace("twitter", "x") == plat
        ]
    table = Table(title=f"Social sources ({len(rows)})")
    table.add_column("Name")
    table.add_column("Platform")
    table.add_column("Brand")
    table.add_column("URL")
    table.add_column("RSS")
    for s in rows:
        plat = (s.platform or detect_platform(s.url) or "other")
        rss = "yes" if (s.channel_id or s.feed_url) else "—"
        table.add_row(s.name, plat, s.brand or "—", s.url[:50], rss)
    console.print(table)
    console.print(
        "Bez logowania. YouTube: ustaw channel_id dla RSS. "
        "LinkedIn/X często zwracają tylko publiczne meta."
    )


@app.command("literature")
def literature_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    kind: Optional[str] = typer.Option(
        None, "--kind", help="book|article|video|proceedings|whitepaper"
    ),
    topic: Optional[str] = typer.Option(None, "--topic"),
    q: Optional[str] = typer.Option(None, "--q"),
    sources: bool = typer.Option(False, "--sources", help="Pokaż sources.literature z config"),
) -> None:
    """Pokaż zarejestrowaną literaturę (książki / artykuły / wideo) lub źródła."""
    path = _resolve_config(config)
    cfg = load_config(path)
    if sources:
        rows = cfg.sources.literature
        if kind:
            rows = [s for s in rows if (s.kind or "mixed").lower() == kind.lower()]
        table = Table(title=f"Literature sources ({len(rows)})")
        table.add_column("Name")
        table.add_column("Kind")
        table.add_column("Publisher")
        table.add_column("URL")
        table.add_column("Feed")
        for s in rows:
            table.add_row(
                s.name,
                s.kind or "mixed",
                s.publisher or s.brand or "—",
                s.url[:48],
                "yes" if s.feed_url else "—",
            )
        console.print(table)
        return

    from market_agents.literature import LiteratureRegistry

    registry = LiteratureRegistry.load(cfg.data_path)
    items = registry.list(kind=kind, topic=topic, q=q, limit=100)
    table = Table(title=f"Literatura ({len(items)} / {len(registry.items)})")
    table.add_column("Kind")
    table.add_column("Tytuł")
    table.add_column("Year")
    table.add_column("URL")
    for item in items:
        table.add_row(
            item.kind,
            item.title[:40],
            item.year or "—",
            item.url[:50],
        )
    console.print(table)
    console.print(
        "Notion Pozycje: Type book|paper|video → local book|article|video. "
        "Sync: tool sync_notion_literature."
    )
    console.print(f"Plik: {LiteratureRegistry.path_for(cfg.data_path)}")


@app.command("ontology")
def ontology_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    entity_type: Optional[str] = typer.Option(
        None, "--type", help="material|machine|coolant|process|tool_family|firm|standard|parameter"
    ),
    q: Optional[str] = typer.Option(None, "--q"),
    node: Optional[str] = typer.Option(None, "--node", help="Pokaż sąsiedztwo (np. process:milling)"),
    path_from: Optional[str] = typer.Option(None, "--from", help="Początek ścieżki (find_path)"),
    path_to: Optional[str] = typer.Option(None, "--to", help="Koniec ścieżki"),
    seed: bool = typer.Option(False, "--seed", help="Wgraj seed ontologii"),
    build: bool = typer.Option(
        False, "--build", help="Zbuduj graf z seed + known_firms + relations + literature"
    ),
    export: Optional[Path] = typer.Option(
        None, "--export", help="Eksport GraphML lub JSON-LD (wg rozszerzenia)"
    ),
) -> None:
    """Lokalny knowledge graph database: materiały / maszyny / chłodziwo / procesy / firmy."""
    from market_agents.ontology import MachiningOntology

    path = _resolve_config(config)
    cfg = load_config(path)
    graph = MachiningOntology.load(cfg.data_path)
    if build:
        result = graph.build_from_knowledge(cfg.data_path)
        console.print(
            f"[green]Built knowledge graph[/green]: "
            f"{result['node_count']} nodes / {result['edge_count']} edges\n"
            f"stats={result['stats']}\nby_type={result['by_type']}\n"
            f"Plik: {result['path']}"
        )
        if export:
            _export_ontology(graph, export)
        return
    if seed or len(graph.nodes) < 5:
        added = graph.merge_seed("config/machining_ontology.seed.json")
        graph.save(cfg.data_path)
        console.print(f"Seed: +{added} (nodes={len(graph.nodes)} edges={len(graph.edges)})")
    if path_from and path_to:
        found = graph.find_path(path_from, path_to)
        if not found.get("ok"):
            console.print(f"[red]{found.get('error')}[/red]")
            raise typer.Exit(code=1)
        console.print(f"[cyan]{found['readable']}[/cyan]  (len={found['length']})")
        return
    if node:
        nb = graph.neighborhood(node, depth=1)
        if not nb.get("ok"):
            console.print(f"[red]{nb.get('error')}[/red]")
            raise typer.Exit(code=1)
        table = Table(title=f"Neighborhood: {node}")
        table.add_column("Source")
        table.add_column("Relation")
        table.add_column("Target")
        for e in nb.get("edges") or []:
            table.add_row(e["source"], e["relation"], e["target"])
        console.print(table)
        console.print(f"Nodes: {nb['counts']['nodes']}  Edges: {nb['counts']['edges']}")
        if export:
            _export_ontology(graph, export)
        return
    brief = graph.domain_brief()
    nodes = graph.list_nodes(entity_type=entity_type, q=q, limit=80)
    table = Table(title=f"Ontology ({len(nodes)} / {brief['node_count']} nodes, {brief['edge_count']} edges)")
    table.add_column("Type")
    table.add_column("ID")
    table.add_column("Label")
    for n in nodes:
        table.add_row(n.type, n.id, n.label[:48])
    console.print(table)
    console.print(
        "Knowledge DB: get_domain_context / extract_domain_context / "
        "ontology_neighborhood / find_ontology_path / build_ontology."
    )
    console.print(f"Plik: {MachiningOntology.path_for(cfg.data_path)}")
    if export:
        _export_ontology(graph, export)


def _export_ontology(graph: object, export: Path) -> None:
    from market_agents.ontology import MachiningOntology

    assert isinstance(graph, MachiningOntology)
    suffix = export.suffix.lower()
    if suffix in {".jsonld", ".json"}:
        out = graph.export_jsonld(export)
    else:
        out = graph.export_graphml(export if suffix else Path(str(export) + ".graphml"))
    console.print(f"[green]Export[/green]: {out}")


@app.command("sync-ontology")
def sync_ontology_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    export_graphml: Optional[Path] = typer.Option(None, "--graphml"),
) -> None:
    """Zbuduj knowledge graph z known_firms + relations + literature + seed tech."""
    from market_agents.ontology import MachiningOntology

    path = _resolve_config(config)
    cfg = load_config(path)
    graph = MachiningOntology.load(cfg.data_path)
    result = graph.build_from_knowledge(cfg.data_path)
    console.print(
        Panel.fit(
            f"Ontology DB: [cyan]{result['node_count']}[/cyan] nodes / "
            f"[cyan]{result['edge_count']}[/cyan] edges\n"
            f"{result['stats']}\n{result['path']}"
        )
    )
    if export_graphml:
        out = graph.export_graphml(export_graphml)
        console.print(f"GraphML: {out}")


@app.command("product-tech")
def product_tech_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    brand: Optional[str] = typer.Option(None, "--brand"),
    kind: Optional[str] = typer.Option(
        None, "--kind", help="cutting_data|handbook|application_guide|iso13399|tech_datasheet|grade_chart"
    ),
    access: Optional[str] = typer.Option(None, "--access"),
    q: Optional[str] = typer.Option(None, "--q"),
) -> None:
    """Pokaż zarejestrowane źródła informacji technicznych o produktach."""
    from market_agents.product_tech import ProductTechRegistry

    path = _resolve_config(config)
    cfg = load_config(path)
    registry = ProductTechRegistry.load(cfg.data_path)
    items = registry.list(brand=brand, kind=kind, access=access, q=q, limit=100)
    table = Table(title=f"Product tech ({len(items)} / {len(registry.items)})")
    table.add_column("Marka")
    table.add_column("Kind")
    table.add_column("Tytuł")
    table.add_column("Access")
    table.add_column("URL")
    for item in items:
        table.add_row(
            item.brand or "—",
            item.kind,
            item.title[:36],
            item.access,
            item.url[:50],
        )
    console.print(table)
    console.print(
        "Polityka: schemat pól do nauki kalkulatora TIZ — bez kopiowania tabel vc/fz do CutData."
    )
    console.print(f"Plik: {ProductTechRegistry.path_for(cfg.data_path)}")


@app.command("pricelists")
def list_pricelists_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    brand: Optional[str] = typer.Option(None, "--brand"),
    access: Optional[str] = typer.Option(None, "--access", help="public|login|request|unknown"),
    q: Optional[str] = typer.Option(None, "--q", help="Filtr tekstowy"),
) -> None:
    """Pokaż zarejestrowane / odkryte dostępne cenniki konkurencji."""
    from market_agents.pricelists import PricelistRegistry

    path = _resolve_config(config)
    cfg = load_config(path)
    registry = PricelistRegistry.load(cfg.data_path)
    items = registry.list(brand=brand, access=access, q=q, limit=100)
    table = Table(title=f"Dostępne cenniki ({len(items)} / {len(registry.items)})")
    table.add_column("Marka")
    table.add_column("Tytuł")
    table.add_column("Access")
    table.add_column("URL")
    for item in items:
        table.add_row(item.brand or "—", item.title[:40], item.access, item.url[:60])
    console.print(table)
    console.print(f"Plik: {PricelistRegistry.path_for(cfg.data_path)}")


@app.command("fleet-status")
def fleet_status(config: Optional[Path] = typer.Option(None, "--config", "-c")) -> None:
    """Status floty: centrala (GPU) ↔ workery VPS."""
    from market_agents.fleet import FleetSync, fleet_config_from_app

    path = _resolve_config(config)
    cfg = load_config(path)
    sync = FleetSync(cfg.data_path, fleet_config_from_app(cfg))
    st = sync.status()
    table = Table(title="Fleet (central GPU ↔ VPS parsers)")
    table.add_column("Pole")
    table.add_column("Wartość")
    for key in (
        "role",
        "worker_id",
        "sync_dir",
        "latest_knowledge",
        "feedback_total",
        "feedback_pending",
        "auto_pull_before_run",
        "auto_push_after_run",
    ):
        table.add_row(key, str(st.get(key)))
    console.print(table)


@app.command("fleet-publish")
def fleet_publish(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    notes: str = typer.Option("", "--notes", help="Opis knowledge packa"),
) -> None:
    """Centrala: opublikuj skills/rules/crawl_health dla VPS."""
    from market_agents.fleet import FleetSync, fleet_config_from_app

    path = _resolve_config(config)
    cfg = load_config(path)
    sync = FleetSync(cfg.data_path, fleet_config_from_app(cfg))
    result = sync.publish_knowledge(notes=notes)
    if not result.get("ok"):
        console.print(f"[red]{result.get('error')}[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]Opublikowano knowledge[/green]: {result['pack_id']}\n"
        f"pliki: {result['files']}\n"
        f"path: {result['path']}"
    )


@app.command("fleet-pull")
def fleet_pull(config: Optional[Path] = typer.Option(None, "--config", "-c")) -> None:
    """Worker VPS: pobierz najnowszy knowledge pack z centrali."""
    from market_agents.fleet import FleetSync, fleet_config_from_app

    path = _resolve_config(config)
    cfg = load_config(path)
    sync = FleetSync(cfg.data_path, fleet_config_from_app(cfg))
    result = sync.pull_knowledge()
    if not result.get("ok"):
        console.print(f"[red]{result.get('error')}[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]Pobrano knowledge[/green]: {result['pack_id']} → "
        f"{cfg.data_path}/knowledge ({result['files']})"
    )


@app.command("fleet-push")
def fleet_push(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    notes: str = typer.Option("", "--notes"),
) -> None:
    """Worker VPS: wyślij crawl_health / lokalne reguły do centrali."""
    from market_agents.fleet import FleetSync, fleet_config_from_app

    path = _resolve_config(config)
    cfg = load_config(path)
    sync = FleetSync(cfg.data_path, fleet_config_from_app(cfg))
    result = sync.push_feedback(notes=notes)
    if not result.get("ok"):
        console.print(f"[red]{result.get('error')}[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]Wysłano feedback[/green]: {result['worker_id']} / {result['pack_id']}\n"
        f"pliki: {result['files']}"
    )


@app.command("fleet-absorb")
def fleet_absorb(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    limit: int = typer.Option(50, "--limit"),
    republish: bool = typer.Option(
        True,
        "--republish/--no-republish",
        help="Po absorb automatycznie opublikuj ulepszony knowledge pack",
    ),
) -> None:
    """Centrala: wchłłoń feedback z VPS i (opcjonalnie) opublikuj ulepszenia."""
    from market_agents.fleet import FleetSync, fleet_config_from_app

    path = _resolve_config(config)
    cfg = load_config(path)
    sync = FleetSync(cfg.data_path, fleet_config_from_app(cfg))
    result = sync.absorb_feedback(limit=limit)
    if not result.get("ok"):
        console.print(f"[red]{result.get('error')}[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]Absorb OK[/green]: packs={result['absorbed_packs']} "
        f"hosts={result['merged_hosts']} rules={result['merged_rules']}"
    )
    if republish:
        pub = sync.publish_knowledge(notes="auto after absorb")
        if pub.get("ok"):
            console.print(f"[green]Republish[/green]: {pub['pack_id']}")


@app.command("worker")
def worker_run(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    every_hours: Optional[float] = typer.Option(
        None, "--every-hours", help="Jeśli podane — pętla co N godzin"
    ),
) -> None:
    """
    Tryb VPS: bez LLM — tylko polite parsing/collect.
    Auto pull knowledge z centrali → collect → push feedback.
    """
    from market_agents.fleet import FleetSync, fleet_config_from_app

    path = _resolve_config(config)
    cfg = load_config(path)
    # wymuś worker semantics
    cfg.agents.fleet.role = "worker"
    if not cfg.agents.fleet.worker_id:
        cfg.agents.fleet.worker_id = None  # FleetSync wygeneruje
    sync = FleetSync(cfg.data_path, fleet_config_from_app(cfg))

    def cycle() -> None:
        console.print(Panel.fit(f"Worker cycle: [cyan]{sync.worker_id}[/cyan]"))
        if cfg.agents.fleet.auto_pull_before_run:
            pulled = sync.pull_knowledge()
            if pulled.get("ok"):
                console.print(f"pull: {pulled['pack_id']} files={pulled['files']}")
            else:
                console.print(f"[yellow]pull:[/yellow] {pulled.get('error')}")
        orch = Orchestrator(cfg)
        result = orch.run(skip_llm=True, agentic=False)
        console.print(
            f"[green]collect OK[/green]: items={result.new_items} → {result.md_path}"
        )
        if cfg.agents.fleet.auto_push_after_run:
            pushed = sync.push_feedback(notes="worker cycle")
            console.print(f"push: {pushed.get('pack_id')} files={pushed.get('files')}")

    if every_hours is None:
        cycle()
        return
    schedule.every(every_hours).hours.do(cycle)
    console.print(f"Worker schedule co {every_hours}h. Ctrl+C aby przerwać.")
    cycle()
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    app()
