from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
from typer.core import TyperOption
from typer.main import get_command
from typer.testing import CliRunner

from mogil_bench import artifacts, evidence
from mogil_bench.artifacts import ArtifactError, upload_artifact
from mogil_bench.cli import app
from mogil_bench.evidence import EvidenceError, upload_evidence_artifact
from mogil_bench.runner import run_pack

ROOT = Path(__file__).parents[1]
CLI = CliRunner()


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self.payload if limit < 0 else self.payload[:limit]


def test_upload_timeout_is_forwarded_for_legacy_and_strict_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_path = run_pack(ROOT / "packs/sample-v1.yaml", tmp_path / "run") / "blindbench.json"
    strict_path = ROOT / "tests/fixtures/daytona-reviewer-contract.json"
    observed: list[float] = []

    def legacy_open(_request: object, *, timeout: float) -> _Response:
        observed.append(timeout)
        return _Response(
            json.dumps(
                {"imported": 2, "deduped": 0, "invalid": 0, "truncated": False}
            ).encode()
        )

    def evidence_open(_request: object, *, timeout: float) -> _Response:
        observed.append(timeout)
        return _Response(
            json.dumps(
                {"complete": 1, "imported": 1, "deduped": 0, "invalid": 0}
            ).encode()
        )

    monkeypatch.setattr(artifacts, "urlopen", legacy_open)
    assert upload_artifact(
        legacy_path,
        "https://example.convex.site/ingest/v1/traces",
        "legacy-token",
        confirm=True,
        timeout=91.5,
    ) is not None
    monkeypatch.setattr(evidence, "urlopen", evidence_open)
    assert upload_evidence_artifact(
        strict_path,
        "https://blindbench.example/ingest/v1/eval-runs",
        "automation-token",
        confirm=True,
        timeout=123,
    ) is not None
    assert observed == [91.5, 123]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1, 601])
def test_upload_timeout_validation_rejects_unsafe_values(tmp_path: Path, value: float) -> None:
    path = run_pack(ROOT / "packs/sample-v1.yaml", tmp_path / "run") / "blindbench.json"
    with pytest.raises(ArtifactError, match="timeout"):
        upload_artifact(
            path,
            "https://example.convex.site/ingest/v1/traces",
            "",
            confirm=False,
            timeout=value,
        )
    with pytest.raises(EvidenceError, match="timeout"):
        upload_evidence_artifact(
            ROOT / "tests/fixtures/daytona-reviewer-contract.json",
            "https://blindbench.example/ingest/v1/eval-runs",
            "",
            confirm=False,
            timeout=value,
        )


def test_timeout_reports_unknown_outcome_and_does_not_retry_or_disclose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = run_pack(ROOT / "packs/sample-v1.yaml", tmp_path / "run") / "blindbench.json"
    calls = 0

    def timeout_open(_request: object, *, timeout: float) -> _Response:
        nonlocal calls
        calls += 1
        raise TimeoutError("timed out with legacy-token and prompt payload")

    monkeypatch.setattr(artifacts, "urlopen", timeout_open)
    with pytest.raises(ArtifactError) as caught:
        upload_artifact(
            path,
            "https://example.convex.site/ingest/v1/traces",
            "legacy-token",
            confirm=True,
            timeout=90,
        )

    message = str(caught.value).lower()
    assert "outcome is unknown" in message
    assert "may have completed" in message
    assert "same artifact" in message and "idempotent" in message
    assert "legacy-token" not in message and "prompt payload" not in message
    assert calls == 1


def test_http_diagnostics_are_bounded_sanitized_and_payload_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = run_pack(ROOT / "packs/sample-v1.yaml", tmp_path / "run") / "blindbench.json"
    request_payload = json.loads(path.read_text(encoding="utf-8"))["records"][0]["output"][
        "content"
    ]
    token = "legacy-secret-token"
    response_body = json.dumps(
        {
            "message": (
                f"lease conflict; echoed {request_payload}; Authorization: Bearer {token}; "
                + "x" * 5000
            )
        }
    ).encode()
    calls = 0

    def failing_open(request: object, *, timeout: float) -> _Response:
        nonlocal calls
        calls += 1
        raise HTTPError(
            "https://example.convex.site/ingest/v1/traces",
            409,
            "Conflict",
            {"Content-Type": "application/json"},
            io.BytesIO(response_body),
        )

    monkeypatch.setattr(artifacts, "urlopen", failing_open)
    with pytest.raises(ArtifactError) as caught:
        upload_artifact(
            path,
            "https://example.convex.site/ingest/v1/traces",
            token,
            confirm=True,
            timeout=120,
        )

    message = str(caught.value)
    assert "HTTP 409" in message and "lease conflict" in message
    assert request_payload not in message and token not in message
    assert "Authorization" not in message
    assert len(message) < 600
    assert calls == 1


def test_strict_http_and_malformed_response_errors_never_echo_evidence_or_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = ROOT / "tests/fixtures/daytona-reviewer-contract.json"
    payload_text = "Correct the fictional calculator."
    token = "automation-secret-token"

    def http_failure(_request: object, *, timeout: float) -> _Response:
        body = json.dumps(
            {"detail": f"pending lease for {payload_text}; token={token}"}
        ).encode()
        raise HTTPError(
            "https://blindbench.example/ingest/v1/eval-runs",
            423,
            "Locked",
            {"Content-Type": "application/json"},
            io.BytesIO(body),
        )

    monkeypatch.setattr(evidence, "urlopen", http_failure)
    with pytest.raises(EvidenceError) as caught:
        upload_evidence_artifact(
            path,
            "https://blindbench.example/ingest/v1/eval-runs",
            token,
            confirm=True,
        )
    message = str(caught.value)
    assert "HTTP 423" in message and "pending lease" in message
    assert payload_text not in message and token not in message

    monkeypatch.setattr(evidence, "urlopen", lambda *_args, **_kwargs: _Response(b"not-json"))
    with pytest.raises(EvidenceError) as malformed:
        upload_evidence_artifact(
            path,
            "https://blindbench.example/ingest/v1/eval-runs",
            token,
            confirm=True,
        )
    malformed_message = str(malformed.value)
    assert "malformed JSON" in malformed_message
    assert "not-json" not in malformed_message and token not in malformed_message


def test_cli_exposes_safe_upload_timeout_default() -> None:
    root = get_command(app)
    for group_name, endpoint in (
        ("artifact", "https://x.convex.site/ingest/v1/traces"),
        ("evidence", "https://x.test/ingest/v1/eval-runs"),
    ):
        command = root.commands[group_name].commands["upload"]
        option = next(parameter for parameter in command.params if parameter.name == "timeout")
        assert isinstance(option, TyperOption)
        assert option.default == 120.0
        result = CLI.invoke(
            app,
            [group_name, "upload", "missing.json", "--endpoint", endpoint, "--timeout", "nan"],
        )
        assert result.exit_code == 1
        assert "timeout" in result.stderr.lower()
