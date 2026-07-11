from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TrajectoryError(ValueError):
    """The retained Pi stream cannot prove a complete, linked trajectory."""


EventKind = Literal[
    "user_message",
    "assistant_message",
    "assistant_reasoning",
    "tool_call",
    "tool_result",
    "tool_error",
    "final_output",
    "termination",
]


class CanonicalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^evt-[0-9a-f]{32}$")
    sequence: int = Field(ge=0)
    kind: EventKind
    timestamp: str | None = None
    content: str | None = None
    call_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    result: Any | None = None
    stop_reason: str | None = None

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_canonical_utc_iso8601(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("event timestamp must be ISO-8601") from error
        if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
            raise ValueError("event timestamp must be UTC")
        if parsed.astimezone(UTC).isoformat() != value:
            raise ValueError("event timestamp must use canonical UTC ISO-8601")
        return value

    @model_validator(mode="after")
    def fields_match_kind(self) -> CanonicalEvent:
        message_kinds = {
            "user_message",
            "assistant_message",
            "assistant_reasoning",
            "final_output",
            "termination",
        }
        if self.kind in message_kinds:
            if self.content is None or any(
                value is not None
                for value in (self.call_id, self.tool_name, self.arguments, self.result)
            ):
                raise ValueError("message event fields do not match kind")
            assistant_kinds = {"assistant_message", "assistant_reasoning", "final_output"}
            if self.kind in assistant_kinds and not self.stop_reason:
                raise ValueError("assistant event requires stop reason")
            if self.kind not in assistant_kinds and self.stop_reason is not None:
                raise ValueError("non-assistant event cannot have stop reason")
        elif self.kind == "tool_call":
            if (
                self.call_id is None
                or self.tool_name is None
                or self.arguments is None
                or self.content is not None
                or self.result is not None
                or self.stop_reason is not None
            ):
                raise ValueError("tool call event fields do not match kind")
        elif (
            self.call_id is None
            or self.tool_name is None
            or self.content is not None
            or self.arguments is not None
            or self.stop_reason is not None
        ):
            raise ValueError("tool result event fields do not match kind")
        return self


class TrajectoryUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class CanonicalTrajectory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_format: Literal["harbor-0.18.0-pi-jsonl-filtered-message-updates"]
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_size_bytes: int = Field(ge=0)
    session_id: str
    session_timestamp: str
    events: list[CanonicalEvent]
    usage: TrajectoryUsage
    complete: Literal[True]


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TrajectoryError(f"invalid Pi {label}: expected object")
    return value


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise TrajectoryError("invalid Pi message content")
    parts: list[str] = []
    for block in content:
        item = _object(block, "content block")
        if item.get("type") == "text":
            value = item.get("text")
            if not isinstance(value, str):
                raise TrajectoryError("invalid Pi text block")
            parts.append(value)
    return "".join(parts)


def _timestamp(value: object) -> tuple[str, datetime] | None:
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            raise ValueError("boolean timestamp")
        if isinstance(value, (int, float)):
            numeric = float(value)
            if not math.isfinite(numeric) or numeric <= 0:
                raise ValueError("non-positive or non-finite timestamp")
            # Pi's AgentMessage.timestamp is Date.now() epoch milliseconds. Accept
            # epoch seconds as a compatibility boundary for older emitters.
            seconds = numeric / 1000 if numeric >= 100_000_000_000 else numeric
            parsed = datetime.fromtimestamp(seconds, UTC)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timestamp lacks timezone")
            parsed = parsed.astimezone(UTC)
        else:
            raise ValueError("unsupported timestamp type")
    except (OSError, OverflowError, ValueError) as error:
        raise TrajectoryError("invalid Pi timestamp") from error
    return parsed.isoformat(), parsed


def parse_pi_jsonl(raw: bytes, *, run_id: str) -> CanonicalTrajectory:
    """Normalize Harbor's byte-preserved Pi JSONL, rejecting partial evidence."""
    if not raw or not raw.endswith(b"\n"):
        raise TrajectoryError("Pi JSONL is empty or truncated")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            raise TrajectoryError(f"invalid empty Pi JSON line {line_number}")
        try:
            decoded = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TrajectoryError(f"invalid Pi JSON line {line_number}") from error
        rows.append(_object(decoded, f"event line {line_number}"))
    if not rows or rows[0].get("type") != "session":
        raise TrajectoryError("Pi stream is missing session header")
    session_id = rows[0].get("id")
    if not isinstance(session_id, str) or not session_id:
        raise TrajectoryError("Pi session header is missing id")
    normalized_session_timestamp = _timestamp(rows[0].get("timestamp"))
    if normalized_session_timestamp is None:
        raise TrajectoryError("Pi session header is missing timestamp")
    session_timestamp, last_timestamp = normalized_session_timestamp

    pending_message_role: str | None = None
    pending_message_timestamp: tuple[str, datetime] | None = None
    declared_calls: dict[str, tuple[str, dict[str, Any]]] = {}
    started_calls: set[str] = set()
    ended_calls: set[str] = set()
    call_errors: dict[str, bool] = {}
    tool_messages: set[str] = set()
    provisional: list[dict[str, Any]] = []
    agent_started = False
    agent_ended = False
    agent_settled = False
    totals = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    usage_seen = False
    cost = 0.0

    def append(kind: EventKind, **values: Any) -> None:
        provisional.append({"kind": kind, **values})

    ignored = {
        "turn_start",
        "turn_end",
        "queue_update",
        "compaction_start",
        "compaction_end",
        "auto_retry_start",
        "auto_retry_end",
        "tool_execution_update",
        "message_update",
    }
    for row in rows[1:]:
        if agent_settled:
            raise TrajectoryError("Pi events cannot follow agent_settled")
        kind = row.get("type")
        if kind == "agent_start":
            if agent_started or agent_ended:
                raise TrajectoryError("invalid Pi agent lifecycle")
            agent_started = True
        elif kind == "agent_end":
            if not agent_started or agent_ended or agent_settled:
                raise TrajectoryError("invalid Pi agent termination")
            agent_ended = True
        elif kind == "agent_settled":
            if not agent_ended or agent_settled:
                raise TrajectoryError("invalid Pi settled lifecycle")
            agent_settled = True
        elif kind == "message_start":
            message = _object(row.get("message"), "message_start")
            role = message.get("role")
            if pending_message_role is not None or not isinstance(role, str):
                raise TrajectoryError("invalid Pi message lifecycle")
            pending_message_role = role
            pending_message_timestamp = _timestamp(message.get("timestamp"))
        elif kind == "message_end":
            message = _object(row.get("message"), "message_end")
            role = message.get("role")
            if role != pending_message_role:
                raise TrajectoryError("incomplete Pi message linkage")
            ended_timestamp = _timestamp(message.get("timestamp"))
            if (
                pending_message_timestamp is not None
                and ended_timestamp is not None
                and pending_message_timestamp[1] != ended_timestamp[1]
            ):
                raise TrajectoryError("inconsistent Pi message timestamp linkage")
            normalized_timestamp = ended_timestamp or pending_message_timestamp
            pending_message_role = None
            pending_message_timestamp = None
            timestamp = normalized_timestamp[0] if normalized_timestamp is not None else None
            if normalized_timestamp is not None:
                if normalized_timestamp[1] < last_timestamp:
                    raise TrajectoryError("Pi timestamps must be monotonic")
                last_timestamp = normalized_timestamp[1]
            if role == "user":
                append("user_message", content=_text(message.get("content")), timestamp=timestamp)
            elif role == "assistant":
                content = message.get("content")
                stop_reason = message.get("stopReason")
                if not isinstance(content, list) or not isinstance(stop_reason, str):
                    raise TrajectoryError("invalid Pi assistant content or stop reason")
                text_parts: list[str] = []
                for block_value in content:
                    block = _object(block_value, "assistant content block")
                    block_type = block.get("type")
                    if block_type == "thinking":
                        thinking = block.get("thinking")
                        if not isinstance(thinking, str):
                            raise TrajectoryError("invalid Pi reasoning block")
                        append(
                            "assistant_reasoning",
                            content=thinking,
                            timestamp=timestamp,
                            stop_reason=stop_reason,
                        )
                    elif block_type == "text":
                        text = block.get("text")
                        if not isinstance(text, str):
                            raise TrajectoryError("invalid Pi text block")
                        if text:
                            text_parts.append(text)
                    elif block_type == "toolCall":
                        call_id, name, arguments = (
                            block.get("id"),
                            block.get("name"),
                            block.get("arguments"),
                        )
                        if (
                            not isinstance(call_id, str)
                            or not isinstance(name, str)
                            or not isinstance(arguments, dict)
                            or call_id in declared_calls
                        ):
                            raise TrajectoryError("invalid or duplicate Pi tool call")
                        declared_calls[call_id] = (name, arguments)
                    else:
                        raise TrajectoryError(f"unsupported Pi assistant block: {block_type!r}")
                if text_parts:
                    append(
                        "assistant_message",
                        content="".join(text_parts),
                        timestamp=timestamp,
                        stop_reason=stop_reason,
                    )
                usage = message.get("usage")
                if usage is not None:
                    usage_object = _object(usage, "usage")
                    for key in totals:
                        value = usage_object.get(key, 0)
                        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                            raise TrajectoryError("invalid Pi usage")
                        totals[key] += value
                    cost_object = usage_object.get("cost") or {}
                    value = _object(cost_object, "cost").get("total", 0)
                    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                        raise TrajectoryError("invalid Pi cost")
                    cost += float(value)
                    usage_seen = True
            elif role == "toolResult":
                call_id = message.get("toolCallId")
                if not isinstance(call_id, str) or call_id not in ended_calls:
                    raise TrajectoryError("incomplete Pi tool result linkage")
                if message.get("isError") is not call_errors[call_id]:
                    raise TrajectoryError("inconsistent Pi tool result linkage")
                tool_messages.add(call_id)
            else:
                raise TrajectoryError(f"unsupported Pi message role: {role!r}")
        elif kind == "tool_execution_start":
            call_id, name, arguments = row.get("toolCallId"), row.get("toolName"), row.get("args")
            if (
                not isinstance(call_id, str)
                or call_id not in declared_calls
                or call_id in started_calls
            ):
                raise TrajectoryError("incomplete Pi tool call linkage")
            if declared_calls[call_id] != (name, arguments):
                raise TrajectoryError("inconsistent Pi tool call linkage")
            started_calls.add(call_id)
            append("tool_call", call_id=call_id, tool_name=name, arguments=arguments)
        elif kind == "tool_execution_end":
            call_id, name = row.get("toolCallId"), row.get("toolName")
            if (
                not isinstance(call_id, str)
                or call_id not in started_calls
                or call_id in ended_calls
                or declared_calls[call_id][0] != name
            ):
                raise TrajectoryError("incomplete Pi tool result linkage")
            is_error = row.get("isError")
            if not isinstance(is_error, bool):
                raise TrajectoryError("invalid Pi tool result status")
            ended_calls.add(call_id)
            call_errors[call_id] = is_error
            append(
                "tool_error" if is_error else "tool_result",
                call_id=call_id,
                tool_name=name,
                result=row.get("result"),
            )
        elif kind in ignored:
            continue
        else:
            raise TrajectoryError(f"unsupported Pi event type: {kind!r}")

    if (
        not agent_started
        or not agent_ended
        or not agent_settled
        or pending_message_role is not None
    ):
        raise TrajectoryError("Pi stream is missing complete termination")
    if (
        set(declared_calls) != started_calls
        or started_calls != ended_calls
        or ended_calls != tool_messages
    ):
        raise TrajectoryError("incomplete Pi tool linkage")
    terminal_indexes = [
        index
        for index, event in enumerate(provisional)
        if event["kind"] == "assistant_message" and event.get("stop_reason") == "stop"
    ]
    if not terminal_indexes:
        raise TrajectoryError("Pi stream is missing terminal assistant stop response")
    if len(terminal_indexes) != 1:
        raise TrajectoryError("Pi stream requires exactly one terminal assistant stop response")
    terminal_index = terminal_indexes[0]
    if terminal_index != len(provisional) - 1:
        raise TrajectoryError("terminal assistant stop response must follow all tool evidence")
    provisional[terminal_index]["kind"] = "final_output"
    append("termination", content="completed")

    events: list[CanonicalEvent] = []
    for sequence, event in enumerate(provisional):
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        event_id = (
            "evt-" + hashlib.sha256(f"{run_id}:{sequence}:{canonical}".encode()).hexdigest()[:32]
        )
        events.append(CanonicalEvent(id=event_id, sequence=sequence, **event))
    usage = TrajectoryUsage()
    if usage_seen:
        usage = TrajectoryUsage(
            input_tokens=totals["input"],
            output_tokens=totals["output"],
            cache_read_tokens=totals["cacheRead"],
            cache_write_tokens=totals["cacheWrite"],
            total_tokens=sum(totals.values()),
            cost_usd=cost,
        )
    return CanonicalTrajectory(
        source_format="harbor-0.18.0-pi-jsonl-filtered-message-updates",
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        raw_size_bytes=len(raw),
        session_id=session_id,
        session_timestamp=session_timestamp,
        events=events,
        usage=usage,
        complete=True,
    )
