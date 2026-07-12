from __future__ import annotations

import re
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrivacyClass(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    PII = "pii"
    PHI = "phi"


class Lane(StrEnum):
    HERMES_TEXT = "hermes-text"
    PI_CODING = "pi-coding"


class Backend(StrEnum):
    HARBOR = "harbor"


class EnvironmentType(StrEnum):
    DOCKER = "docker"
    DAYTONA = "daytona"


class NetworkMode(StrEnum):
    NO_NETWORK = "no-network"
    ALLOWLIST = "allowlist"


class SandboxPolicy(StrictModel):
    """Provider-neutral requested sandbox policy parsed from a pack."""

    image: str = Field(pattern=r"^[^\s]+@sha256:[0-9a-f]{64}$")
    cpus: int = Field(ge=1, le=64)
    memory_mb: int = Field(ge=1024, le=262144, multiple_of=1024)
    storage_mb: int = Field(ge=4096, le=1048576, multiple_of=1024)
    network_mode: NetworkMode
    allowed_hosts: list[str] = Field(default_factory=list, max_length=64)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    max_lifetime_minutes: int = Field(default=120, ge=5, le=1440)

    @model_validator(mode="after")
    def validate_network_and_secrets(self) -> SandboxPolicy:
        if self.network_mode == NetworkMode.NO_NETWORK and self.allowed_hosts:
            raise ValueError("allowed_hosts requires network_mode=allowlist")
        if self.network_mode == NetworkMode.ALLOWLIST and not self.allowed_hosts:
            raise ValueError("allowlist network policy requires allowed_hosts")
        if self.secret_refs and self.network_mode != NetworkMode.ALLOWLIST:
            raise ValueError("secret references require restricted allowlist networking")
        safe_name = re.compile(r"^[A-Z][A-Z0-9_]*$")
        safe_ref = re.compile(r"^ref:[a-zA-Z0-9][a-zA-Z0-9._-]*$")
        if any(not safe_name.fullmatch(key) for key in self.secret_refs):
            raise ValueError("secret reference keys must be environment variable names")
        if any(not safe_ref.fullmatch(value) for value in self.secret_refs.values()):
            raise ValueError("secret references must use ref:<organization-secret-name>")
        if any(
            not host
            or len(host) > 253
            or "://" in host
            or "/" in host
            or "@" in host
            for host in self.allowed_hosts
        ):
            raise ValueError("allowed_hosts must contain hostnames, not URLs or credentials")
        return self


class EvidenceStatus(StrEnum):
    NON_QUALITY = "non_quality"
    INSUFFICIENT = "insufficient"
    FIXTURE_COMPLETE = "fixture_complete"
    QUALITY_ELIGIBLE = "quality_eligible"


class AgentOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class VerifierOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    NOT_RUN = "not_run"


class InfrastructureOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CleanupStatus(StrEnum):
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class AttemptIdentity(StrictModel):
    logical_run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)


def create_attempt_identity(
    logical_run_id: str,
    *,
    attempt_id_factory: Callable[[], object] = uuid4,
) -> AttemptIdentity:
    return AttemptIdentity(
        logical_run_id=logical_run_id,
        attempt_id=str(attempt_id_factory()),
    )


def create_numbered_attempt_identity(
    logical_run_id: str, attempt_number: int
) -> AttemptIdentity:
    if attempt_number < 1:
        raise ValueError("attempt number must be positive")
    return AttemptIdentity(
        logical_run_id=logical_run_id,
        attempt_id=str(
            uuid5(NAMESPACE_URL, f"mogil-bench:{logical_run_id}:attempt:{attempt_number}")
        ),
    )


class CleanupEvidence(StrictModel):
    status: CleanupStatus
    requested: bool
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None


class Verifier(StrictModel):
    argv: list[str] = Field(min_length=1)
    timeout_seconds: float = Field(default=10, gt=0, le=300)
    expected_exit_code: int = 0
    stdout_contains: str | None = None


