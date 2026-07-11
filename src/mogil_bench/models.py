from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

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
    adapter: Literal["mock", "command", "pi"]
    harness: Harness

    @field_validator("provider", "model")
    @classmethod
    def safe_cli_value(cls, value: str) -> str:
        if value.startswith("-") or any(character in value for character in ("\x00", "\n", "\r")):
            raise ValueError("provider and model must be safe CLI values")
        return value


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
        if any(c.adapter == "pi" for c in self.configurations) and not self.allow_agents:
            raise ValueError("pi adapter requires pack allow_agents: true")
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


class BlindBenchBatch(StrictModel):
    records: list[BlindBenchRecord]
