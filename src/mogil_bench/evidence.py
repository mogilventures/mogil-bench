from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .trajectory import CanonicalEvent, TrajectoryError, TrajectoryUsage, parse_pi_jsonl
from .uploads import (
    DEFAULT_UPLOAD_TIMEOUT_SECONDS,
    UploadResponseError,
    http_error_diagnostic,
    is_timeout_error,
    read_json_response,
    timeout_diagnostic,
    validate_upload_timeout,
)

MAX_PATCH_BYTES = 65_536
MAX_VERIFIER_STREAM_BYTES = 8_192

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|api|key|token|secret|password)[-_][A-Za-z0-9._-]{12,}\b"),
    re.compile(r"(?i)\b([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)\s*=\s*)[^\s,;]+"),
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+"),
    re.compile(r"\bHIDDEN_VERIFIER_CANARY_[A-Za-z0-9_-]+\b"),
)
_HOST_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:root|home|Users|tmp|var/folders|etc|usr|opt|mnt|srv|run)"
    r"(?:/[A-Za-z0-9_.@+-]+)+"
)
_WORKSPACE_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:workspace|artifacts/candidate-workspace)(?:/[A-Za-z0-9_.@+-]+)+"
)
_ABSOLUTE_PATH = re.compile(r"(?<![:A-Za-z0-9_.-])/(?!/)(?:[A-Za-z0-9_.@+-]+/)*[A-Za-z0-9_.@+-]+")
_LINE_ENDING_COLON = re.compile(r"(?<=[A-Za-z]):(?=\r?\n)")
_PYTHON_CACHE_SUFFIXES = (".pyc", ".pyo")
_DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")


class EvidenceError(ValueError):
    pass


def evidence_run_id(logical_run_id: str, attempt_id: str) -> str:
    """Return a stable BlindBench run identity for one actual attempt."""
    identity = f"{logical_run_id}\0{attempt_id}".encode()
    return f"mogil-attempt-{hashlib.sha256(identity).hexdigest()}"


def redact_text(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", result
        )
    result = _HOST_PATH.sub("[HOST_PATH]", result)
    result = _WORKSPACE_PATH.sub("[WORKSPACE_PATH]", result)
    result = _ABSOLUTE_PATH.sub("[ABSOLUTE_PATH]", result)
    # BlindBench scans JSON serialization for Windows paths. A line ending in an
    # ASCII letter plus ':' serializes as e.g. ``s:\\n`` and trips that check.
    return _LINE_ENDING_COLON.sub(": ", result)


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _redact(item)
            for key, item in value.items()
            if key.lower() not in {"provider", "model", "argv", "expected_exit_code"}
        }
    return value


def _reviewer_sha256(value: str | list[dict[str, str]]) -> str:
    if isinstance(value, str):
        data = value.encode("utf-8")
    else:
        data = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _bounded_text(path: Path, limit: int) -> tuple[str, bool, str]:
    data = path.read_bytes()
    return (
        redact_text(data[:limit].decode("utf-8", errors="replace")),
        len(data) > limit,
        hashlib.sha256(data).hexdigest(),
    )


class StrictEvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskEvidence(StrictEvidenceModel):
    id: str
    revision: str
    privacy_class: str
    prompt: str