class Task(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    category: str = Field(min_length=1)
    lane: Lane
    prompt: str | None = None
    fixture: str | None = None
    privacy_class: PrivacyClass = PrivacyClass.INTERNAL
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    command: list[str] | None = None
    verifier: Verifier | None = None

    @model_validator(mode="after")
    def has_input(self) -> Task:
        if not self.prompt and not self.fixture:
            raise ValueError("task requires prompt or fixture")
        if self.command is not None and not self.command:
            raise ValueError("command argv must not be empty")
        return self


class Harness(StrictModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sdk: str | None = None


class Configuration(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    adapter: Literal["mock", "command", "pi", "harbor"]
    harness: Harness
    backend: Backend | None = None
    environment_type: EnvironmentType | None = None
    environment_policy: SandboxPolicy | None = None
    mounts: list[dict[str, str]] = Field(default_factory=list)

    @field_validator("provider", "model")
    @classmethod
    def safe_cli_value(cls, value: str) -> str:
        if value.startswith("-") or any(character in value for character in ("\x00", "\n", "\r")):
            raise ValueError("provider and model must be safe CLI values")
        return value

    @model_validator(mode="after")
    def validate_backend(self) -> Configuration:
        if self.adapter == "harbor":
            self.backend = self.backend or Backend.HARBOR
            self.environment_type = self.environment_type or EnvironmentType.DOCKER
            if self.mounts:
                raise ValueError("Harbor configurations require empty mounts")
            if self.environment_type == EnvironmentType.DAYTONA:
                if self.environment_policy is None:
                    raise ValueError("Daytona requires an explicit pinned environment_policy")
                if not self.environment_policy.secret_refs:
                    raise ValueError("Daytona requires restricted secret_refs")
        elif (
            self.backend is not None
            or self.environment_type is not None
            or self.environment_policy is not None
            or self.mounts
        ):
            raise ValueError(
                "backend, environment_type, environment_policy, and mounts require adapter=harbor"
            )
        return self


class Pack(StrictModel):
    version: Literal["1"]
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    revision: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    allow_commands: bool = False
    allow_agents: bool = False
    tasks: list[Task] = Field(min_length=1)
    configurations: list[Configuration] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids_and_command_opt_in(self) -> Pack:
        for label, values in (("task", self.tasks), ("configuration", self.configurations)):
            ids = [value.id for value in values]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {label} id")
        if any(c.adapter == "command" for c in self.configurations) and not self.allow_commands:
            raise ValueError("command adapter requires pack allow_commands: true")
        agent_configs = [c for c in self.configurations if c.adapter in {"pi", "harbor"}]
        if agent_configs and not self.allow_agents:
            raise ValueError("pi and harbor adapters require pack allow_agents: true")
        if any(c.adapter == "harbor" for c in self.configurations) and any(
            task.lane != Lane.PI_CODING for task in self.tasks
        ):
            raise ValueError("harbor adapter is valid only for the pi-coding lane")
        return self


class Message(StrictModel):
    role: str
    content: str


class RecordInput(StrictModel):
    messages: list[Message] = Field(min_length=1)


class ToolCall(StrictModel):
    id: str | None = None
    name: str = Field(min_length=1)
    arguments: dict[str, Any]


class ToolResult(StrictModel):
    tool_call_id: str = Field(min_length=1)
    name: str | None = None
    result: Any | None = None


class RecordOutput(StrictModel):
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_results: list[ToolResult] | None = None


class Usage(StrictModel):
    input_tokens: StrictFloat | None = None
    output_tokens: StrictFloat | None = None
    total_tokens: StrictFloat | None = None
    cost_usd: StrictFloat | None = None
    duration_ms: StrictFloat | None = None


class BlindBenchRecord(StrictModel):
    version: Literal["1"]
    id: str = Field(min_length=1)
    timestamp: str = Field(min_length=1)
    model: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    input: RecordInput
    output: RecordOutput | None = None
    usage: Usage | None = None
    product: str | None = None
    module: str | None = None
    environment: str | None = None
    harness: Harness | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    privacy_class: PrivacyClass

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_iso8601(cls, value: str) -> str:
        from datetime import datetime

        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value


class HarborRunRecord(StrictModel):
    bundle_version: Literal["1"]
    logical_run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    harbor_version: str = Field(min_length=1)
    pi_version: str = Field(min_length=1)
    agent_log_format: str = Field(min_length=1)
    agent_outcome: AgentOutcome
    verifier_outcome: VerifierOutcome
    infrastructure_outcome: InfrastructureOutcome
    evidence_status: EvidenceStatus
    harbor_job_id: str | None = None
    harbor_trial_id: str | None = None
    trial_name: str | None = None
    trial_uri: str | None = None
    task_checksum: str | None = None
    error: str | None = None
    artifact_collection_error: str | None = None


class BlindBenchBatch(StrictModel):
    records: list[BlindBenchRecord]
