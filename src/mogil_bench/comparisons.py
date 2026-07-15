from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .evidence import (
    EvidenceError,
    HarborEvidence,
    evidence_run_id,
    redact_text,
    validate_evidence_artifact,
)

_BLINDBENCH_PRIVACY_CLASSES = {"public", "internal", "confidential", "pii", "phi"}
_BLINDBENCH_MAX_ROWS = 1_000

_HEADERS = (
    "case_id",
    "context",
    "candidate_a",
    "candidate_b",
    "candidate_a_model",
    "candidate_b_model",
    "candidate_a_harness",
    "candidate_b_harness",
    "product",
    "environment",
    "privacy_class",
    "segment",
)


class ComparisonExportError(ValueError):
    pass


@dataclass(frozen=True)
class _Cell:
    task_id: str
    configuration_id: str
    attempt_number: int
    logical_run_id: str
    attempt_id: str
    provider: str
    model: str
    harness: dict[str, Any]
    prompt: str
    privacy_class: str
    task_revision: str
    artifact: HarborEvidence


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ComparisonExportError(f"cannot read {path.name}") from error
    if not isinstance(value, dict):
        raise ComparisonExportError(f"{path.name} must contain a JSON object")
    return value


def _read_evidence_file(path: Path) -> list[HarborEvidence]:
    try:
        expected = validate_evidence_artifact(path)
        if path.suffix == ".jsonl":
            values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
            values = value if isinstance(value, list) else [value]
        artifacts = [HarborEvidence.model_validate(value) for value in values]
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, EvidenceError) as error:
        raise ComparisonExportError("aggregate Harbor evidence validation failed") from error
    if len(artifacts) != expected:
        raise ComparisonExportError("aggregate Harbor evidence count changed during validation")
    return artifacts


def _read_evidence(run_dir: Path) -> list[HarborEvidence]:
    paths = [
        path
        for path in (
            run_dir / "mogil.harbor-evidence.jsonl",
            run_dir / "mogil.harbor-evidence.json",
        )
        if path.is_file()
    ]
    if not paths:
        raise ComparisonExportError("validated aggregate Harbor evidence is absent")
    aggregates = [_read_evidence_file(path) for path in paths]
    canonical = [
        {
            (artifact.run.id, artifact.run.attempt): artifact.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
            for artifact in aggregate
        }
        for aggregate in aggregates
    ]
    if any(value != canonical[0] for value in canonical[1:]):
        raise ComparisonExportError("aggregate Harbor evidence files do not match")
    return aggregates[0]


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ComparisonExportError(f"manifest {label} must be a non-empty string")
    return value


def _required_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ComparisonExportError(f"manifest {label} must be an object")
    return value


def _reviewer_safe(value: str, label: str) -> str:
    if not value.strip() or redact_text(value) != value:
        raise ComparisonExportError(f"reviewer-visible {label} is empty or not safely redacted")
    return value


def _provider_qualified_model(provider: str, model: str) -> str:
    prefix = f"{provider}/"
    return model if model.startswith(prefix) else prefix + model


def _harness_identifier(harness: dict[str, Any]) -> str:
    if set(harness) - {"name", "version", "sdk"}:
        raise ComparisonExportError("harness provenance contains unsupported fields")
    name = harness.get("name")
    version = harness.get("version")
    sdk = harness.get("sdk")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(version, str)
        or not version
        or (sdk is not None and (not isinstance(sdk, str) or not sdk))
    ):
        raise ComparisonExportError("harness provenance is incomplete")
    identifier = f"{name}@{version}"
    return identifier if sdk is None else f"{identifier} ({sdk})"


