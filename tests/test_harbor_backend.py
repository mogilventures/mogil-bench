from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from mogil_bench.artifacts import validate_artifact
from mogil_bench.harbor_backend import (
    _FIXTURE_PROFILE_TOKEN,
    HARBOR_VERSION,
    PI_VERSION,
    HarborBackend,
    PreflightError,
    PreflightProbes,
    preflight,
    require_harbor,
)
from mogil_bench.harbor_tasks import HarborTranslation
from mogil_bench.models import (
    Configuration,
    EvidenceStatus,
    Harness,
    Pack,
    create_attempt_identity,
)
from mogil_bench.runner import _run_shipped_fixture_pack, logical_run_id, run_pack

ROOT = Path(__file__).parents[1]


def test_compatibility_versions_are_exact() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["requires-python"] == ">=3.12,<3.13"
    assert "harbor==0.18.0" in project["project"]["dependencies"]
    assert HARBOR_VERSION == "0.18.0"
    assert PI_VERSION == "0.80.6"
    assert require_harbor().__name__ == "harbor"


def test_rejects_incompatible_harbor_version() -> None:
    imported = False

    def importer(name: str) -> object:
        nonlocal imported
        imported = True
        return object()

    with pytest.raises(RuntimeError, match=r"requires Harbor 0\.18\.0; installed 0\.18\.1"):
        require_harbor(version_getter=lambda _name: "0.18.1", importer=importer)

    assert imported is False


def harbor_configuration(**overrides: object) -> Configuration:
    values: dict[str, object] = {
        "id": "harbor-pi",
        "provider": "anthropic",
        "model": "claude-test",
        "adapter": "harbor",
        "harness": Harness(name="harbor", version=HARBOR_VERSION),
    }
    values.update(overrides)
    return Configuration.model_validate(values)


def test_harbor_config_is_narrow_and_serializes_defaults() -> None:
    config = harbor_configuration()

    assert config.model_dump(mode="json")["backend"] == "harbor"
    assert config.model_dump(mode="json")["environment_type"] == "docker"
    assert config.mounts == []

    with pytest.raises(ValidationError, match="backend"):
        harbor_configuration(backend="other")
    with pytest.raises(ValidationError, match="environment_policy"):
        harbor_configuration(environment_type="daytona")
    with pytest.raises(ValidationError, match="mounts"):
        harbor_configuration(mounts=[{"source": "/tmp", "target": "/work"}])


def test_harbor_config_requires_pi_coding_lane_and_agent_opt_in() -> None:
    raw = {
        "version": "1",
        "id": "harbor-pack",
        "revision": "1",
        "name": "Harbor pack",
        "allow_agents": True,
        "tasks": [{"id": "t", "category": "text", "lane": "hermes-text", "prompt": "x"}],
        "configurations": [harbor_configuration().model_dump(mode="json")],
    }
    with pytest.raises(ValidationError, match="pi-coding"):
        Pack.model_validate(raw)

    raw["tasks"][0]["lane"] = "pi-coding"  # type: ignore[index]
    raw["allow_agents"] = False
    with pytest.raises(ValidationError, match="allow_agents"):
        Pack.model_validate(raw)


def test_logical_identity_is_stable_and_attempt_identity_is_unique() -> None:
    config = harbor_configuration()
    task = Pack.model_validate(
        {
            "version": "1",
            "id": "p",
            "revision": "1",
            "name": "p",
            "allow_agents": True,
            "tasks": [{"id": "t", "category": "coding", "lane": "pi-coding", "prompt": "x"}],
            "configurations": [config.model_dump(mode="json")],
        }
    ).tasks[0]
    first_logical = logical_run_id("fingerprint", task, config)
    second_logical = logical_run_id("fingerprint", task, config)
    first = create_attempt_identity(first_logical)
    second = create_attempt_identity(second_logical)

    assert first.logical_run_id == second.logical_run_id == first_logical
    assert first.attempt_id != second.attempt_id
    assert UUID(first.attempt_id).version == UUID(second.attempt_id).version == 4
    injected = create_attempt_identity(first_logical, attempt_id_factory=lambda: "attempt-test")
    assert injected.attempt_id == "attempt-test"


