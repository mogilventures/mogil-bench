from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from mogil_bench.artifacts import (
    ArtifactError,
    upload_artifact,
    validate_artifact,
    validate_ingest_counts,
)
from mogil_bench.cli import app
from mogil_bench.models import Pack, PrivacyClass
from mogil_bench.packs import PackError, load_pack
from mogil_bench.runner import MAX_OUTPUT_BYTES, run_pack

ROOT = Path(__file__).parents[1]
CLI = CliRunner()


def command_pack(tasks: list[dict[str, object]]) -> dict[str, object]:
    return {
        "version": "1",
        "id": "test-pack",
        "revision": "1",
        "name": "Test pack",
        "allow_commands": True,
        "tasks": tasks,
        "configurations": [
            {
                "id": "command",
                "provider": "local",
                "model": "test",
                "adapter": "command",
                "harness": {"name": "test", "version": "1"},
            }
        ],
    }


def pi_pack(configurations: list[dict[str, object]]) -> dict[str, object]:
    return {
        "version": "1",
        "id": "pi-test-pack",
        "revision": "1",
        "name": "Pi test pack",
        "allow_agents": True,
        "tasks": [
            {
                "id": "task",
                "category": "coding",
                "lane": "pi-coding",
                "prompt": "Inspect the fixture and report the result.",
                "timeout_seconds": 5,
            }
        ],
        "configurations": configurations,
    }