def _validated_cells(
    run_dir: Path, *, candidate_a: str, candidate_b: str
) -> dict[tuple[str, str, int], _Cell]:
    if not candidate_a or not candidate_b or candidate_a == candidate_b:
        raise ComparisonExportError("candidate A and B configuration IDs must be distinct")
    manifest = _read_object(run_dir / "manifest.json")
    results = manifest.get("results")
    result_count = manifest.get("result_count")
    if (
        not isinstance(results, list)
        or not isinstance(result_count, int)
        or isinstance(result_count, bool)
        or result_count != len(results)
        or result_count < 2
    ):
        raise ComparisonExportError("manifest result count does not match a completed run")

    artifacts = _read_evidence(run_dir)
    if len(artifacts) != result_count:
        raise ComparisonExportError("manifest and aggregate evidence counts do not match")
    evidence_by_identity = {(item.run.id, item.run.attempt): item for item in artifacts}
    if len(evidence_by_identity) != len(artifacts):
        raise ComparisonExportError("aggregate evidence identities are duplicated")

    cells: dict[tuple[str, str, int], _Cell] = {}
    seen_evidence: set[tuple[str, str]] = set()
    configurations: set[str] = set()
    logical_by_arm: dict[tuple[str, str], str] = {}
    for raw in results:
        if not isinstance(raw, dict):
            raise ComparisonExportError("manifest result must be an object")
        task_id = _required_string(raw.get("task_id"), "task_id")
        configuration_id = _required_string(raw.get("configuration_id"), "configuration_id")
        configurations.add(configuration_id)
        attempt_number = raw.get("attempt_number")
        if (
            not isinstance(attempt_number, int)
            or isinstance(attempt_number, bool)
            or attempt_number < 1
        ):
            raise ComparisonExportError("manifest attempt_number must be a positive integer")
        logical_run_id = _required_string(raw.get("logical_run_id"), "logical_run_id")
        attempt_id = _required_string(raw.get("attempt_id"), "attempt_id")
        provider = _required_string(raw.get("provider"), "provider")
        model = _required_string(raw.get("model"), "model")
        harness = _required_mapping(raw.get("harness"), "harness")
        prompt = _required_string(raw.get("prompt"), "prompt")
        privacy_class = _required_string(raw.get("privacy_class"), "privacy_class")
        if privacy_class not in _BLINDBENCH_PRIVACY_CLASSES:
            raise ComparisonExportError("manifest privacy_class is not BlindBench-compatible")
        key = (task_id, configuration_id, attempt_number)
        if key in cells:
            raise ComparisonExportError(
                "manifest contains a duplicate task/configuration/attempt cell"
            )
        arm = (task_id, configuration_id)
        if arm in logical_by_arm and logical_by_arm[arm] != logical_run_id:
            raise ComparisonExportError("logical run identity is unstable within a matrix arm")
        logical_by_arm[arm] = logical_run_id

        identity = (evidence_run_id(logical_run_id, attempt_id), attempt_id)
        artifact = evidence_by_identity.get(identity)
        if artifact is None or identity in seen_evidence:
            raise ComparisonExportError(
                "manifest attempt identity does not match aggregate evidence"
            )
        seen_evidence.add(identity)
        metadata = artifact.analysis_metadata
        task_revision = artifact.reviewer.task.revision
        if not task_revision:
            raise ComparisonExportError("quality evidence task revision is absent")
        if (
            artifact.run.status != "quality_eligible"
            or artifact.run.termination_reason != "completed"
            or metadata.get("logical_run_id") != logical_run_id
            or metadata.get("provider") != provider
            or metadata.get("model") != model
            or metadata.get("harness") != harness
            or artifact.reviewer.task.id != task_id
            or artifact.reviewer.task.prompt != prompt
            or artifact.reviewer.task.privacy_class != privacy_class
        ):
            raise ComparisonExportError("manifest and quality-eligible evidence cell do not match")
        cells[key] = _Cell(
            task_id=task_id,
            configuration_id=configuration_id,
            attempt_number=attempt_number,
            logical_run_id=logical_run_id,
            attempt_id=attempt_id,
            provider=provider,
            model=model,
            harness=harness,
            prompt=prompt,
            privacy_class=privacy_class,
            task_revision=task_revision,
            artifact=artifact,
        )

    if configurations != {candidate_a, candidate_b}:
        raise ComparisonExportError(
            "completed comparison run must contain exactly the requested two arms"
        )
    if len(seen_evidence) != len(artifacts):
        raise ComparisonExportError("aggregate evidence contains attempts absent from the manifest")

    tasks = {key[0] for key in cells}
    expected_attempts: set[int] | None = None
    for task_id in tasks:
        revisions = {
            cell.task_revision for cell in cells.values() if cell.task_id == task_id
        }
        if len(revisions) != 1:
            raise ComparisonExportError(
                "immutable task revision is unstable across comparison attempts"
            )
        for configuration_id in (candidate_a, candidate_b):
            attempts = {
                attempt
                for task, configuration, attempt in cells
                if task == task_id and configuration == configuration_id
            }
            if not attempts or attempts != set(range(1, max(attempts) + 1)):
                raise ComparisonExportError(
                    "comparison matrix attempts must be complete and contiguous"
                )
            if expected_attempts is None:
                expected_attempts = attempts
            elif attempts != expected_attempts:
                raise ComparisonExportError("comparison matrix cells have mismatched attempt sets")
    return cells


