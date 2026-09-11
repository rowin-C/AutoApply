"""AutoApply CLI.

aa login linkedin          once, manually
aa search                  phase A: capture listings
aa fetch-details           phase B: JD + apply-path classification
aa qualify                 rules-based scoring -> queued/skipped
aa apply                   Step 2: submit Easy Apply listings (auto-filled)
aa daily                   whole pipeline in one shot + Telegram report
aa review                  ranked review queue
aa test-telegram           verify the Telegram channel
aa install-service         install the daily systemd --user service
"""

from __future__ import annotations

import functools
from pathlib import Path

import typer
from rich.console import Console

from . import __version__
from .apply import run_apply
from .browser import bootstrap_login
from .db import init_db
from .errors import AccountBlocked, AutoApplyError, CaptchaDetected, LoginRequired
from .notify.telegram import send_message
from .notify.telegram import test_telegram as send_test_telegram
from .pipeline.daily import format_daily_report, run_daily
from .pipeline.phase_a import run_phase_a
from .pipeline.phase_b import run_phase_b
from .pipeline.qualify import run_qualify
from .pipeline.review import render_review
from .settings import CONFIG_DIR, LOG_DIR, ensure_dirs, load_settings
from .sources import known_sources
from .utils.log import setup_logging

app = typer.Typer(
    add_completion=False, help="AutoApply — AI-assisted job application discovery."
)
console = Console()


@app.callback()
def _main(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Debug logging to console."
    ),
) -> None:
    ensure_dirs()
    setup_logging(LOG_DIR, level="DEBUG" if verbose else "INFO")


def _require_source(source: str) -> None:
    if source not in known_sources():
        raise typer.BadParameter(
            f"Unknown source '{source}'. Have: {', '.join(known_sources())}"
        )


def _guard(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (LoginRequired, CaptchaDetected, AccountBlocked) as exc:
            console.print(f"[bold red]BLOCKED[/bold red] {exc}", style="red")
            raise typer.Exit(code=3) from exc
        except AutoApplyError as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")
            raise typer.Exit(code=2) from exc

    return wrapper


@app.command()
@_guard
def login(
    source: str = typer.Argument("linkedin", help="Source to authenticate (linkedin)."),
) -> None:
    """Open a visible browser to log into a source once; session is saved."""
    _require_source(source)
    bootstrap_login(source)


@app.command("search")
@_guard
def search(
    source: str = typer.Option("linkedin", help="Source adapter to run."),
    headed: bool = typer.Option(False, "--headed", help="Run with a visible browser."),
) -> None:
    """Phase A: capture listings from configured searches into the DB."""
    _require_source(source)
    settings = load_settings()
    init_db()
    report = run_phase_a(settings, headless=not headed)
    console.print(
        f"[green]Capture done:[/green] seen={report.seen} new={report.new} "
        f"updated={report.updated}"
    )


@app.command("fetch-details")
@_guard
def fetch_details(
    limit: int | None = typer.Option(
        None, "--limit", help="Max detail fetches this run."
    ),
    headed: bool = typer.Option(False, "--headed", help="Run with a visible browser."),
) -> None:
    """Phase B: fetch JDs and classify apply paths for new listings."""
    settings = load_settings()
    init_db()
    report = run_phase_b(settings, headless=not headed, limit=limit)
    console.print(
        f"[green]Detail pass done:[/green] fetched={report.fetched} "
        f"guarded={report.guarded} errors={report.errors} budget_left={report.budget_left}"
    )


@app.command()
@_guard
def qualify() -> None:
    """Score DETAILED listings against profile rules; queue or skip them."""
    settings = load_settings()
    init_db()
    queued, skipped = run_qualify(settings)
    console.print(f"[green]Qualify done:[/green] queued={queued} skipped={skipped}")


@app.command()
def review(
    status: str | None = typer.Option(
        None, "--status", help="Filter by listing status."
    ),
    min_score: float | None = typer.Option(
        None, "--min-score", help="Minimum match score."
    ),
    apply_path: str | None = typer.Option(
        None, "--apply-path", help="inline | redirect"
    ),
    source: str | None = typer.Option(None, "--source"),
) -> None:
    """Show the ranked review queue."""
    init_db()
    render_review(
        status=status, min_score=min_score, apply_path=apply_path, source=source
    )


@app.command()
@_guard
def apply(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview the flow; never submits anything."
    ),
    limit: int | None = typer.Option(
        None, "--limit", help="Max Easy Apply submissions this run."
    ),
    headed: bool = typer.Option(False, "--headed", help="Run with a visible browser."),
) -> None:
    """Step 2: submit queued inline (Easy Apply) listings within daily budget."""
    settings = load_settings()
    init_db()
    report = run_apply(settings, headless=not headed, dry_run=dry_run, limit=limit)
    mode = "DRY-RUN preview" if dry_run else "Apply run"
    console.print(
        f"[green]{mode} done:[/green] applied={report.applied} "
        f"unknown={report.unknown} guarded={report.guarded} errors={report.errors} "
        f"budget_left={report.budget_left}"
    )
    if report.unknown_fields:
        for company, fields in report.unknown_fields:
            console.print(
                f"[yellow]  needs input[/yellow] {company}: {', '.join(fields)}"
            )