def test_public_runner_cannot_mint_fixture_complete_with_an_import_path(
    tmp_path: Path,
) -> None:
    assert "test_agent_import_path" not in inspect.signature(run_pack).parameters
    with pytest.raises(ValueError, match="immutable shipped Harbor fixture"):
        _run_shipped_fixture_pack(ROOT / "packs/pi-template-v1.yaml", tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_harbor_requires_operator_opt_in_and_never_falls_back_to_host_pi(
    tmp_path: Path,
) -> None:
    pack_path = tmp_path / "pack.yaml"
    pack_path.write_text(
        """version: '1'
id: harbor-pack
revision: '1'
name: Harbor pack
allow_agents: true
tasks:
  - id: t
    category: coding
    lane: pi-coding
    prompt: fix it
configurations:
  - id: harbor
    provider: test
    model: test
    adapter: harbor
    harness: {name: harbor, version: '0.18.0'}
""",
        encoding="utf-8",
    )

    with pytest.raises(PermissionError, match="--allow-agents"):
        run_pack(pack_path, tmp_path / "without-opt-in")
    assert not (tmp_path / "without-opt-in").exists()



def valid_translation(tmp_path: Path) -> HarborTranslation:
    return HarborTranslation(
        task_dir=tmp_path / "task",
        job_config={
            "n_attempts": 1,
            "n_concurrent_trials": 1,
            "retry": {"max_retries": 0},
        },
        agent_config={"name": "pi", "kwargs": {"version": PI_VERSION}},
        environment_config={"type": "docker", "delete": True, "mounts": []},
    )


def probes(
    *,
    python: tuple[int, int] = (3, 12),
    harbor: str = HARBOR_VERSION,
    docker: str | None = "/usr/bin/docker",
    returncode: int = 0,
) -> PreflightProbes:
    return PreflightProbes(
        python_version=lambda: python,
        harbor_version=lambda: harbor,
        find_executable=lambda _name: docker,
        run_command=lambda argv: subprocess.CompletedProcess(argv, returncode, "ok", "error"),
    )


@pytest.mark.parametrize(
    ("probe", "message"),
    [
        (probes(python=(3, 13)), "Python 3.12"),
        (probes(harbor="0.18.1"), "Harbor 0.18.0"),
        (probes(docker=None), "docker executable"),
        (probes(returncode=1), "docker daemon"),
    ],
)
def test_preflight_rejects_setup_failures_before_output_creation(
    tmp_path: Path, probe: PreflightProbes, message: str
) -> None:
    output = tmp_path / "output"
    with pytest.raises(PreflightError, match=message):
        preflight(output, valid_translation(tmp_path), probes=probe)
    assert not output.exists()


def test_preflight_enforces_job_security_invariants(tmp_path: Path) -> None:
    translation = valid_translation(tmp_path)
    preflight(tmp_path / "valid", translation, probes=probes())

    unsafe_values = [
        ("job_config", "n_attempts", 2),
        ("job_config", "n_concurrent_trials", 2),
        ("job_config", "retry", {"max_retries": 1}),
        ("environment_config", "type", "daytona"),
        ("environment_config", "delete", False),
        ("environment_config", "mounts", [{"source": "/", "target": "/host"}]),
    ]
    for section, field, value in unsafe_values:
        candidate = valid_translation(tmp_path)
        getattr(candidate, section)[field] = value
        with pytest.raises(PreflightError, match="security invariant"):
            preflight(tmp_path / f"unsafe-{field}", candidate, probes=probes())


class FakeHarborResult:
    def __init__(self, exception_type: str | None = None) -> None:
        self.id = "00000000-0000-4000-8000-000000000001"
        self.trial_results = [
            SimpleNamespace(
                id="00000000-0000-4000-8000-000000000002",
                trial_name="task__attempt",
                trial_uri="local://task__attempt",
                task_checksum="sha256:task",
                exception_info=(
                    SimpleNamespace(exception_type=exception_type, exception_message="failure")
                    if exception_type
                    else None
                ),
                verifier_result=SimpleNamespace(
                    rewards={"reward": 1.0, "command_exit": 1.0, "stdout_assertion": 1.0}
                ),
            )
        ]

    def model_dump(self, **_kwargs: object) -> dict[str, object]:
        trial = self.trial_results[0]
        return {
            "id": self.id,
            "trial_results": [
                {
                    "id": trial.id,
                    "trial_name": trial.trial_name,
                    "trial_uri": trial.trial_uri,
                    "task_checksum": trial.task_checksum,
                    "exception_info": (
                        {"exception_type": trial.exception_info.exception_type}
                        if trial.exception_info
                        else None
                    ),
                    "verifier_result": {"rewards": trial.verifier_result.rewards},
                }
            ],
        }


class RecordingJob:
    def __init__(self, config: Any, exception_type: str | None = None) -> None:
        self.config = config
        self.job_dir = config.jobs_dir / config.job_name
        self.exception_type = exception_type

    async def run(self) -> FakeHarborResult:
        result = FakeHarborResult(self.exception_type)
        trial_name = result.trial_results[0].trial_name
        workspace = f"{trial_name}/verifier/workspace"
        files: dict[str, bytes] = {
            "config.json": self.config.model_dump_json().encode(),
            "lock.json": b"{}\n",
            f"{trial_name}/config.json": b"{}\n",
            f"{trial_name}/lock.json": b"{}\n",
            f"{trial_name}/result.json": json.dumps(
                result.model_dump()["trial_results"][0]
            ).encode(),
            f"{trial_name}/trial.log": b"trial log\n",
            f"{trial_name}/agent/pi.txt": b"raw pi bytes\xff",
            f"{trial_name}/verifier/verification.json": json.dumps(
                {
                    "started_at": "2026-07-11T00:00:00+00:00",
                    "ended_at": "2026-07-11T00:00:01+00:00",
                    "duration_ms": 1000,
                    "timed_out": False,
                    "exit_code": 0,
                    "expected_exit_code": 0,
                    "command_exit_passed": True,
                    "stdout_assertion_passed": True,
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "verifier_outcome": "passed",
                    "infrastructure_outcome": "succeeded",
                }
            ).encode(),
            f"{trial_name}/verifier/stdout.txt": b"passed\n",
            f"{trial_name}/verifier/stderr.txt": b"",
            f"{trial_name}/verifier/reward.json": json.dumps(
                {"reward": 1.0, "command_exit": 1.0, "stdout_assertion": 1.0}
            ).encode(),
            f"{trial_name}/artifacts/manifest.json": b"{}\n",
            f"{workspace}/before-manifest.json": b'{"complete":true,"files":[],"omissions":[]}\n',
            f"{workspace}/after-manifest.json": b'{"complete":true,"files":[],"omissions":[]}\n',
            f"{workspace}/patch.diff": b"patch\n",
            f"{workspace}/changed-files.json": (
                b'[{"path":"calculator.py","status":"modified"}]\n'
            ),
        }
        for relative, data in files.items():
            path = self.job_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return result


def backend_run(
    tmp_path: Path,
    *,
    exception_type: str | None = None,
    inspector: Any = lambda _identifier: [],
) -> tuple[Any, list[Any]]:
    captured: list[Any] = []

    async def factory(config: Any) -> RecordingJob:
        captured.append(config)
        return RecordingJob(config, exception_type)

    backend = HarborBackend(
        job_factory=factory,
        docker_inspector=inspector,
        now=lambda: datetime(2026, 7, 11, tzinfo=UTC),
    )
    identity = create_attempt_identity("logical", attempt_id_factory=lambda: "attempt")
    result = asyncio.run(
        backend.run_attempt(
            valid_translation(tmp_path),
            identity,
            tmp_path / "results",
            _fixture_profile=_FIXTURE_PROFILE_TOKEN,
        )
    )
    return result, captured


def test_backend_constructs_exact_harbor_job_and_ingests_result(tmp_path: Path) -> None:
    from harbor.models.job.config import JobConfig

    result, captured = backend_run(tmp_path)

    assert len(captured) == 1 and isinstance(captured[0], JobConfig)
    config = captured[0]
    assert config.n_attempts == config.n_concurrent_trials == 1
    assert config.retry.max_retries == 0
    assert config.environment.type.value == "docker"
    assert config.environment.mounts == [] and config.environment.delete is True
    assert config.agents[0].name == "pi"
    assert config.agents[0].kwargs == {"version": PI_VERSION}
    assert len(config.tasks) == len(config.agents) == 1
    assert result.run["harbor_job_id"] == "00000000-0000-4000-8000-000000000001"
    assert result.run["harbor_trial_id"] == "00000000-0000-4000-8000-000000000002"
    assert result.run["trial_name"] == "task__attempt"
    assert result.run["task_checksum"] == "sha256:task"
    assert result.evidence_status == EvidenceStatus.FIXTURE_COMPLETE
    assert (result.bundle_dir / "agent/pi.txt").read_bytes() == b"raw pi bytes\xff"


def test_backend_derives_verifier_failure_from_rewards_not_result_presence(
    tmp_path: Path,
) -> None:
    class FailedRewardJob(RecordingJob):
        async def run(self) -> FakeHarborResult:
            result = await super().run()
            result.trial_results[0].verifier_result.rewards["reward"] = 0.0
            trial_name = result.trial_results[0].trial_name
            reward = self.job_dir / trial_name / "verifier/reward.json"
            reward.write_text(
                '{"reward":0,"command_exit":1,"stdout_assertion":0}\n', encoding="utf-8"
            )
            return result

    async def factory(config: Any) -> FailedRewardJob:
        return FailedRewardJob(config)

    backend = HarborBackend(job_factory=factory, docker_inspector=lambda _identifier: [])
    result = asyncio.run(
        backend.run_attempt(
            valid_translation(tmp_path),
            create_attempt_identity("logical", attempt_id_factory=lambda: "attempt"),
            tmp_path / "results",
            _fixture_profile=_FIXTURE_PROFILE_TOKEN,
        )
    )
    assert result.run["verifier_outcome"] == "failed"
    assert result.evidence_status == EvidenceStatus.INSUFFICIENT


@pytest.mark.parametrize(
    ("exception_type", "agent", "verifier", "infrastructure"),
    [
        ("NonZeroAgentExitCodeError", "failed", "passed", "succeeded"),
        ("AgentTimeoutError", "timed_out", "passed", "succeeded"),
        ("VerifierOutputParseError", "succeeded", "failed", "failed"),
        ("VerifierTimeoutError", "succeeded", "timed_out", "failed"),
        ("ArtifactDownloadError", "succeeded", "not_run", "failed"),
        ("EnvironmentStartError", "failed", "not_run", "failed"),
    ],
)
def test_backend_preserves_failure_dimensions(
    tmp_path: Path,
    exception_type: str,
    agent: str,
    verifier: str,
    infrastructure: str,
) -> None:
    result, _ = backend_run(tmp_path, exception_type=exception_type)
    assert result.run["agent_outcome"] == agent
    assert result.run["verifier_outcome"] == verifier
    assert result.run["infrastructure_outcome"] == infrastructure
    assert result.evidence_status == EvidenceStatus.INSUFFICIENT


def test_backend_records_job_setup_failure_without_inventing_identifiers(tmp_path: Path) -> None:
    async def factory(_config: Any) -> Any:
        raise RuntimeError("setup exploded")

    backend = HarborBackend(job_factory=factory, docker_inspector=lambda _identifier: [])
    result = asyncio.run(
        backend.run_attempt(
            valid_translation(tmp_path),
            create_attempt_identity("logical", attempt_id_factory=lambda: "attempt"),
            tmp_path / "results",
        )
    )
    assert result.run["infrastructure_outcome"] == "failed"
    assert "setup exploded" in result.run["error"]
    assert "harbor_job_id" not in result.run and "harbor_trial_id" not in result.run
    assert result.cleanup["status"] == "unknown"
    assert result.evidence_status == EvidenceStatus.INSUFFICIENT


def test_backend_persists_cleanup_before_propagating_cancellation(tmp_path: Path) -> None:
    inspected: list[str] = []

    async def factory(config: Any) -> Any:
        class CancelledJob:
            job_dir = config.jobs_dir / config.job_name

            async def run(self) -> object:
                trial = self.job_dir / "cancelled-trial"
                trial.mkdir(parents=True)
                raise asyncio.CancelledError

        return CancelledJob()

    backend = HarborBackend(
        job_factory=factory,
        docker_inspector=lambda identifier: inspected.append(identifier) or [],
    )
    output = tmp_path / "results"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            backend.run_attempt(
                valid_translation(tmp_path),
                create_attempt_identity("logical", attempt_id_factory=lambda: "attempt"),
                output,
            )
        )
    bundle = output / "logical/attempt"
    cleanup = json.loads((bundle / "cleanup.json").read_text(encoding="utf-8"))
    run = json.loads((bundle / "run.json").read_text(encoding="utf-8"))
    assert cleanup["status"] == "confirmed"
    assert run["infrastructure_outcome"] == "failed"
    assert "cancelled-trial__env" in inspected