class RunEvidence(StrictEvidenceModel):
    id: str
    attempt: str
    started_at: str
    ended_at: str
    status: Literal["quality_eligible", "fixture_complete", "insufficient"]
    termination_reason: str

    @field_validator("started_at", "ended_at")
    @classmethod
    def timestamps_are_canonical_utc_iso8601(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("run timestamp must be ISO-8601") from error
        if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
            raise ValueError("run timestamp must be timezone-aware UTC")
        if parsed.astimezone(UTC).isoformat() != value:
            raise ValueError("run timestamp must use canonical UTC ISO-8601")
        return value

    @model_validator(mode="after")
    def timestamps_are_chronological(self) -> RunEvidence:
        if datetime.fromisoformat(self.ended_at) < datetime.fromisoformat(self.started_at):
            raise ValueError("run ended_at must not precede started_at")
        return self


class Outcomes(StrictEvidenceModel):
    process: str
    verifier: str
    infrastructure: str
    evidence_completeness: str


class RewardDimensions(StrictEvidenceModel):
    reward: float
    command_exit: float
    stdout_assertion: float


class EvidenceReference(StrictEvidenceModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def path_is_safe_relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("evidence reference must be a safe relative path")
        return value


class ReviewerEvidenceReference(EvidenceReference):
    reviewer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewerEvidence(StrictEvidenceModel):
    changed_files: list[dict[str, str]]
    changed_files_reference: ReviewerEvidenceReference
    patch: str
    patch_truncated: bool
    patch_reference: ReviewerEvidenceReference
    verifier_command_summary: str
    verifier_exit_code: int | None
    verifier_timed_out: bool
    verifier_stdout: str
    verifier_stderr: str
    verifier_stdout_truncated: bool
    verifier_stderr_truncated: bool
    verifier_references: list[ReviewerEvidenceReference]

    @model_validator(mode="after")
    def reviewer_hashes_bind_exact_inline_values(self) -> ReviewerEvidence:
        if self.changed_files_reference.path != "workspace/changed-files.json":
            raise ValueError("changed-files reference path is invalid")
        if self.patch_reference.path != "workspace/patch.diff":
            raise ValueError("patch reference path is invalid")
        if self.changed_files_reference.reviewer_sha256 != _reviewer_sha256(
            self.changed_files
        ):
            raise ValueError("changed-files reviewer hash mismatch")
        if self.patch_reference.reviewer_sha256 != _reviewer_sha256(self.patch):
            raise ValueError("patch reviewer hash mismatch")
        references = {reference.path: reference for reference in self.verifier_references}
        if len(references) != len(self.verifier_references) or set(references) != {
            "verifier/stdout.txt",
            "verifier/stderr.txt",
        }:
            raise ValueError("verifier reviewer references are invalid")
        if references["verifier/stdout.txt"].reviewer_sha256 != _reviewer_sha256(
            self.verifier_stdout
        ):
            raise ValueError("verifier stdout reviewer hash mismatch")
        if references["verifier/stderr.txt"].reviewer_sha256 != _reviewer_sha256(
            self.verifier_stderr
        ):
            raise ValueError("verifier stderr reviewer hash mismatch")
        return self


class ReviewerProjection(StrictEvidenceModel):
    task: TaskEvidence
    environment_class: Literal["docker", "isolated-sandbox"]
    harness_schema: Literal["harbor/pi-jsonl@0.18.0"]
    events: list[CanonicalEvent]
    outcomes: Outcomes
    rewards: RewardDimensions
    evidence: ReviewerEvidence

    @model_validator(mode="after")
    def event_integrity(self) -> ReviewerProjection:
        if [event.sequence for event in self.events] != list(range(len(self.events))):
            raise ValueError("events must have contiguous ordered sequence")
        ids = [event.id for event in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("event IDs must be unique")
        call_events = [event for event in self.events if event.kind == "tool_call"]
        result_events = [
            event for event in self.events if event.kind in {"tool_result", "tool_error"}
        ]
        call_ids = [event.call_id for event in call_events]
        result_ids = [event.call_id for event in result_events]
        if (
            None in call_ids
            or len(call_ids) != len(set(call_ids))
            or len(result_ids) != len(set(result_ids))
            or set(call_ids) != set(result_ids)
        ):
            raise ValueError("tool events must be completely and uniquely linked")
        positions = {event.call_id: event.sequence for event in call_events}
        if any(event.sequence <= positions[event.call_id] for event in result_events):
            raise ValueError("tool result must follow its linked call")
        final_outputs = [event for event in self.events if event.kind == "final_output"]
        if len(final_outputs) != 1 or final_outputs[0].stop_reason != "stop":
            raise ValueError("exactly one terminal stop final output is required")
        if (
            len(self.events) < 2
            or self.events[-2] is not final_outputs[0]
            or self.events[-1].kind != "termination"
            or self.events[-1].content != "completed"
        ):
            raise ValueError("final output and completed termination must end trajectory")
        timestamps = [
            datetime.fromisoformat(event.timestamp)
            for event in self.events
            if event.timestamp is not None
        ]
        if any(
            current < previous
            for previous, current in zip(timestamps, timestamps[1:], strict=False)
        ):
            raise ValueError("event timestamps must be monotonic")
        return self


_REQUIRED_OUTCOME = {
    "process": "succeeded",
    "verifier": "passed",
    "infrastructure": "succeeded",
    "evidence_completeness": "complete",
}


class HarborEvidence(StrictEvidenceModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    schema_name: Literal["mogil.harbor-evidence"] = Field(
        alias="schema", serialization_alias="schema"
    )
    version: Literal["1.0"]
    run: RunEvidence
    harness: dict[str, str]
    raw: EvidenceReference
    usage: TrajectoryUsage
    outcomes: Outcomes
    rewards: RewardDimensions
    analysis_metadata: dict[str, Any]
    reviewer: ReviewerProjection

    @model_validator(mode="after")
    def quality_status_is_bound_to_objective_evidence(self) -> HarborEvidence:
        if self.outcomes != self.reviewer.outcomes or self.rewards != self.reviewer.rewards:
            raise ValueError("private and reviewer objective outcomes must match")
        if self.run.status == "quality_eligible":
            if self.outcomes.model_dump() != _REQUIRED_OUTCOME:
                raise ValueError("quality eligible run requires successful complete outcomes")
            if self.rewards.model_dump() != {
                "reward": 1.0,
                "command_exit": 1.0,
                "stdout_assertion": 1.0,
            }:
                raise ValueError("quality eligible run requires full verifier rewards")
            if self.run.termination_reason != "completed":
                raise ValueError("quality eligible run requires completed termination")
        return self


def _reviewer_environment_class(bundle: Path) -> Literal["docker", "isolated-sandbox"]:
    path = bundle / "environment.json"
    if not path.is_file():
        # Backward compatibility for callers producing the pre-Daytona Docker bundle.
        return "docker"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError("invalid private environment evidence") from error
    if not isinstance(value, dict):
        raise EvidenceError("invalid private environment evidence")
    provider = value.get("provider", value.get("type"))
    if provider == "docker":
        return "docker"
    if provider == "daytona":
        return "isolated-sandbox"
    raise EvidenceError("unsupported private environment provider")


def build_harbor_evidence(
    bundle: Path,
    *,
    run_id: str,
    attempt_id: str,
    task: dict[str, str],
    outcomes: dict[str, str],
    analysis_metadata: dict[str, Any],
    termination_reason: str,
    fixture: bool = False,
) -> HarborEvidence:
    """Build the strict private artifact and its separately blinded projection."""
    try:
        raw_path = bundle / "agent/pi.txt"
        raw = raw_path.read_bytes()
        trajectory = parse_pi_jsonl(raw, run_id=run_id)
        changed_value = json.loads(
            (bundle / "workspace/changed-files.json").read_text(encoding="utf-8")
        )
        if not isinstance(changed_value, list) or any(
            not isinstance(item, dict) for item in changed_value
        ):
            raise EvidenceError("changed-files evidence must be a list")
        changed_files: list[dict[str, str]] = []
        for item in changed_value:
            path, changed_status = item.get("path"), item.get("status")
            if not isinstance(path, str) or not isinstance(changed_status, str):
                raise EvidenceError("invalid changed-files evidence")
            changed_files.append({"path": redact_text(path), "status": changed_status})
        patch, patch_truncated, patch_hash = _bounded_text(
            bundle / "workspace/patch.diff", MAX_PATCH_BYTES
        )
        verification = json.loads(
            (bundle / "verifier/verification.json").read_text(encoding="utf-8")
        )
        if not isinstance(verification, dict):
            raise EvidenceError("invalid verifier evidence")
        rewards = RewardDimensions.model_validate_json(
            (bundle / "verifier/reward.json").read_text(encoding="utf-8")
        )
        stdout, stdout_truncated, stdout_hash = _bounded_text(
            bundle / "verifier/stdout.txt", MAX_VERIFIER_STREAM_BYTES
        )
        stderr, stderr_truncated, stderr_hash = _bounded_text(
            bundle / "verifier/stderr.txt", MAX_VERIFIER_STREAM_BYTES
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TrajectoryError,
        ValidationError,
    ) as error:
        raise EvidenceError(
            f"cannot build complete Harbor evidence: {type(error).__name__}"
        ) from error

    parsed_outcomes = Outcomes.model_validate(outcomes)
    status: Literal["quality_eligible", "fixture_complete", "insufficient"]
    complete_outcomes = outcomes == _REQUIRED_OUTCOME
    if complete_outcomes and trajectory.complete:
        status = "fixture_complete" if fixture else "quality_eligible"
    else:
        status = "insufficient"
    reviewer_events = [
        CanonicalEvent.model_validate(_redact(event.model_dump(mode="json")))
        for event in trajectory.events
    ]
    task_evidence = TaskEvidence.model_validate({**task, "prompt": redact_text(task["prompt"])})
    references = [
        ReviewerEvidenceReference(
            path="verifier/stdout.txt",
            sha256=stdout_hash,
            reviewer_sha256=_reviewer_sha256(stdout),
        ),
        ReviewerEvidenceReference(
            path="verifier/stderr.txt",
            sha256=stderr_hash,
            reviewer_sha256=_reviewer_sha256(stderr),
        ),
    ]
    reviewer_evidence = ReviewerEvidence(
        changed_files=changed_files,
        changed_files_reference=ReviewerEvidenceReference(
            path="workspace/changed-files.json",
            sha256=hashlib.sha256(
                (bundle / "workspace/changed-files.json").read_bytes()
            ).hexdigest(),
            reviewer_sha256=_reviewer_sha256(changed_files),
        ),
        patch=patch,
        patch_truncated=patch_truncated,
        patch_reference=ReviewerEvidenceReference(
            path="workspace/patch.diff",
            sha256=patch_hash,
            reviewer_sha256=_reviewer_sha256(patch),
        ),
        verifier_command_summary="hidden deterministic verifier",
        verifier_exit_code=verification.get("exit_code")
        if isinstance(verification.get("exit_code"), int)
        else None,
        verifier_timed_out=verification.get("timed_out") is True,
        verifier_stdout=stdout,
        verifier_stderr=stderr,
        verifier_stdout_truncated=stdout_truncated or verification.get("stdout_truncated") is True,
        verifier_stderr_truncated=stderr_truncated or verification.get("stderr_truncated") is True,
        verifier_references=references,
    )
    reviewer = ReviewerProjection(
        task=task_evidence,
        environment_class=_reviewer_environment_class(bundle),
        harness_schema="harbor/pi-jsonl@0.18.0",
        events=reviewer_events,
        outcomes=parsed_outcomes,
        rewards=rewards,
        evidence=reviewer_evidence,
    )
    return HarborEvidence(
        schema="mogil.harbor-evidence",
        version="1.0",
        run=RunEvidence(
            id=run_id,
            attempt=attempt_id,
            started_at=trajectory.session_timestamp,
            ended_at=(
                str(verification["ended_at"])
                if isinstance(verification.get("ended_at"), str)
                else trajectory.session_timestamp
            ),
            status=status,
            termination_reason=termination_reason,
        ),
        harness={"name": "harbor/pi", "schema": "0.18.0"},
        raw=EvidenceReference(path="agent/pi.txt", sha256=trajectory.raw_sha256),
        usage=trajectory.usage,
        outcomes=parsed_outcomes,
        rewards=rewards,
        analysis_metadata=analysis_metadata,
        reviewer=reviewer,
    )


def _generated_python_cache_path(value: str) -> bool:
    path = PurePosixPath(value)
    return "__pycache__" in path.parts or path.name.endswith(_PYTHON_CACHE_SUFFIXES)


def _sanitize_reviewer_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_sanitize_reviewer_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_reviewer_value(item) for key, item in value.items()}
    return value


def _reviewer_safe_patch(value: str) -> str:
    blocks = re.split(r"(?=^diff --git )", value, flags=re.MULTILINE)
    retained: list[str] = []
    for block in blocks:
        first_line = block.splitlines()[0] if block else ""
        header = _DIFF_HEADER.fullmatch(first_line)
        if header and any(_generated_python_cache_path(path) for path in header.groups()):
            continue
        retained.append(block)
    return redact_text("".join(retained))


def _bundle_from_manifest(run_dir: Path, reference_value: object) -> Path:
    if not isinstance(reference_value, str):
        raise EvidenceError("manifest bundle reference must be a string")
    reference = Path(reference_value)
    if reference.is_absolute() or not reference.parts or any(
        part in {"", ".", ".."} for part in reference.parts
    ):
        raise EvidenceError("manifest bundle reference must be a safe relative path")
    root = run_dir.resolve(strict=True)
    current = run_dir
    for part in reference.parts:
        current /= part
        if current.is_symlink():
            raise EvidenceError("manifest bundle path must not contain symlinks")
    try:
        bundle = current.resolve(strict=True)
    except OSError as error:
        raise EvidenceError("manifest bundle path cannot be resolved") from error
    if not bundle.is_relative_to(root):
        raise EvidenceError("manifest bundle path escapes run root")
    return bundle


def _write_staged_file(run_dir: Path, *, prefix: str, data: bytes) -> Path:
    descriptor, temporary = tempfile.mkstemp(dir=run_dir, prefix=prefix, suffix=".tmp")
    path = Path(temporary)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _replace_evidence_pair(
    run_dir: Path,
    json_bytes: bytes,
    jsonl_bytes: bytes,
    *,
    replace_file: Callable[[Path, Path], object] = os.replace,
) -> tuple[Path, Path]:
    """Replace both files with rollback on observed errors; not crash-atomic as a pair."""
    destinations = (
        run_dir / "mogil.harbor-evidence.json",
        run_dir / "mogil.harbor-evidence.jsonl",
    )
    staged: list[Path] = []
    backups: dict[Path, Path | None] = {}
    try:
        for destination, data in zip(destinations, (json_bytes, jsonl_bytes), strict=True):
            staged.append(
                _write_staged_file(
                    run_dir,
                    prefix=f".{destination.name}.new.",
                    data=data,
                )
            )
            backups[destination] = (
                _write_staged_file(
                    run_dir,
                    prefix=f".{destination.name}.backup.",
                    data=destination.read_bytes(),
                )
                if destination.exists()
                else None
            )
        try:
            for staged_path, destination in zip(staged, destinations, strict=True):
                replace_file(staged_path, destination)
        except OSError as error:
            rollback_errors: list[OSError] = []
            for destination in destinations:
                backup = backups[destination]
                try:
                    if backup is None:
                        destination.unlink(missing_ok=True)
                    else:
                        replace_file(backup, destination)
                except OSError as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                raise EvidenceError(
                    "aggregate replacement failed and prior pair could not be fully restored"
                ) from error
            raise EvidenceError(
                "aggregate replacement failed; prior destination bytes and absence restored"
            ) from error
        return destinations
    finally:
        for path in (*staged, *(backup for backup in backups.values() if backup is not None)):
            path.unlink(missing_ok=True)


def reexport_harbor_evidence(run_dir: Path) -> tuple[Path, Path]:
    """Rebuild aggregates with validated staging and rollback on replacement errors."""
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError("cannot read re-export manifest") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("results"), list):
        raise EvidenceError("re-export manifest has an invalid shape")
    results = manifest["results"]
    expected = manifest.get("result_count")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected != len(results):
        raise EvidenceError("re-export manifest count does not match results")
    if expected < 1:
        raise EvidenceError("re-export manifest count must be positive")

    from .run_bundle import validate_checksums

    artifacts: list[HarborEvidence] = []
    manifest_attempts: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            raise EvidenceError("re-export manifest result is invalid")
        logical_run_id = result.get("logical_run_id")
        attempt_id = result.get("attempt_id")
        task_id = result.get("task_id")
        if (
            not isinstance(logical_run_id, str)
            or not logical_run_id
            or not isinstance(attempt_id, str)
            or not attempt_id
            or attempt_id in manifest_attempts
            or not isinstance(task_id, str)
            or not task_id
        ):
            raise EvidenceError("re-export manifest identity is invalid")
        manifest_attempts.add(attempt_id)
        bundle = _bundle_from_manifest(run_dir, result.get("bundle"))
        if not validate_checksums(bundle):
            raise EvidenceError("retained bundle checksum validation failed")
        private_path = bundle / "mogil.harbor-evidence.json"
        if validate_evidence_artifact(private_path) != 1:
            raise EvidenceError("retained bundle evidence validation failed")
        try:
            artifact = HarborEvidence.model_validate_json(
                private_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValidationError) as error:
            raise EvidenceError("retained bundle evidence is invalid") from error
        desired_run_id = evidence_run_id(logical_run_id, attempt_id)
        retained_logical = artifact.analysis_metadata.get("logical_run_id")
        if (
            artifact.run.attempt != attempt_id
            or artifact.run.id not in {logical_run_id, desired_run_id}
            or (retained_logical is not None and retained_logical != logical_run_id)
            or artifact.reviewer.task.id != task_id
        ):
            raise EvidenceError("retained bundle identity does not match manifest")

        value = artifact.model_dump(mode="json", by_alias=True, exclude_none=True)
        value["run"]["id"] = desired_run_id
        value["analysis_metadata"]["logical_run_id"] = logical_run_id
        value["reviewer"] = _sanitize_reviewer_value(value["reviewer"])
        reviewer_evidence = value["reviewer"]["evidence"]
        changed_files = [
            item
            for item in reviewer_evidence["changed_files"]
            if not _generated_python_cache_path(item["path"])
        ]
        patch = _reviewer_safe_patch(reviewer_evidence["patch"])
        reviewer_evidence["changed_files"] = changed_files
        reviewer_evidence["changed_files_reference"]["reviewer_sha256"] = (
            _reviewer_sha256(changed_files)
        )
        reviewer_evidence["patch"] = patch
        reviewer_evidence["patch_reference"]["reviewer_sha256"] = _reviewer_sha256(patch)
        verifier_values = {
            "verifier/stdout.txt": reviewer_evidence["verifier_stdout"],
            "verifier/stderr.txt": reviewer_evidence["verifier_stderr"],
        }
        for reference in reviewer_evidence["verifier_references"]:
            reference["reviewer_sha256"] = _reviewer_sha256(
                verifier_values[reference["path"]]
            )
        artifacts.append(HarborEvidence.model_validate(value))

    values = [
        artifact.model_dump(mode="json", by_alias=True, exclude_none=True)
        for artifact in artifacts
    ]
    if (
        len(values) != expected
        or len({artifact.run.id for artifact in artifacts}) != expected
        or len({artifact.run.attempt for artifact in artifacts}) != expected
    ):
        raise EvidenceError("re-export count or identity validation failed")
    json_bytes = (
        json.dumps(values, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    jsonl_bytes = "".join(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
        for value in values
    ).encode("utf-8")

    with tempfile.TemporaryDirectory(dir=run_dir, prefix=".evidence-validate-") as temporary:
        staged_json = Path(temporary) / "mogil.harbor-evidence.json"
        staged_jsonl = Path(temporary) / "mogil.harbor-evidence.jsonl"
        staged_json.write_bytes(json_bytes)
        staged_jsonl.write_bytes(jsonl_bytes)
        if (
            validate_evidence_artifact(staged_json) != expected
            or validate_evidence_artifact(staged_jsonl) != expected
        ):
            raise EvidenceError("re-export aggregate validation failed")
    return _replace_evidence_pair(run_dir, json_bytes, jsonl_bytes)


def validate_evidence_endpoint(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    loopback = False
    if parsed.hostname:
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
    secure = parsed.scheme == "https" or (parsed.scheme == "http" and loopback)
    if (
        not secure
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/ingest/v1/eval-runs"
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise EvidenceError(
            "endpoint must use HTTPS (or HTTP loopback) and exactly /ingest/v1/eval-runs"
        )


def validate_evidence_ingest_counts(payload: Any, expected: int) -> dict[str, int]:
    if not isinstance(payload, dict):
        raise EvidenceError("ingest returned an invalid complete-count response")
    required = ("complete", "imported", "deduped", "invalid")
    if any(
        not isinstance(payload.get(key), int) or isinstance(payload.get(key), bool)
        for key in required
    ):
        raise EvidenceError("ingest complete-count response is missing integer fields")
    counts = {key: int(payload[key]) for key in required}
    if any(value < 0 for value in counts.values()):
        raise EvidenceError("ingest complete counts cannot be negative")
    if counts["invalid"] != 0:
        raise EvidenceError("ingest rejected one or more evidence artifacts")
    if counts["complete"] != expected or counts["imported"] + counts["deduped"] != expected:
        raise EvidenceError(
            f"ingest complete count does not match the intended artifact count ({expected})"
        )
    return counts


def upload_evidence_artifact(
    path: Path,
    endpoint: str,
    token: str,
    *,
    confirm: bool,
    timeout: float = DEFAULT_UPLOAD_TIMEOUT_SECONDS,
) -> dict[str, int] | None:
    try:
        validated_timeout = validate_upload_timeout(timeout)
    except ValueError as error:
        raise EvidenceError(str(error)) from error
    validate_evidence_endpoint(endpoint)
    expected = validate_evidence_artifact(path)
    if not confirm:
        return None
    if not token:
        raise EvidenceError("project Automation token is required with --confirm")
    if path.suffix == ".jsonl":
        artifacts = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        ]
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
        artifacts = value if isinstance(value, list) else [value]
    body = json.dumps({"runs": artifacts}, separators=(",", ":")).encode()
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(  # noqa: S310 - endpoint validated above
            request, timeout=validated_timeout
        ) as response:
            payload: Any = read_json_response(response)
    except HTTPError as error:
        diagnostic = http_error_diagnostic(error, body=body, token=token)
        raise EvidenceError(f"evidence upload failed: {diagnostic}") from error
    except (TimeoutError, URLError) as error:
        if is_timeout_error(error):
            raise EvidenceError(timeout_diagnostic("evidence upload")) from error
        raise EvidenceError(f"evidence upload failed: {type(error).__name__}") from error
    except UploadResponseError as error:
        raise EvidenceError(f"evidence upload failed: {error}") from error
    except OSError as error:
        raise EvidenceError(f"evidence upload failed: {type(error).__name__}") from error
    return validate_evidence_ingest_counts(payload, expected)


def validate_evidence_artifact(path: Path) -> int:
    try:
        if path.suffix == ".jsonl":
            lines = path.read_text(encoding="utf-8").splitlines()
            if not lines or any(not line.strip() for line in lines):
                raise EvidenceError("JSONL must contain one non-empty object per line")
            values = [json.loads(line) for line in lines]
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
            values = value if isinstance(value, list) else [value]
        if not values:
            raise EvidenceError("artifact is empty")
        artifacts = [HarborEvidence.model_validate(value) for value in values]
        run_ids = {artifact.run.id for artifact in artifacts}
        attempts = {artifact.run.attempt for artifact in artifacts}
        if len(run_ids) != len(artifacts):
            raise EvidenceError("artifact contains duplicate run ids")
        if len(attempts) != len(artifacts):
            raise EvidenceError("artifact contains duplicate attempts")
        return len(artifacts)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError(
            f"invalid mogil.harbor-evidence artifact: {type(error).__name__}"
        ) from error
