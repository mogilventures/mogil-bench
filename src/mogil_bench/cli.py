from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from .artifacts import ArtifactError, export_run, upload_artifact, validate_artifact
from .packs import PackError, load_pack, pack_fingerprint
from .runner import run_pack

app = typer.Typer(help="Run safe local benchmark packs and emit BlindBench artifacts.")
pack_app = typer.Typer(help="Inspect and validate benchmark packs.")
artifact_app = typer.Typer(help="Validate or upload BlindBench artifacts.")
export_app = typer.Typer(help="Export run artifacts.")
app.add_typer(pack_app, name="pack")
app.add_typer(export_app, name="export")
app.add_typer(artifact_app, name="artifact")


def _fail(message: str) -> None:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


@pack_app.command("list")
def list_packs(directory: Annotated[Path, typer.Argument()] = Path("packs")) -> None:
    """List valid YAML packs in DIRECTORY."""
    paths = sorted((*directory.glob("*.yaml"), *directory.glob("*.yml")))
    if not paths:
        _fail(f"no packs found in {directory}")
    for path in paths:
        try:
            pack = load_pack(path)
            typer.echo(
                f"{pack.id}\trevision={pack.revision}\ttasks={len(pack.tasks)}\tconfigs={len(pack.configurations)}\t{path}"
            )
        except PackError as error:
            typer.echo(f"INVALID\t{path}\t{error}")


@pack_app.command("validate")
def validate_pack(path: Path) -> None:
    """Validate a pack and all referenced fixtures."""
    try:
        pack = load_pack(path)
        fingerprint = pack_fingerprint(path, pack)
    except PackError as error:
        _fail(str(error))
    typer.echo(f"valid pack {pack.id}@{pack.revision} fingerprint={fingerprint}")


@app.command("run")
def run(
    pack_path: Path,
    output_dir: Annotated[Path | None, typer.Option("--output-dir", "-o")] = None,
    allow_commands: Annotated[
        bool,
        typer.Option(
            "--allow-commands", help="Acknowledge execution of pack-approved argv commands."
        ),
    ] = False,
    allow_agents: Annotated[
        bool,
        typer.Option(
            "--allow-agents", help="Acknowledge execution of pack-approved Pi agent runs."
        ),
    ] = False,
) -> None:
    """Run every task/configuration pair in PACK_PATH."""
    destination = output_dir or Path("runs") / (
        f"{pack_path.stem}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    try:
        run_pack(
            pack_path,
            destination,
            allow_commands=allow_commands,
            allow_agents=allow_agents,
        )
    except (PackError, PermissionError, FileExistsError, OSError) as error:
        _fail(str(error))
    typer.echo(f"run written to {destination}")


@export_app.command("blindbench")
def export_blindbench(run_dir: Path) -> None:
    """Rebuild BlindBench JSON and JSONL files for RUN_DIR."""
    try:
        json_path, jsonl_path = export_run(run_dir)
    except (ArtifactError, OSError, KeyError) as error:
        _fail(str(error))
    typer.echo(f"wrote {json_path} and {jsonl_path}")


@artifact_app.command("validate")
def artifact_validate(path: Path) -> None:
    """Validate a BlindBench batch JSON or JSONL artifact."""
    try:
        count = validate_artifact(path)
    except ArtifactError as error:
        _fail(str(error))
    typer.echo(f"valid eval-record v1 artifact: {count} record(s)")


@artifact_app.command("upload")
def artifact_upload(
    path: Path,
    endpoint: Annotated[str, typer.Option("--endpoint")],
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Perform upload; otherwise dry-run.")
    ] = False,
) -> None:
    """Dry-run or explicitly upload a batch to a guarded BlindBench endpoint."""
    token = os.environ.get("BLINDBENCH_INGEST_TOKEN", "")
    try:
        counts = upload_artifact(path, endpoint, token, confirm=confirm)
    except ArtifactError as error:
        _fail(str(error))
    if counts is None:
        typer.echo("dry-run valid; no network request made (pass --confirm to upload)")
    else:
        typer.echo(
            "upload counts: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        )


if __name__ == "__main__":
    app()
