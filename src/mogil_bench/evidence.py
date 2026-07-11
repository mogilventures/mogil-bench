from __future__ import annotations

import hashlib
import ipaddress
import json
import re
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


class EvidenceError(ValueError):
    pass


def redact_text(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", result
        )
    result = _HOST_PATH.sub("[HOST_PATH]", result)
    result = _WORKSPACE_PATH.sub("[WORKSPACE_PATH]", result)
    return _ABSOLUTE_PATH.sub("[ABSOLUTE_PATH]", result)


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
    environment_class: Literal["docker"]
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
        environment_class="docker",
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
    path: Path, endpoint: str, token: str, *, confirm: bool
) -> dict[str, int] | None:
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
        with urlopen(request, timeout=30) as response:  # noqa: S310 - endpoint validated above
            payload: Any = json.loads(response.read())
    except (HTTPError, URLError, json.JSONDecodeError, OSError) as error:
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
        if len({artifact.run.id for artifact in artifacts}) != len(artifacts):
            raise EvidenceError("artifact contains duplicate run IDs")
        return len(artifacts)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError(
            f"invalid mogil.harbor-evidence artifact: {type(error).__name__}"
        ) from error