def test_backend_rejects_incompatible_harbor_result_layout(tmp_path: Path) -> None:
    async def factory(config: Any) -> Any:
        class BadJob:
            job_dir = config.jobs_dir / config.job_name

            async def run(self) -> object:
                return object()

        return BadJob()

    backend = HarborBackend(job_factory=factory, docker_inspector=lambda _identifier: [])
    result = asyncio.run(
        backend.run_attempt(
            valid_translation(tmp_path),
            create_attempt_identity("logical", attempt_id_factory=lambda: "attempt"),
            tmp_path / "results",
        )
    )
    assert result.run["infrastructure_outcome"] == "failed"
    assert "incompatible Harbor result layout" in result.run["error"]
    assert result.evidence_status == EvidenceStatus.INSUFFICIENT


def test_cleanup_is_confirmed_on_success_and_failure_paths(tmp_path: Path) -> None:
    inspected: list[str] = []

    def inspector(identifier: str) -> list[str]:
        inspected.append(identifier)
        return []

    for index, exception_type in enumerate((None, "AgentTimeoutError", "VerifierTimeoutError")):
        case = tmp_path / str(index)
        result, _ = backend_run(case, exception_type=exception_type, inspector=inspector)
        assert result.cleanup["status"] == "confirmed"
        assert result.cleanup["requested"] is True
        assert result.cleanup["started_at"] and result.cleanup["ended_at"]
    assert "task__attempt__env" in inspected
    assert "task__attempt__verifier__trial" in inspected