def _final_output(cell: _Cell) -> str:
    outputs = [
        event.content
        for event in cell.artifact.reviewer.events
        if event.kind == "final_output"
    ]
    if len(outputs) != 1 or not isinstance(outputs[0], str):
        raise ComparisonExportError("quality evidence must contain exactly one text final output")
    return _reviewer_safe(outputs[0], "candidate output")


def _comparison_bytes(
    cells: dict[tuple[str, str, int], _Cell], *, candidate_a: str, candidate_b: str
) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=_HEADERS, lineterminator="\n")
    writer.writeheader()
    tasks = sorted({key[0] for key in cells})
    for task_id in tasks:
        attempts = sorted(
            attempt
            for task, configuration, attempt in cells
            if task == task_id and configuration == candidate_a
        )
        for attempt in attempts:
            arm_a = cells[(task_id, candidate_a, attempt)]
            arm_b = cells[(task_id, candidate_b, attempt)]
            if (
                arm_a.prompt != arm_b.prompt
                or arm_a.privacy_class != arm_b.privacy_class
                or arm_a.task_revision != arm_b.task_revision
                or arm_a.artifact.reviewer.environment_class
                != arm_b.artifact.reviewer.environment_class
            ):
                raise ComparisonExportError(
                    "paired cells have mismatched reviewer context or identity"
                )
            writer.writerow(
                {
                    "case_id": f"{task_id}--attempt-{attempt:02d}",
                    "context": _reviewer_safe(arm_a.prompt, "context"),
                    "candidate_a": _final_output(arm_a),
                    "candidate_b": _final_output(arm_b),
                    "candidate_a_model": _provider_qualified_model(
                        arm_a.provider, arm_a.model
                    ),
                    "candidate_b_model": _provider_qualified_model(
                        arm_b.provider, arm_b.model
                    ),
                    "candidate_a_harness": _harness_identifier(arm_a.harness),
                    "candidate_b_harness": _harness_identifier(arm_b.harness),
                    "product": "mogil-bench",
                    "environment": arm_a.artifact.reviewer.environment_class,
                    "privacy_class": arm_a.privacy_class,
                    "segment": task_id,
                }
            )
    return stream.getvalue().encode("utf-8")


def _validate_csv(data: bytes, expected_rows: int) -> None:
    try:
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"), newline="")))
    except (UnicodeError, csv.Error) as error:
        raise ComparisonExportError("generated paired comparison CSV is invalid") from error
    if (
        not rows
        or len(rows) != expected_rows
        or len(rows) > _BLINDBENCH_MAX_ROWS
        or tuple(rows[0]) != _HEADERS
        or any(None in row or any(value is None for value in row.values()) for row in rows)
        or len({row["case_id"] for row in rows}) != expected_rows
        or any(
            not row[name]
            for row in rows
            for name in ("case_id", "context", "candidate_a", "candidate_b")
        )
    ):
        raise ComparisonExportError("generated paired comparison CSV failed validation")


def _staged_file(directory: Path, prefix: str, data: bytes) -> Path:
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
    path = Path(temporary)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _publish(
    output_path: Path,
    data: bytes,
    *,
    replace_file: Callable[[Path, Path], object],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staged = _staged_file(output_path.parent, f".{output_path.name}.new.", data)
    backup: Path | None = None
    try:
        if output_path.exists():
            backup = _staged_file(
                output_path.parent,
                f".{output_path.name}.backup.",
                output_path.read_bytes(),
            )
        try:
            replace_file(staged, output_path)
        except OSError as error:
            try:
                if backup is None:
                    output_path.unlink(missing_ok=True)
                else:
                    replace_file(backup, output_path)
            except OSError as rollback_error:
                raise ComparisonExportError(
                    "comparison publication failed and the prior destination could not be restored"
                ) from rollback_error
            raise ComparisonExportError(
                "comparison publication failed; prior destination bytes or absence restored"
            ) from error
    finally:
        staged.unlink(missing_ok=True)
        if backup is not None:
            backup.unlink(missing_ok=True)


def export_paired_comparison(
    run_dir: Path,
    *,
    candidate_a: str,
    candidate_b: str,
    output_path: Path,
    replace_file: Callable[[Path, Path], object] = os.replace,
) -> Path:
    """Validate and deterministically export one completed two-arm run as paired CSV."""
    cells = _validated_cells(run_dir, candidate_a=candidate_a, candidate_b=candidate_b)
    data = _comparison_bytes(cells, candidate_a=candidate_a, candidate_b=candidate_b)
    _validate_csv(data, len(cells) // 2)
    _publish(output_path, data, replace_file=replace_file)
    return output_path
