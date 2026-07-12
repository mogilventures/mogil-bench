from __future__ import annotations

import copy
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from mogil_bench.evidence import (
    EvidenceError,
    build_harbor_evidence,
    upload_evidence_artifact,
    validate_evidence_artifact,
    validate_evidence_endpoint,
    validate_evidence_ingest_counts,
)
from mogil_bench.packs import load_pack
from mogil_bench.trajectory import TrajectoryError, parse_pi_jsonl


def _real_pi_stream() -> bytes:
    rows = [
        {
            "type": "session",
            "version": 3,
            "id": "session-1",
            "timestamp": "2026-07-11T00:00:00Z",
            "cwd": "/workspace",
        },
        {"type": "agent_start"},
        {"type": "turn_start"},
        {
            "type": "message_start",
            "message": {"role": "user", "content": "Fix it", "timestamp": 1783728001000},
        },
        {
            "type": "message_end",
            "message": {"role": "user", "content": "Fix it", "timestamp": 1783728001000},
        },
        {
            "type": "message_start",
            "message": {"role": "assistant", "content": [], "timestamp": 1783728002000},
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "inspect"},
                    {"type": "text", "text": "I will edit."},
                    {
                        "type": "toolCall",
                        "id": "call-1",
                        "name": "edit",
                        "arguments": {"path": "/workspace/a.py", "oldText": "x", "newText": "y"},
                    },
                ],
                "timestamp": 1783728002000,
                "provider": "secret-provider",
                "model": "secret-model",
                "usage": {"input": 2, "output": 3, "cost": {"total": 0.01}},
                "stopReason": "toolUse",
            },
        },
        {
            "type": "tool_execution_start",
            "toolCallId": "call-1",
            "toolName": "edit",
            "args": {"path": "/workspace/a.py", "oldText": "x", "newText": "y"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "call-1",
            "toolName": "edit",
            "result": {"content": [{"type": "text", "text": "edited /workspace/a.py"}]},
            "isError": False,
        },
        {
            "type": "message_start",
            "message": {
                "role": "toolResult",
                "toolCallId": "call-1",
                "toolName": "edit",
                "content": [{"type": "text", "text": "edited"}],
                "isError": False,
                "timestamp": 1783728003000,
            },
        },
        {
            "type": "message_end",
            "message": {
                "role": "toolResult",
                "toolCallId": "call-1",
                "toolName": "edit",
                "content": [{"type": "text", "text": "edited"}],
                "isError": False,
                "timestamp": 1783728003000,
            },
        },
        {"type": "turn_end", "message": {"role": "assistant", "content": []}, "toolResults": []},
        {"type": "turn_start"},
        {
            "type": "message_start",
            "message": {"role": "assistant", "content": [], "timestamp": 1783728004000},
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Done."}],
                "timestamp": 1783728004000,
                "provider": "secret-provider",
                "model": "secret-model",
                "usage": {"input": 4, "output": 1, "cost": {"total": 0.02}},
                "stopReason": "stop",
            },
        },
        {"type": "turn_end", "message": {"role": "assistant", "content": []}, "toolResults": []},
        {"type": "agent_end", "messages": []},
        {"type": "agent_settled"},
    ]
    return b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in rows)


def test_activation_pack_has_three_public_fictional_tasks() -> None:
    pack = load_pack(Path("packs/pi-activation-v1.yaml"))
    assert len(pack.tasks) == 3
    assert {task.privacy_class.value for task in pack.tasks} == {"public"}
    assert all(task.verifier is not None and task.fixture for task in pack.tasks)
    assert [(config.provider, config.model) for config in pack.configurations] == [
        ("anthropic", "claude-sonnet-4-6")
    ]


def test_real_pi_events_are_ordered_linked_and_stable(tmp_path: Path) -> None:
    first = parse_pi_jsonl(_real_pi_stream(), run_id="run-1")
    second = parse_pi_jsonl(_real_pi_stream(), run_id="run-1")
    assert first.events == second.events
    assert [event.sequence for event in first.events] == list(range(len(first.events)))
    assert [event.kind for event in first.events] == [
        "user_message",
        "assistant_reasoning",
        "assistant_message",
        "tool_call",
        "tool_result",
        "final_output",
        "termination",
    ]
    assert first.events[3].call_id == first.events[4].call_id == "call-1"
    assert [event.timestamp for event in first.events] == [
        "2026-07-11T00:00:01+00:00",
        "2026-07-11T00:00:02+00:00",
        "2026-07-11T00:00:02+00:00",
        None,
        None,
        "2026-07-11T00:00:04+00:00",
        None,
    ]
    assert first.complete is True
    assert first.usage.total_tokens == 10
    assert first.usage.cost_usd == 0.03


