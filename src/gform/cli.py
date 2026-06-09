"""Command-line interface: import / preview / run."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from .config import (
    Config,
    ConfigError,
    load_config,
    scaffold_dict,
    validate_against_schema,
    validate_internal,
)
from .generator import generate
from .importer import FormAccessError, import_form
from .submitter import submit_all

app = typer.Typer(
    add_completion=False,
    help="Deterministic Google Forms survey-response generator (for forms you own).",
)
console = Console()


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
@app.command("import")
def import_cmd(
    url: str = typer.Argument(..., help="Public Google Form viewform URL."),
    output: Path = typer.Option(Path("config.yaml"), "-o", "--output"),
    count: int = typer.Option(1000, "--count", help="Default response count to scaffold."),
    seed: int = typer.Option(42, "--seed"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config."),
):
    """Fetch a form and scaffold an editable config with uniform distributions."""
    schema = _import_or_exit(url)
    _print_schema(schema)
    for warning in schema.warnings:
        console.print(f"[yellow]! {warning}[/yellow]")
    if not schema.questions:
        console.print("[red]No supported multiple-choice questions found.[/red]")
        raise typer.Exit(1)

    if output.exists() and not force:
        console.print(f"[red]{output} already exists. Use --force to overwrite.[/red]")
        raise typer.Exit(1)

    data = scaffold_dict(schema, count, seed)
    with open(output, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
    console.print(
        f"[green]Wrote {output}[/green]  →  edit the distributions, then run "
        f"[bold]gform preview {output}[/bold]"
    )


@app.command("preview")
def preview_cmd(
    config: Path = typer.Argument(..., help="Path to the config YAML."),
    rows: int = typer.Option(10, "--rows", help="How many sample rows to show."),
):
    """Generate offline and show target vs. realized distributions + sample rows."""
    cfg = _load_or_exit(config)
    _validate_internal_or_exit(cfg)
    responses = generate(cfg)
    _print_marginals(cfg, responses)
    _print_sample(cfg, responses, rows)
    console.print(
        f"[dim]Generated {len(responses)} responses (seed {cfg.generation.seed}, "
        f"mode {cfg.generation.mode}). No data submitted.[/dim]"
    )


@app.command("run")
def run_cmd(
    config: Path = typer.Argument(..., help="Path to the config YAML."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Build payloads without submitting."),
    yes: bool = typer.Option(False, "--yes", help="Skip the interactive ownership prompt."),
    log: Path = typer.Option(None, "--log", help="Audit CSV path (default run-<seed>.csv)."),
):
    """Generate and submit responses to the live form (ownership required)."""
    cfg = _load_or_exit(config)
    _validate_internal_or_exit(cfg)

    # We must hit the live form to (a) confirm it is accessible, (b) validate
    # option labels match exactly, and (c) capture a fresh fbzx token.
    schema = _import_or_exit(cfg.form.url)
    try:
        validate_against_schema(cfg, schema)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    responses = generate(cfg)

    if dry_run:
        result = submit_all(
            responses,
            cfg,
            fbzx=schema.fbzx,
            dry_run=True,
            log_path=str(log) if log else None,
            form_url=schema.url,
        )
        console.print(f"[green]Dry run: built {len(result)} payloads, none submitted.[/green]")
        console.print(f"[dim]Submit endpoint:[/dim] {schema.url.split('?')[0].replace('/viewform', '/formResponse')}")
        if result:
            console.print("[dim]First payload:[/dim]")
            console.print(result[0]["payload"])
        return

    if not (cfg.submission.enabled and cfg.submission.i_own_this_form):
        console.print(
            "[red]Submission blocked.[/red] Set [bold]submission.enabled: true[/bold] "
            "and [bold]submission.i_own_this_form: true[/bold] in the config."
        )
        raise typer.Exit(1)

    console.print(
        f"[bold]About to submit {len(responses)} responses[/bold] to:\n  {schema.url}"
    )
    if not yes:
        answer = typer.prompt("Type YES to confirm you own / are authorized to test this form")
        if answer.strip() != "YES":
            console.print("Aborted.")
            raise typer.Exit(1)

    log_path = log or Path(f"run-{cfg.generation.seed}.csv")

    def progress(i, status, _payload):
        console.log(f"#{i} -> {status}")

    rows_out = submit_all(
        responses,
        cfg,
        fbzx=schema.fbzx,
        log_path=str(log_path),
        on_progress=progress,
        form_url=schema.url,
    )
    ok = sum(1 for r in rows_out if r["success"] is True)
    color = "green" if ok == len(rows_out) else "yellow"
    console.print(f"[{color}]Submitted {ok}/{len(rows_out)} successfully.[/{color}]  Log: {log_path}")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _import_or_exit(url: str):
    try:
        return import_form(url)
    except FormAccessError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    except Exception as exc:  # network/HTTP errors
        console.print(f"[red]Failed to fetch form: {exc}[/red]")
        raise typer.Exit(1)


def _load_or_exit(path: Path) -> Config:
    try:
        return load_config(path)
    except FileNotFoundError:
        console.print(f"[red]Config not found: {path}[/red]")
        raise typer.Exit(1)
    except Exception as exc:
        console.print(f"[red]Invalid config: {exc}[/red]")
        raise typer.Exit(1)


def _validate_internal_or_exit(cfg: Config) -> None:
    try:
        validate_internal(cfg)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


def _print_schema(schema) -> None:
    table = Table(title=f"Form: {schema.title or '(untitled)'}")
    table.add_column("entry_id")
    table.add_column("type")
    table.add_column("req")
    table.add_column("title")
    table.add_column("options")
    for q in schema.questions:
        table.add_row(
            q.entry_id, q.type.value, "✓" if q.required else "", q.title, ", ".join(q.options)
        )
    console.print(table)


def _print_marginals(cfg: Config, responses) -> None:
    n = len(responses)
    for qc in cfg.questions:
        table = Table(title=f"{qc.title or qc.entry_id}  ({qc.type.value})")
        table.add_column("option")
        table.add_column("target", justify="right")
        table.add_column("count", justify="right")
        table.add_column("actual", justify="right")
        counter: Counter = Counter()
        for r in responses:
            value = r.answers[qc.entry_id]
            if isinstance(value, list):
                counter.update(value)
            else:
                counter[value] += 1
        for label, p in qc.distribution.items():
            count = counter.get(label, 0)
            pct = (count / n * 100) if n else 0.0
            table.add_row(label, f"{p * 100:.1f}%", str(count), f"{pct:.1f}%")
        console.print(table)


def _print_sample(cfg: Config, responses, rows: int) -> None:
    shown = min(rows, len(responses))
    table = Table(title=f"Sample rows (first {shown} of {len(responses)})")
    table.add_column("#")
    for qc in cfg.questions:
        table.add_column(qc.title or qc.entry_id)
    for i, r in enumerate(responses[:shown]):
        cells = [str(i)]
        for qc in cfg.questions:
            value = r.answers[qc.entry_id]
            cells.append(", ".join(value) if isinstance(value, list) else str(value))
        table.add_row(*cells)
    console.print(table)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