@app.command()
def knowledge() -> None:
    """Show the accumulated Easy Apply field knowledge base."""
    from .knowledge import load_knowledge, render

    console.print(render(load_knowledge()))


@app.command()
@_guard
def daily(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Capture/detail/qualify + preview applies only."
    ),
    headed: bool = typer.Option(False, "--headed", help="Run with a visible browser."),
    notify: bool = typer.Option(
        True, "--notify/--no-notify", help="Send the Telegram report."
    ),
) -> None:
    """Full daily pipeline: search -> fetch-details -> qualify -> apply -> notify."""
    settings = load_settings()
    report = run_daily(settings, headed=headed, dry_run=dry_run)
    text = format_daily_report(report, settings)
    console.print(text)
    if notify and settings.telegram.enabled:
        ok = send_message(text, settings.telegram)
        console.print(
            "[green]Telegram report sent[/green]" if ok else
            "[yellow]Telegram report NOT sent (see log)[/yellow]"
        )


@app.command()
def test_telegram() -> None:
    """Send a probe message via the configured Telegram bot."""
    settings = load_settings()
    if not settings.telegram.enabled:
        console.print(
            "[yellow]telegram is disabled in config/telegram.yaml — "
            "set enabled: true and fill bot_token + chat_id.[/yellow]"
        )
        raise typer.Exit(code=1)
    if send_test_telegram(settings.telegram):
        console.print("[green]Telegram test message sent ✓[/green]")
    else:
        console.print("[red]Telegram test failed — check token/chat_id and network.[/red]")
        raise typer.Exit(code=1)


_SERVICE_TEMPLATE = """\
[Unit]
Description=AutoApply daily LinkedIn run
After=network-online.target

[Service]
Type=oneshot
Environment=AUTOAPPLY_CONFIG_DIR={config_dir}
Environment=AUTOAPPLY_DATA_DIR={data_dir}
WorkingDirectory={project_root}
ExecStart={venv}/aa daily
TimeoutStopSec=600

[Install]
WantedBy=default.target
"""


@app.command("install-service")
def install_service() -> None:
    """Install the daily run as a systemd --user service (runs at login)."""
    project_root = Path(__file__).resolve().parent.parent
    venv = project_root / ".venv" / "bin"
    unit = _SERVICE_TEMPLATE.format(
        config_dir=CONFIG_DIR,
        data_dir=project_root / "data",
        project_root=project_root,
        venv=venv,
    )
    user_unit_dir = Path.home() / ".config" / "systemd" / "user"
    user_unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = user_unit_dir / "autoapply.service"
    unit_path.write_text(unit, encoding="utf-8")
    console.print(f"Wrote {unit_path}")
    for cmd in ("systemctl --user daemon-reload", "systemctl --user enable autoapply.service"):
        result = _run_systemctl(cmd)
        console.print(f"[{'green' if result == 0 else 'red'}]{cmd}[/{'green' if result == 0 else 'red'}]")
    console.print(
        "The service runs at every login. Control it with:\n"
        "  systemctl --user start autoapply.service    # run now\n"
        "  systemctl --user status autoapply.service   # view status\n"
        "  journalctl --user -u autoapply -f           # watch logs\n"
        "  systemctl --user disable autoapply.service  # stop auto-running"
    )


def _run_systemctl(cmd: str) -> int:
    import shutil
    import subprocess

    if shutil.which("systemctl") is None:
        console.print("[red]systemctl not found — is this systemd?[/red]")
        return 1
    return subprocess.run(cmd.split(), capture_output=True, check=False).returncode


@app.command()
def version() -> None:
    """Show version."""
    console.print(f"autoapply {__version__}")


if __name__ == "__main__":
    app()