def test_pi_0806_parallel_tool_calls_preserve_distinct_ids_and_completion_order() -> None:
    rows = [json.loads(line) for line in _real_pi_stream().splitlines()]
    assistant = next(
        row["message"]
        for row in rows
        if row["type"] == "message_end"
        and row.get("message", {}).get("role") == "assistant"
        and any(block.get("type") == "toolCall" for block in row["message"]["content"])
    )
    assistant["content"].append(
        {
            "type": "toolCall",
            "id": "call-2",
            "name": "read",
            "arguments": {"path": "/workspace/b.py"},
        }
    )
    start_index = next(i for i, row in enumerate(rows) if row["type"] == "tool_execution_start")
    rows.insert(
        start_index + 1,
        {
            "type": "tool_execution_start",
            "toolCallId": "call-2",
            "toolName": "read",
            "args": {"path": "/workspace/b.py"},
        },
    )
    end_index = next(i for i, row in enumerate(rows) if row["type"] == "tool_execution_end")
    rows.insert(
        end_index,
        {
            "type": "tool_execution_end",
            "toolCallId": "call-2",
            "toolName": "read",
            "result": {"content": [{"type": "text", "text": "parallel read"}]},
            "isError": False,
        },
    )
    tool_message_end = next(
        i
        for i, row in enumerate(rows)
        if row["type"] == "message_end" and row.get("message", {}).get("role") == "toolResult"
    )
    second_message = {
        "role": "toolResult",
        "toolCallId": "call-2",
        "toolName": "read",
        "content": [{"type": "text", "text": "parallel read"}],
        "isError": False,
        "timestamp": 1783728003000,
    }
    rows[tool_message_end + 1 : tool_message_end + 1] = [
        {"type": "message_start", "message": second_message},
        {"type": "message_end", "message": second_message},
    ]
    raw = b"".join(json.dumps(row).encode() + b"\n" for row in rows)

    trajectory = parse_pi_jsonl(raw, run_id="parallel-run")

    tool_events = [
        (event.kind, event.call_id)
        for event in trajectory.events
        if event.kind in {"tool_call", "tool_result", "tool_error"}
    ]
    assert tool_events == [
        ("tool_call", "call-1"),
        ("tool_call", "call-2"),
        ("tool_result", "call-2"),
        ("tool_result", "call-1"),
    ]
    assert trajectory.events[-1].kind == "termination"


def test_evidence_batch_requires_run_ids_and_attempts_to_be_independently_unique(
    tmp_path: Path,
) -> None:
    artifact = json.loads(
        Path("tests/fixtures/daytona-reviewer-contract.json").read_text(encoding="utf-8")
    )
    second = copy.deepcopy(artifact)
    second["run"]["attempt"] = "fictional-attempt-2"
    path = tmp_path / "attempts.json"
    path.write_text(json.dumps([artifact, second]), encoding="utf-8")

    with pytest.raises(EvidenceError, match="duplicate run ids"):
        validate_evidence_artifact(path)

    second["run"]["id"] = "fictional-run-2"
    path.write_text(json.dumps([artifact, second]), encoding="utf-8")
    assert validate_evidence_artifact(path) == 2

    second["run"]["attempt"] = artifact["run"]["attempt"]
    path.write_text(json.dumps([artifact, second]), encoding="utf-8")
    with pytest.raises(EvidenceError, match="duplicate attempts"):
        validate_evidence_artifact(path)


def test_pi_tool_errors_remain_explicit_and_linked() -> None:
    rows = [json.loads(line) for line in _real_pi_stream().splitlines()]
    for row in rows:
        if row["type"] == "tool_execution_end":
            row["isError"] = True
        if row["type"] in {"message_start", "message_end"}:
            message = row.get("message", {})
            if message.get("role") == "toolResult":
                message["isError"] = True
    raw = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
    trajectory = parse_pi_jsonl(raw, run_id="run-error")
    assert [event.kind for event in trajectory.events].count("tool_error") == 1
    assert [event.kind for event in trajectory.events].count("tool_result") == 0


