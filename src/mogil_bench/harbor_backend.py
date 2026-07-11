from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast

from .models import AttemptIdentity, EvidenceStatus

HARBOR_VERSION = "0.18.0"
PI_VERSION = "0.80.6"
_FIXTURE_PROFILE_TOKEN = object()


class _Job(Protocol):
    job_dir: Path

    async def run(self) -> object: ...


class _Translation(Protocol):
    @property
    def task_dir(self) -> Path: ...

    @property
    def job_config(self) -> dict[str, object]: ...

    @property
    def agent_config(self) -> dict[str, object]: ...

    @property
    def environment_config(self) -> dict[str, object]: ...


JobFactory = Callable[[object], Awaitable[_Job]]
DockerInspector = Callable[[str], list[str]]


class PreflightError(RuntimeError):
    pass


def _run_docker_info(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, check=False, text=True, timeout=10)


@dataclass(frozen=True)
class PreflightProbes:
    python_version: Callable[[], tuple[int, int]] = field(
        default=lambda: (sys.version_info.major, sys.version_info.minor)
    )
    harbor_version: Callable[[], str] = field(default=lambda: version("harbor"))
    find_executable: Callable[[str], str | None] = shutil.which
    run_command: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = _run_docker_info


def require_harbor(
    *,
    version_getter: Callable[[str], str] = version,
    importer: Callable[[str], object] = import_module,
) -> ModuleType:
    """Load Harbor only after enforcing Mogil Bench's exact compatibility pin."""
    try:
        installed = version_getter("harbor")
    except PackageNotFoundError as error:
        message = f"Mogil Bench requires Harbor {HARBOR_VERSION}; it is not installed"
        raise RuntimeError(message) from error
    if installed != HARBOR_VERSION:
        raise RuntimeError(
            f"Mogil Bench requires Harbor {HARBOR_VERSION}; installed {installed}"
        )
    module = importer("harbor")
    if not isinstance(module, ModuleType):
        raise RuntimeError("Harbor importer did not return a module")
    return module


def _security_invariants(translation: _Translation) -> bool:
    try:
        job = translation.job_config
        environment = translation.environment_config
        retry = job["retry"]
        return bool(
            isinstance(retry, dict)
            and job["n_attempts"] == 1
            and job["n_concurrent_trials"] == 1
            and retry["max_retries"] == 0
            and environment["type"] == "docker"
            and environment["delete"] is True
            and environment["mounts"] == []
        )
    except (AttributeError, KeyError, TypeError):
        return False


def preflight(
    output_dir: Path,
    translation: _Translation,
    *,
    probes: PreflightProbes | None = None,
) -> None:
    """Validate the fixed local-Docker boundary without creating output files."""
    probes = probes or PreflightProbes()
    if output_dir.exists():
        raise PreflightError(f"output path already exists: {output_dir}")
    if probes.python_version() != (3, 12):
        raise PreflightError("Mogil Harbor runs require Python 3.12.x")
    try:
        installed_harbor = probes.harbor_version()
    except PackageNotFoundError as error:
        raise PreflightError(f"Harbor {HARBOR_VERSION} is not installed") from error
    if installed_harbor != HARBOR_VERSION:
        raise PreflightError(
            f"Mogil Harbor runs require Harbor {HARBOR_VERSION}; installed {installed_harbor}"
        )
    docker = probes.find_executable("docker")
    if not docker:
        raise PreflightError("docker executable not found; install Docker and add it to PATH")
    result = probes.run_command([docker, "info"])
    if result.returncode != 0:
        raise PreflightError(
            "docker daemon is unreachable; start Docker and verify `docker info` succeeds"
        )

    if not _security_invariants(translation):
        raise PreflightError(
            "Harbor security invariant failed: Docker only, one attempt, concurrency 1, "
            "retries 0, delete true, and empty mounts are required"
        )


@dataclass(frozen=True)
class HarborAttemptResult:
    bundle_dir: Path
    run: dict[str, Any]
    cleanup: dict[str, Any]
    evidence_status: EvidenceStatus


def _sanitize_project_name(identifier: str) -> str:
    value = identifier.lower()
    if not re.match(r"^[a-z0-9]", value):
        value = "0" + value
    return re.sub(r"[^a-z0-9_-]", "-", value)


def _inspect_docker_project(project: str) -> list[str]:
    completed = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.ID}}",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError("docker post-cleanup inspection failed")
    return [line for line in completed.stdout.splitlines() if line]


async def _create_actual_job(config: object) -> _Job:
    require_harbor()
    from harbor.job import Job  # type: ignore[import-untyped]
    from harbor.models.job.config import JobConfig  # type: ignore[import-untyped]

    if not isinstance(config, JobConfig):
        raise TypeError("expected Harbor JobConfig")
    return cast(_Job, await Job.create(config))