def write_pack(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "pack.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def raw_results(run_dir: Path) -> list[dict[str, object]]:
    return [json.loads(path.read_text()) for path in sorted((run_dir / "results").glob("*.json"))]


def test_schema_rejects_unknown_privacy_and_command_without_pack_opt_in() -> None:
    raw = command_pack(
        [{"id": "task", "category": "x", "lane": "pi-coding", "prompt": "x", "command": ["true"]}]
    )
    raw["allow_commands"] = False
    with pytest.raises(ValidationError, match="allow_commands"):
        Pack.model_validate(raw)
    raw["allow_commands"] = True
    raw["tasks"][0]["privacy_class"] = "secret"  # type: ignore[index]
    with pytest.raises(ValidationError):
        Pack.model_validate(raw)
    assert {item.value for item in PrivacyClass} == {
        "public",
        "internal",
        "confidential",
        "pii",
        "phi",
    }


def test_schema_requires_agent_opt_in_for_pi_adapter() -> None:
    raw = pi_pack(
        [
            {
                "id": "pi",
                "provider": "provider-a",
                "model": "model-a",
                "adapter": "pi",
                "harness": {"name": "pi", "version": "1"},
            }
        ]
    )
    raw["allow_agents"] = False
    with pytest.raises(ValidationError, match="allow_agents"):
        Pack.model_validate(raw)


def test_pack_rejects_fixture_escape(tmp_path: Path) -> None:
    data = command_pack(
        [
            {
                "id": "task",
                "category": "x",
                "lane": "pi-coding",
                "fixture": "../secret",
                "command": ["true"],
            }
        ]
    )
    with pytest.raises(PackError, match="escapes"):
        load_pack(write_pack(tmp_path, data))


def test_mock_is_deterministic_and_ids_are_stable(tmp_path: Path) -> None:
    first = run_pack(ROOT / "packs/sample-v1.yaml", tmp_path / "one")
    second = run_pack(ROOT / "packs/sample-v1.yaml", tmp_path / "two")
    first_raw = raw_results(first)
    second_raw = raw_results(second)
    assert [item["id"] for item in first_raw] == [item["id"] for item in second_raw]
    assert [item["execution"]["content"] for item in first_raw] == [  # type: ignore[index]
        item["execution"]["content"]
        for item in second_raw  # type: ignore[index]
    ]


def test_command_requires_cli_acknowledgement(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="--allow-commands"):
        run_pack(ROOT / "packs/command-smoke-v1.yaml", tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_pi_requires_acknowledgement_before_creating_output(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="--allow-agents"):
        run_pack(ROOT / "packs/pi-template-v1.yaml", tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_pi_uses_safe_argv_and_configuration_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_pi = tmp_path / "pi"
    fake_pi.write_text(
        "#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)
    monkeypatch.setenv("MOGIL_BENCH_PI_EXECUTABLE", str(fake_pi))
    configs = [
        {
            "id": "first",
            "provider": "provider-a",
            "model": "model-a",
            "adapter": "pi",
            "harness": {"name": "pi", "version": "test"},
        },
        {
            "id": "second",
            "provider": "provider-b",
            "model": "model-b",
            "adapter": "pi",
            "harness": {"name": "pi", "version": "test"},
        },
    ]
    pack_path = write_pack(tmp_path, pi_pack(configs))
    run_dir = tmp_path / "run"
    invoked = CLI.invoke(
        app,
        [
            "run",
            str(pack_path),
            "--output-dir",
            str(run_dir),
            "--allow-agents",
        ],
    )
    assert invoked.exit_code == 0, invoked.stdout
    results = {item["configuration_id"]: item for item in raw_results(run_dir)}
    first_argv = json.loads(results["first"]["execution"]["content"])  # type: ignore[index,arg-type]
    second_argv = json.loads(results["second"]["execution"]["content"])  # type: ignore[index,arg-type]
    assert first_argv != second_argv
    assert first_argv[first_argv.index("--provider") + 1] == "provider-a"
    assert first_argv[first_argv.index("--model") + 1] == "model-a"
    assert "--print" in first_argv
    assert "--no-session" in first_argv
    assert "--no-extensions" in first_argv
    assert "--no-skills" in first_argv
    assert "--no-prompt-templates" in first_argv
    assert "--no-context-files" in first_argv
    assert first_argv[first_argv.index("--tools") + 1] == "read,write,edit"
    assert "current benchmark directory" in first_argv[first_argv.index("--system-prompt") + 1]
    assert results["first"]["provider"] == "provider-a"
    assert results["first"]["model"] == "model-a"


def test_pi_executable_override_must_be_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOGIL_BENCH_PI_EXECUTABLE", "relative-pi")
    run_dir = run_pack(
        ROOT / "packs/pi-template-v1.yaml",
        tmp_path / "run",
        allow_agents=True,
    )
    result = raw_results(run_dir)[0]
    assert result["execution"]["status"] == "denied"  # type: ignore[index]
    assert "absolute path" in result["execution"]["error"]  # type: ignore[index,operator]

    monkeypatch.setenv("MOGIL_BENCH_PI_EXECUTABLE", "/bin/echo")
    named_run = run_pack(
        ROOT / "packs/pi-template-v1.yaml",
        tmp_path / "named-run",
        allow_agents=True,
    )
    named_result = raw_results(named_run)[0]
    assert "named pi" in named_result["execution"]["error"]  # type: ignore[index,operator]


def test_command_timeout_and_forbidden_command_are_recorded(tmp_path: Path) -> None:
    tasks = [
        {
            "id": "timeout",
            "category": "x",
            "lane": "pi-coding",
            "prompt": "x",
            "timeout_seconds": 0.05,
            "command": ["python3", "-c", "import time; time.sleep(1)"],
        },
        {
            "id": "denied",
            "category": "x",
            "lane": "pi-coding",
            "prompt": "x",
            "command": ["git", "status"],
        },
    ]
    run_dir = run_pack(
        write_pack(tmp_path, command_pack(tasks)), tmp_path / "run", allow_commands=True
    )
    statuses = {item["task_id"]: item["execution"]["status"] for item in raw_results(run_dir)}  # type: ignore[index]
    assert statuses == {"denied": "denied", "timeout": "timed_out"}


def test_command_output_is_bounded_and_matrix_continues_after_failure(tmp_path: Path) -> None:
    tasks = [
        {
            "id": "large",
            "category": "x",
            "lane": "pi-coding",
            "prompt": "x",
            "command": ["python3", "-c", "print('a' * 40000)"],
        },
        {
            "id": "failure",
            "category": "x",
            "lane": "pi-coding",
            "prompt": "x",
            "command": ["python3", "-c", "raise SystemExit(7)"],
        },
        {
            "id": "success",
            "category": "x",
            "lane": "pi-coding",
            "prompt": "x",
            "command": ["python3", "-c", "print('ok')"],
        },
    ]
    run_dir = run_pack(
        write_pack(tmp_path, command_pack(tasks)), tmp_path / "run", allow_commands=True
    )
    results = {item["task_id"]: item for item in raw_results(run_dir)}
    assert results["large"]["execution"]["output_truncated"] is True  # type: ignore[index]
    assert len(results["large"]["execution"]["content"].encode()) <= MAX_OUTPUT_BYTES  # type: ignore[index,union-attr]
    assert results["failure"]["execution"]["status"] == "failed"  # type: ignore[index]
    assert results["success"]["execution"]["status"] == "succeeded"  # type: ignore[index]


def test_export_shape_and_validation(tmp_path: Path) -> None:
    run_dir = run_pack(ROOT / "packs/sample-v1.yaml", tmp_path / "run")
    batch = json.loads((run_dir / "blindbench.json").read_text())
    assert set(batch) == {"records"}
    assert len(batch["records"]) == 2
    record = batch["records"][0]
    assert record["version"] == "1"
    assert record["input"]["messages"]
    assert set(record["usage"]) == {"duration_ms"}
    assert "cost_usd" not in record["usage"]
    assert record["metadata"]["evidence_status"] == "non_quality"
    assert validate_artifact(run_dir / "blindbench.json") == 2
    assert validate_artifact(run_dir / "blindbench.jsonl") == 2


def test_invalid_artifact_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text('{"records":[{"version":"1","input":{"messages":[]}}]}')
    with pytest.raises(ArtifactError):
        validate_artifact(path)


def test_upload_is_dry_run_and_endpoint_is_guarded(tmp_path: Path) -> None:
    run_dir = run_pack(ROOT / "packs/sample-v1.yaml", tmp_path / "run")
    artifact = run_dir / "blindbench.json"
    assert (
        upload_artifact(artifact, "https://example.convex.site/ingest/v1/traces", "", confirm=False)
        is None
    )
    with pytest.raises(ArtifactError, match="endpoint"):
        upload_artifact(artifact, "https://example.com/ingest/v1/traces", "", confirm=False)


def test_ingest_counts_reject_partial_or_truncated_success() -> None:
    complete = {
        "traces": 2,
        "imported": 1,
        "deduped": 1,
        "steps": 4,
        "requestMissing": 0,
        "responseMissing": 0,
        "invalid": 0,
        "truncated": False,
    }
    assert validate_ingest_counts(complete, 2)["deduped"] == 1

    invalid = {**complete, "imported": 0, "invalid": 1}
    with pytest.raises(ArtifactError, match="invalid"):
        validate_ingest_counts(invalid, 2)

    truncated = {**complete, "truncated": True}
    with pytest.raises(ArtifactError, match="truncated"):
        validate_ingest_counts(truncated, 2)

    partial = {**complete, "imported": 0, "deduped": 1}
    with pytest.raises(ArtifactError, match="accepted 1 of 2"):
        validate_ingest_counts(partial, 2)


def test_cli_end_to_end_smoke(tmp_path: Path) -> None:
    pack = ROOT / "packs/sample-v1.yaml"
    listed = CLI.invoke(app, ["pack", "list", str(ROOT / "packs")])
    assert listed.exit_code == 0
    assert "mogil-sample-v1" in listed.stdout
    validated = CLI.invoke(app, ["pack", "validate", str(pack)])
    assert validated.exit_code == 0
    run_dir = tmp_path / "cli-run"
    ran = CLI.invoke(app, ["run", str(pack), "--output-dir", str(run_dir)])
    assert ran.exit_code == 0, ran.stdout
    checked = CLI.invoke(app, ["artifact", "validate", str(run_dir / "blindbench.json")])
    assert checked.exit_code == 0
    assert "2 record(s)" in checked.stdout