def test_pi_0806_requires_one_terminal_stop_response_after_tools() -> None:
    base = [json.loads(line) for line in _real_pi_stream().splitlines()]

    missing = copy.deepcopy(base)
    final_start = max(
        i
        for i, row in enumerate(missing)
        if row["type"] == "turn_start"
    )
    agent_end = next(i for i, row in enumerate(missing) if row["type"] == "agent_end")
    del missing[final_start:agent_end]
    with pytest.raises(TrajectoryError, match="terminal assistant"):
        parse_pi_jsonl(
            b"".join(json.dumps(row).encode() + b"\n" for row in missing),
            run_id="missing-stop",
        )

    wrong_reason = copy.deepcopy(base)
    final_message = next(
        row["message"]
        for row in reversed(wrong_reason)
        if row["type"] == "message_end" and row.get("message", {}).get("role") == "assistant"
    )
    final_message["stopReason"] = "toolUse"
    with pytest.raises(TrajectoryError, match="terminal assistant"):
        parse_pi_jsonl(
            b"".join(json.dumps(row).encode() + b"\n" for row in wrong_reason),
            run_id="tool-use-stop",
        )

    duplicate = copy.deepcopy(base)
    terminal_end = max(
        i
        for i, row in enumerate(duplicate)
        if row["type"] == "message_end"
        and row.get("message", {}).get("role") == "assistant"
    )
    terminal_start = max(
        i
        for i, row in enumerate(duplicate[:terminal_end])
        if row["type"] == "message_start"
        and row.get("message", {}).get("role") == "assistant"
    )
    final_pair = [duplicate[terminal_start], duplicate[terminal_end]]
    end_index = next(i for i, row in enumerate(duplicate) if row["type"] == "agent_end")
    duplicate[end_index:end_index] = copy.deepcopy(final_pair)
    with pytest.raises(TrajectoryError, match="exactly one terminal"):
        parse_pi_jsonl(
            b"".join(json.dumps(row).encode() + b"\n" for row in duplicate),
            run_id="duplicate-stop",
        )

    out_of_order = copy.deepcopy(base)
    assistant_ends = [
        row["message"]
        for row in out_of_order
        if row["type"] == "message_end" and row.get("message", {}).get("role") == "assistant"
    ]
    assistant_ends[0]["stopReason"] = "stop"
    assistant_ends[-1]["stopReason"] = "toolUse"
    with pytest.raises(TrajectoryError, match="terminal assistant"):
        parse_pi_jsonl(
            b"".join(json.dumps(row).encode() + b"\n" for row in out_of_order),
            run_id="out-of-order-stop",
        )


def test_pi_parser_fails_closed_on_malformed_truncated_or_incomplete_linkage() -> None:
    stream = _real_pi_stream()
    with pytest.raises(TrajectoryError, match="JSON"):
        parse_pi_jsonl(stream + b'{"type":', run_id="run")
    rows = stream.splitlines(keepends=True)
    with pytest.raises(TrajectoryError, match="termination"):
        parse_pi_jsonl(b"".join(rows[:-1]), run_id="run")
    with pytest.raises(TrajectoryError, match="follow agent_settled"):
        parse_pi_jsonl(stream + b'{"type":"agent_settled"}\n', run_id="run")
    decoded = [json.loads(line) for line in stream.splitlines()]
    decoded[-2], decoded[-1] = decoded[-1], decoded[-2]
    out_of_order = b"".join(json.dumps(row).encode() + b"\n" for row in decoded)
    with pytest.raises(TrajectoryError, match="settled lifecycle"):
        parse_pi_jsonl(out_of_order, run_id="run")
    unlinked = stream.replace(b'"toolCallId":"call-1"', b'"toolCallId":"other"', 1)
    with pytest.raises(TrajectoryError, match="link"):
        parse_pi_jsonl(unlinked, run_id="run")
    non_monotonic = stream.replace(b"1783728004000", b"1783728000500")
    with pytest.raises(TrajectoryError, match="monotonic"):
        parse_pi_jsonl(non_monotonic, run_id="run")
    invalid_timestamp = stream.replace(b"1783728001000", b'"not-a-timestamp"')
    with pytest.raises(TrajectoryError, match="timestamp"):
        parse_pi_jsonl(invalid_timestamp, run_id="run")