def _json_file(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _exception_type(trial: object) -> str | None:
    exception = getattr(trial, "exception_info", None)
    value = getattr(exception, "exception_type", None)
    return value if isinstance(value, str) else None


def _outcomes(trial: object | None) -> tuple[str, str, str]:
    if trial is None:
        return "failed", "not_run", "failed"
    exception = _exception_type(trial)
    verifier_result = getattr(trial, "verifier_result", None)
    rewards = getattr(verifier_result, "rewards", None)
    if verifier_result is None:
        verifier = "not_run"
    elif isinstance(rewards, dict) and all(
        rewards.get(key) == 1
        for key in ("reward", "command_exit", "stdout_assertion")
    ):
        verifier = "passed"
    else:
        verifier = "failed"
    if exception is None:
        infrastructure = "failed" if verifier == "not_run" else "succeeded"
        return "succeeded", verifier, infrastructure
    if exception == "AgentTimeoutError":
        return "timed_out", verifier, "succeeded"
    if exception == "NonZeroAgentExitCodeError" or exception.startswith("Agent"):
        return "failed", verifier, "succeeded"
    if exception == "VerifierTimeoutError":
        return "succeeded", "timed_out", "failed"
    if exception.startswith("Verifier") or exception.startswith("Reward"):
        return "succeeded", "failed", "failed"
    if "Artifact" in exception:
        return "succeeded", "not_run", "failed"
    return "failed", "not_run", "failed"


class HarborBackend:
    def __init__(
        self,
        *,
        job_factory: JobFactory = _create_actual_job,
        docker_inspector: DockerInspector = _inspect_docker_project,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._job_factory = job_factory
        self._docker_inspector = docker_inspector
        self._now = now

    @staticmethod
    def _job_config(translation: _Translation, jobs_dir: Path, attempt_id: str) -> object:
        require_harbor()
        from harbor.models.job.config import JobConfig
        from harbor.models.trial.config import (  # type: ignore[import-untyped]
            AgentConfig,
            EnvironmentConfig,
            TaskConfig,
        )

        if not _security_invariants(translation):
            raise PreflightError("Harbor security invariant failed immediately before job creation")
        values = dict(translation.job_config)
        values.update(
            {
                "job_name": f"mogil-{attempt_id}",
                "jobs_dir": jobs_dir,
                "quiet": True,
                "tasks": [TaskConfig(path=translation.task_dir)],
                "agents": [AgentConfig(**translation.agent_config)],
                "environment": EnvironmentConfig(**translation.environment_config),
            }
        )
        return JobConfig(**values)

    async def run_attempt(
        self,
        translation: _Translation,
        identity: AttemptIdentity,
        output_root: Path,
        *,
        _fixture_profile: object | None = None,
    ) -> HarborAttemptResult:
        from .run_bundle import classify_evidence, collect_files, write_checksums

        deterministic_fixture = _fixture_profile is _FIXTURE_PROFILE_TOKEN
        logical_run_id = identity.logical_run_id
        attempt_id = identity.attempt_id
        if not _security_invariants(translation):
            raise PreflightError("Harbor security invariant failed before output creation")
        bundle_dir = output_root / logical_run_id / attempt_id
        bundle_dir.mkdir(parents=True, exist_ok=False)
        cleanup_started = self._now().isoformat()
        result_object: object | None = None
        trial: object | None = None
        job: _Job | None = None
        error: str | None = None
        cancellation: asyncio.CancelledError | None = None
        with tempfile.TemporaryDirectory(prefix="mogil-harbor-job-") as temporary:
            jobs_dir = Path(temporary)
            try:
                config = self._job_config(translation, jobs_dir, attempt_id)
                job = await self._job_factory(config)
                result_object = await job.run()
                trials = getattr(result_object, "trial_results", None)
                if not callable(getattr(result_object, "model_dump", None)) or not isinstance(
                    trials, list
                ) or len(trials) != 1:
                    raise RuntimeError("incompatible Harbor result layout: expected one trial")
                trial = trials[0]
            except asyncio.CancelledError as exception:
                cancellation = exception
                error = "Harbor execution cancelled"
            except Exception as exception:
                error = str(exception)
                if "incompatible Harbor result layout" not in error and result_object is not None:
                    error = f"Harbor execution failed: {error}"

            trial_name = getattr(trial, "trial_name", None)
            if not isinstance(trial_name, str) and job is not None and job.job_dir.exists():
                trial_directories = sorted(
                    path for path in job.job_dir.iterdir() if path.is_dir()
                )
                if len(trial_directories) == 1:
                    trial_name = trial_directories[0].name
            identifiers = (
                [f"{trial_name}__env", f"{trial_name}__verifier__trial"]
                if isinstance(trial_name, str) and trial_name
                else []
            )
            project_labels = [_sanitize_project_name(identifier) for identifier in identifiers]
            remaining: list[str] = []
            cleanup_error: str | None = None
            if project_labels:
                try:
                    for project_label in project_labels:
                        remaining.extend(self._docker_inspector(project_label))
                    cleanup_status = "failed" if remaining else "confirmed"
                    if remaining:
                        cleanup_error = "Docker containers remain after Harbor cleanup"
                except Exception as exception:
                    cleanup_status = "unknown"
                    cleanup_error = str(exception)
            else:
                cleanup_status = "unknown"
                cleanup_error = "trial identifiers unavailable for cleanup confirmation"
            cleanup = {
                "requested": True,
                "started_at": cleanup_started,
                "ended_at": self._now().isoformat(),
                "status": cleanup_status,
                "project_identifiers": identifiers,
                "compose_project_labels": project_labels,
                "remaining_container_ids": remaining,
                "error": cleanup_error,
            }

            agent_outcome, verifier_outcome, infrastructure_outcome = _outcomes(trial)
            if error or cleanup_status != "confirmed":
                infrastructure_outcome = "failed"
            run: dict[str, Any] = {
                "bundle_version": "1",
                "logical_run_id": logical_run_id,
                "attempt_id": attempt_id,
                "harbor_version": HARBOR_VERSION,
                "pi_version": PI_VERSION,
                "agent_log_format": (
                    "deterministic_test_agent_log"
                    if deterministic_fixture
                    else "raw_filtered_pi_events"
                ),
                "agent_outcome": agent_outcome,
                "verifier_outcome": verifier_outcome,
                "infrastructure_outcome": infrastructure_outcome,
            }
            if error:
                run["error"] = error
            if result_object is not None:
                job_id = getattr(result_object, "id", None)
                if job_id is not None:
                    run["harbor_job_id"] = str(job_id)
            if trial is not None:
                for source, destination in (
                    ("id", "harbor_trial_id"),
                    ("trial_name", "trial_name"),
                    ("trial_uri", "trial_uri"),
                    ("task_checksum", "task_checksum"),
                ):
                    value = getattr(trial, source, None)
                    if value is not None:
                        run[destination] = str(value)

            _json_file(bundle_dir / "run.json", run)
            from .harbor_tasks import PYTHON_BASE_IMAGE

            environment = {
                "type": "docker",
                "base_image": PYTHON_BASE_IMAGE,
                "delete": True,
                "mounts": [],
                "requested": translation.environment_config,
                "effective": translation.environment_config,
            }
            _json_file(bundle_dir / "environment.json", environment)
            _json_file(bundle_dir / "cleanup.json", cleanup)

            if job is not None and isinstance(trial_name, str):
                source_root = job.job_dir
                workspace_source = f"{trial_name}/verifier/workspace"
                sources = {
                    "harbor/job-config.json": "config.json",
                    "harbor/job-lock.json": "lock.json",
                    "harbor/trial-config.json": f"{trial_name}/config.json",
                    "harbor/trial-lock.json": f"{trial_name}/lock.json",
                    "harbor/trial-result.json": f"{trial_name}/result.json",
                    "harbor/trial.log": f"{trial_name}/trial.log",
                    "agent/pi.txt": f"{trial_name}/agent/pi.txt",
                    "verifier/verification.json": f"{trial_name}/verifier/verification.json",
                    "verifier/stdout.txt": f"{trial_name}/verifier/stdout.txt",
                    "verifier/stderr.txt": f"{trial_name}/verifier/stderr.txt",
                    "verifier/reward.json": f"{trial_name}/verifier/reward.json",
                    "artifacts/harbor-manifest.json": f"{trial_name}/artifacts/manifest.json",
                    "workspace/before-manifest.json": f"{workspace_source}/before-manifest.json",
                    "workspace/after-manifest.json": f"{workspace_source}/after-manifest.json",
                    "workspace/patch.diff": f"{workspace_source}/patch.diff",
                    "workspace/changed-files.json": f"{workspace_source}/changed-files.json",
                }
                available_sources = {
                    destination: source
                    for destination, source in sources.items()
                    if (source_root / source).exists()
                }
                try:
                    collect_files(source_root, bundle_dir, available_sources)
                except Exception as exception:
                    run["artifact_collection_error"] = str(exception)
                    run["infrastructure_outcome"] = "failed"

        write_checksums(bundle_dir)
        evidence = classify_evidence(
            bundle_dir,
            deterministic_fixture=deterministic_fixture,
        )
        run["evidence_status"] = evidence.value
        _json_file(bundle_dir / "run.json", run)
        write_checksums(bundle_dir)
        if cancellation is not None:
            raise cancellation
        return HarborAttemptResult(
            bundle_dir=bundle_dir,
            run=run,
            cleanup=cleanup,
            evidence_status=evidence,
        )