def test_cleanup_inspection_error_is_persisted(tmp_path: Path) -> None:
    def inspector(_identifier: str) -> list[str]:
        raise RuntimeError("inspection failed")

    result, _ = backend_run(tmp_path, inspector=inspector)
    assert result.cleanup["status"] == "unknown"
    assert result.cleanup["error"] == "inspection failed"
    assert result.run["infrastructure_outcome"] == "failed"
    assert result.evidence_status == EvidenceStatus.INSUFFICIENT


def test_cleanup_failure_changes_infrastructure_and_evidence(tmp_path: Path) -> None:
    result, _ = backend_run(tmp_path, inspector=lambda _identifier: ["container-id"])

    assert result.cleanup["status"] == "failed"
    assert result.cleanup["remaining_container_ids"] == ["container-id", "container-id"]
    assert result.run["infrastructure_outcome"] == "failed"
    assert result.evidence_status == EvidenceStatus.INSUFFICIENT


def test_runner_dispatches_harbor_tasks_as_independent_attempts_without_host_pi(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "harbor-coding-task"
    fixture.mkdir()
    for name in ("calculator.py", "verify.py", "agent.py"):
        source = ROOT / "tests/fixtures/harbor-coding-task" / name
        (fixture / name).write_bytes(source.read_bytes())
    pack_path = tmp_path / "pack.yaml"
    pack_path.write_text(
        """version: '1'
id: harbor-pack
revision: '1'
name: Harbor pack
allow_agents: true
tasks:
  - id: calculator
    category: coding
    lane: pi-coding
    prompt: Fix the calculator.
    fixture: harbor-coding-task
    verifier:
      argv: [python, /tests/hidden_verify.py, /workspace]
      stdout_contains: fixture passed
  - id: calculator-second
    category: coding
    lane: pi-coding
    prompt: Fix it independently.
    fixture: harbor-coding-task
    verifier:
      argv: [python, /tests/hidden_verify.py, /workspace]
      stdout_contains: fixture passed
configurations:
  - id: harbor
    provider: test
    model: deterministic
    adapter: harbor
    harness: {name: harbor, version: '0.18.0'}
""",
        encoding="utf-8",
    )
    calls: list[str] = []

    class FakeBackend:
        async def run_attempt(
            self,
            translation: Any,
            identity: Any,
            output_root: Path,
            **_kwargs: object,
        ) -> Any:
            calls.append("backend")
            bundle = output_root / identity.logical_run_id / identity.attempt_id
            bundle.mkdir(parents=True)
            run = {
                "bundle_version": "1",
                "logical_run_id": identity.logical_run_id,
                "attempt_id": identity.attempt_id,
                "harbor_version": "0.18.0",
                "pi_version": "0.80.6",
                "agent_log_format": "raw_filtered_pi_events",
                "evidence_status": "insufficient",
                "agent_outcome": "succeeded",
                "verifier_outcome": "passed",
                "infrastructure_outcome": "succeeded",
            }
            (bundle / "run.json").write_text(json.dumps(run), encoding="utf-8")
            return SimpleNamespace(bundle_dir=bundle, run=run)

    def checked_preflight(output: Path, _translation: Any) -> None:
        assert not output.exists()
        calls.append("preflight")

    def forbidden_host_pi(*_args: object) -> Any:
        raise AssertionError("host Pi must not run for Harbor")

    attempt_ids = iter(("attempt-one", "attempt-two"))
    run_dir = run_pack(
        pack_path,
        tmp_path / "run",
        allow_agents=True,
        harbor_backend=FakeBackend(),
        harbor_preflight=checked_preflight,
        host_pi=forbidden_host_pi,
        attempt_id_factory=lambda: next(attempt_ids),
    )

    assert calls == ["preflight", "preflight", "backend", "backend"]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["result_count"] == 2
    assert [Path(item["bundle"]).name for item in manifest["results"]] == [
        "attempt-one",
        "attempt-two",
    ]
    assert validate_artifact(run_dir / "blindbench.json") == 2


def test_issue_six_adds_quality_eligible_without_changing_fixture_state() -> None:
    assert {state.value for state in EvidenceStatus} == {
        "non_quality",
        "insufficient",
        "fixture_complete",
        "quality_eligible",
    }
    assert (
        TypeAdapter(EvidenceStatus).validate_python("quality_eligible")
        is EvidenceStatus.QUALITY_ELIGIBLE
    )