def test_evidence_projection_redacts_sensitive_data_and_validates_jsonl(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    for directory in ("agent", "workspace", "verifier"):
        (bundle / directory).mkdir(parents=True, exist_ok=True)
    raw = _real_pi_stream().replace(b"Fix it", b"Use token sk-test-12345678901234567890")
    (bundle / "agent/pi.txt").write_bytes(raw)
    (bundle / "workspace/changed-files.json").write_text('[{"path":"a.py","status":"modified"}]')
    (bundle / "workspace/patch.diff").write_text("--- a/a.py\n+++ b/a.py\n-old\n+new\n")
    (bundle / "verifier/verification.json").write_text(
        json.dumps(
            {
                "exit_code": 0,
                "timed_out": False,
                "command_exit_passed": True,
                "stdout_assertion_passed": True,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "verifier_outcome": "passed",
                "infrastructure_outcome": "succeeded",
            }
        )
    )
    (bundle / "verifier/reward.json").write_text(
        '{"reward":1,"command_exit":1,"stdout_assertion":1}'
    )
    (bundle / "verifier/stdout.txt").write_text("passed HIDDEN_VERIFIER_CANARY_abc /root/private")
    (bundle / "verifier/stderr.txt").write_text("")
    artifact = build_harbor_evidence(
        bundle,
        run_id="run-1",
        attempt_id="attempt-1",
        task={
            "id": "task",
            "revision": "1",
            "privacy_class": "public",
            "prompt": "Fix /root/private",
        },
        outcomes={
            "process": "succeeded",
            "verifier": "passed",
            "infrastructure": "succeeded",
            "evidence_completeness": "complete",
        },
        analysis_metadata={"provider": "secret-provider", "model": "secret-model"},
        termination_reason="completed",
    )
    assert artifact.reviewer.environment_class == "docker"
    (bundle / "environment.json").write_text(
        json.dumps({"schema_version": "1", "provider": "daytona"}),
        encoding="utf-8",
    )
    daytona_artifact = build_harbor_evidence(
        bundle,
        run_id="run-daytona",
        attempt_id="attempt-daytona",
        task={
            "id": "task",
            "revision": "1",
            "privacy_class": "public",
            "prompt": "Fix /root/private",
        },
        outcomes={
            "process": "succeeded",
            "verifier": "passed",
            "infrastructure": "succeeded",
            "evidence_completeness": "complete",
        },
        analysis_metadata={"provider": "secret-provider", "model": "secret-model"},
        termination_reason="completed",
    )
    assert daytona_artifact.reviewer.environment_class == "isolated-sandbox"
    path = tmp_path / "evidence.json"
    path.write_text(artifact.model_dump_json())
    assert validate_evidence_artifact(path) == 1
    jsonl = tmp_path / "evidence.jsonl"
    jsonl.write_text(artifact.model_dump_json() + "\n")
    assert validate_evidence_artifact(jsonl) == 1
    value = json.loads(path.read_text(encoding="utf-8"))
    reviewer_evidence = value["reviewer"]["evidence"]
    canonical_changed = json.dumps(
        reviewer_evidence["changed_files"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    assert (
        reviewer_evidence["changed_files_reference"]["reviewer_sha256"]
        == hashlib.sha256(canonical_changed).hexdigest()
    )
    assert (
        reviewer_evidence["patch_reference"]["reviewer_sha256"]
        == hashlib.sha256(reviewer_evidence["patch"].encode()).hexdigest()
    )
    verifier_references = {
        reference["path"]: reference for reference in reviewer_evidence["verifier_references"]
    }
    assert (
        verifier_references["verifier/stdout.txt"]["reviewer_sha256"]
        == hashlib.sha256(reviewer_evidence["verifier_stdout"].encode()).hexdigest()
    )
    assert (
        verifier_references["verifier/stdout.txt"]["sha256"]
        != verifier_references["verifier/stdout.txt"]["reviewer_sha256"]
    )
    assert (
        verifier_references["verifier/stderr.txt"]["reviewer_sha256"]
        == hashlib.sha256(reviewer_evidence["verifier_stderr"].encode()).hexdigest()
    )

    for label, mutate in (
        (
            "patch",
            lambda candidate: candidate["reviewer"]["evidence"].__setitem__("patch", "[REDACTED]"),
        ),
        (
            "stdout",
            lambda candidate: candidate["reviewer"]["evidence"].__setitem__(
                "verifier_stdout", "tampered"
            ),
        ),
        (
            "changed-files",
            lambda candidate: candidate["reviewer"]["evidence"]["changed_files"].append(
                {"path": "extra.py", "status": "added"}
            ),
        ),
    ):
        tampered = copy.deepcopy(value)
        mutate(tampered)
        tampered_path = tmp_path / f"tampered-{label}.json"
        tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(EvidenceError, match="invalid mogil.harbor-evidence"):
            validate_evidence_artifact(tampered_path)

    eligibility_tampers = (
        (
            "failed-outcome",
            lambda candidate: candidate["outcomes"].__setitem__("process", "failed"),
        ),
        (
            "failed-reward",
            lambda candidate: candidate["rewards"].__setitem__("reward", 0.0),
        ),
        (
            "naive-started-at",
            lambda candidate: candidate["run"].__setitem__(
                "started_at", "2026-07-11T00:00:00"
            ),
        ),
        (
            "reversed-run-time",
            lambda candidate: candidate["run"].__setitem__(
                "ended_at", "2026-07-10T23:59:59+00:00"
            ),
        ),
        (
            "termination-mismatch",
            lambda candidate: candidate["run"].__setitem__(
                "termination_reason", "failed"
            ),
        ),
    )
    for label, mutate in eligibility_tampers:
        tampered = copy.deepcopy(value)
        mutate(tampered)
        tampered_path = tmp_path / f"eligible-{label}.json"
        tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(EvidenceError, match="invalid mogil.harbor-evidence"):
            validate_evidence_artifact(tampered_path)

    missing_reviewer_hash = copy.deepcopy(value)
    del missing_reviewer_hash["reviewer"]["evidence"]["patch_reference"][
        "reviewer_sha256"
    ]
    missing_hash_path = tmp_path / "missing-reviewer-hash.json"
    missing_hash_path.write_text(json.dumps(missing_reviewer_hash), encoding="utf-8")
    with pytest.raises(EvidenceError, match="invalid mogil.harbor-evidence"):
        validate_evidence_artifact(missing_hash_path)

    invalid_value = json.loads(path.read_text(encoding="utf-8"))
    invalid_value["reviewer"]["events"][5]["timestamp"] = "2026-07-11T00:00:00+00:00"
    invalid_path = tmp_path / "non-monotonic.json"
    invalid_path.write_text(json.dumps(invalid_value), encoding="utf-8")
    with pytest.raises(EvidenceError, match="invalid mogil.harbor-evidence"):
        validate_evidence_artifact(invalid_path)
    reviewer = json.dumps(artifact.reviewer.model_dump(mode="json"))
    assert "secret-provider" not in reviewer and "secret-model" not in reviewer
    assert "sk-test" not in reviewer and "/root/private" not in reviewer
    assert "HIDDEN_VERIFIER_CANARY" not in reviewer

    observed: dict[str, object] = {}

    class IngestHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            size = int(self.headers["Content-Length"])
            observed["authorization"] = self.headers["Authorization"]
            observed["payload"] = json.loads(self.rfile.read(size))
            response = json.dumps(
                {"complete": 1, "imported": 1, "deduped": 0, "invalid": 0}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), IngestHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        counts = upload_evidence_artifact(
            path,
            f"http://127.0.0.1:{server.server_port}/ingest/v1/eval-runs",
            "automation-secret-token",
            confirm=True,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert counts == {"complete": 1, "imported": 1, "deduped": 0, "invalid": 0}
    assert observed["authorization"] == "Bearer automation-secret-token"
    assert len(observed["payload"]["runs"]) == 1  # type: ignore[index]


def test_evidence_upload_endpoint_and_complete_counts_are_strict() -> None:
    validate_evidence_endpoint("https://blindbench.example/ingest/v1/eval-runs")
    validate_evidence_endpoint("http://127.0.0.1:3000/ingest/v1/eval-runs")
    validate_evidence_endpoint("http://[::1]:3000/ingest/v1/eval-runs")
    for endpoint in (
        "http://blindbench.example/ingest/v1/eval-runs",
        "https://blindbench.example/ingest/v1/traces",
        "https://user:secret@blindbench.example/ingest/v1/eval-runs",
        "https://blindbench.example/ingest/v1/eval-runs?token=secret",
    ):
        with pytest.raises(EvidenceError, match="endpoint"):
            validate_evidence_endpoint(endpoint)
    assert (
        validate_evidence_ingest_counts(
            {"complete": 3, "imported": 2, "deduped": 1, "invalid": 0}, 3
        )["complete"]
        == 3
    )
    with pytest.raises(EvidenceError, match="complete"):
        validate_evidence_ingest_counts(
            {"complete": 2, "imported": 2, "deduped": 0, "invalid": 0}, 3
        )
