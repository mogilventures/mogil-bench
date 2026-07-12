from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
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
    daytona_credentials: Callable[[], bool] = field(
        default=lambda: bool(os.environ.get("DAYTONA_API_KEY"))
        or bool(
            os.environ.get("DAYTONA_JWT_TOKEN")
            and os.environ.get("DAYTONA_ORGANIZATION_ID")
        )
    )
    daytona_importable: Callable[[], bool] = field(
        default=lambda: __import__("importlib.util").util.find_spec("daytona") is not None
    )
    plaintext_secret_present: Callable[[Sequence[str]], bool] = field(
        default=lambda names: any(os.environ.get(name) for name in names)
    )


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


_DAYTONA_IMPORT_PATH = "mogil_bench.harbor_daytona:MogilDaytonaEnvironment"


def _environment_provider(translation: _Translation) -> str | None:
    environment = translation.environment_config
    if environment.get("type") == "docker" and environment.get("import_path") is None:
        return "docker"
    if environment.get("import_path") == _DAYTONA_IMPORT_PATH and environment.get("type") is None:
        return "daytona"
    return None


def _canonical_policy(translation: _Translation) -> dict[str, object] | None:
    provider = _environment_provider(translation)
    if provider == "docker" and not (translation.task_dir / "task.toml").is_file():
        from .harbor_tasks import (
            AGENT_CPUS,
            AGENT_MEMORY_MB,
            AGENT_STORAGE_MB,
            PYTHON_BASE_IMAGE,
        )

        return {
            "provider": "docker",
            "image": PYTHON_BASE_IMAGE,
            "cpus": AGENT_CPUS,
            "memory_mb": AGENT_MEMORY_MB,
            "storage_mb": AGENT_STORAGE_MB,
            "network_mode": "public",
            "allowed_hosts": [],
            "verifier_network_mode": "no-network",
            "secret_transport": "host_environment",
            "delete": True,
            "mounts": [],
        }
    try:
        task = tomllib.loads((translation.task_dir / "task.toml").read_text(encoding="utf-8"))
        task_environment = task["environment"]
        verifier_environment = task["verifier"]["environment"]
        config = translation.environment_config
        if provider is None:
            return None
        image = task_environment.get("docker_image")
        if provider == "docker":
            dockerfile = (translation.task_dir / "environment/Dockerfile").read_text(
                encoding="utf-8"
            )
            first_line = dockerfile.splitlines()[0]
            image = first_line.removeprefix("FROM ")
        return {
            "provider": provider,
            "image": image,
            "cpus": task_environment["cpus"],
            "memory_mb": task_environment["memory_mb"],
            "storage_mb": task_environment["storage_mb"],
            "network_mode": task_environment["network_mode"],
            "allowed_hosts": task_environment.get("allowed_hosts", []),
            "verifier_network_mode": verifier_environment["network_mode"],
            "secret_transport": (
                "restricted_reference" if provider == "daytona" else "host_environment"
            ),
            "delete": config["delete"],
            "mounts": config["mounts"],
        }
    except (KeyError, OSError, TypeError, tomllib.TOMLDecodeError):
        return None


def _daytona_effective_policy(
    receipt_dir: Path | None,
    *,
    attempt_id: str,
    requested: dict[str, object] | None,
    cleanup_receipts: list[dict[str, Any]],
    expected_sessions: dict[str, str],
) -> tuple[dict[str, object] | None, dict[str, Path]]:
    receipt_paths: dict[str, Path] = {}
    if receipt_dir is None or requested is None:
        return None, receipt_paths
    try:
        receipts: dict[str, tuple[dict[str, object], Path]] = {}
        for index, path in enumerate(sorted(receipt_dir.glob("*.json"))):
            receipt_paths[f"unverified-{index}"] = path
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None, receipt_paths
            role = raw.get("role")
            if role not in {"agent", "verifier"} or role in receipts:
                return None, receipt_paths
            receipts[role] = (raw, path)
        if set(receipts) != {"agent", "verifier"}:
            return None, receipt_paths
        requested_cpus = requested.get("cpus")
        requested_memory_mb = requested.get("memory_mb")
        requested_storage_mb = requested.get("storage_mb")
        requested_allowed_hosts = requested.get("allowed_hosts")
        if (
            not isinstance(requested_cpus, int)
            or isinstance(requested_cpus, bool)
            or not isinstance(requested_memory_mb, int)
            or isinstance(requested_memory_mb, bool)
            or not isinstance(requested_storage_mb, int)
            or isinstance(requested_storage_mb, bool)
            or not isinstance(requested_allowed_hosts, list)
            or any(not isinstance(host, str) for host in requested_allowed_hosts)
        ):
            return None, receipt_paths
        effective_by_role: dict[str, dict[str, object]] = {}
        sandbox_ids: set[str] = set()
        for role, (receipt, _path) in receipts.items():
            effective = receipt.get("effective")
            create_parameters = receipt.get("create_parameters")
            sandbox_id = receipt.get("sandbox_id")
            if (
                set(receipt)
                != {
                    "version",
                    "attempt_id",
                    "session_id",
                    "sandbox_id",
                    "role",
                    "source",
                    "effective",
                    "create_parameters",
                    "status",
                    "error",
                }
                or receipt.get("version") != "1"
                or receipt.get("attempt_id") != attempt_id
                or receipt.get("source") != "daytona_provider_refresh"
                or receipt.get("status") != "verified"
                or receipt.get("error") is not None
                or receipt.get("session_id") != expected_sessions.get(role)
                or not isinstance(sandbox_id, str)
                or not sandbox_id
                or sandbox_id in sandbox_ids
                or not isinstance(effective, dict)
                or set(effective)
                != {
                    "cpus",
                    "memory_mb",
                    "storage_mb",
                    "network_mode",
                    "allowed_hosts",
                    "attempt_label_verified",
                    "runtime_prerequisites_verified",
                }
                or not isinstance(create_parameters, dict)
                or set(create_parameters) != {"secret_references_attached"}
                or not isinstance(
                    create_parameters.get("secret_references_attached"), bool
                )
            ):
                return None, receipt_paths
            sandbox_ids.add(sandbox_id)
            cpus = effective.get("cpus")
            memory_mb = effective.get("memory_mb")
            storage_mb = effective.get("storage_mb")
            allowed_hosts = effective.get("allowed_hosts")
            if (
                not isinstance(cpus, (int, float))
                or isinstance(cpus, bool)
                or not math.isfinite(float(cpus))
                or cpus <= 0
                or not isinstance(memory_mb, int)
                or isinstance(memory_mb, bool)
                or not isinstance(storage_mb, int)
                or isinstance(storage_mb, bool)
                or not isinstance(allowed_hosts, list)
                or len(allowed_hosts) > 64
                or any(
                    not isinstance(host, str) or len(host) > 253
                    for host in allowed_hosts
                )
                or effective.get("attempt_label_verified") is not True
                or effective.get("runtime_prerequisites_verified") is not True
                or cpus < requested_cpus
                or memory_mb < requested_memory_mb
                or storage_mb < requested_storage_mb
            ):
                return None, receipt_paths
            secrets_attached = create_parameters["secret_references_attached"]
            if role == "agent":
                if (
                    effective.get("network_mode") != requested["network_mode"]
                    or sorted(allowed_hosts) != sorted(requested_allowed_hosts)
                    or secrets_attached is not True
                ):
                    return None, receipt_paths
            elif (
                effective.get("network_mode") != "no-network"
                or allowed_hosts != []
                or secrets_attached is not False
            ):
                return None, receipt_paths
            effective_by_role[role] = effective
        cleanup_ids = {
            receipt.get("sandbox_id")
            for receipt in cleanup_receipts
            if receipt.get("status") == "confirmed"
        }
        if cleanup_ids != sandbox_ids:
            return None, receipt_paths
        return (
            {
                "source": "provider-reported",
                "agent": effective_by_role["agent"],
                "verifier": effective_by_role["verifier"],
            },
            {role: value[1] for role, value in receipts.items()},
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None, receipt_paths


def _security_invariants(translation: _Translation) -> bool:
    try:
        job = translation.job_config
        environment = translation.environment_config
        retry = job["retry"]
        provider = _environment_provider(translation)
        policy = _canonical_policy(translation)
        if policy is None:
            return False
        common = bool(
            isinstance(retry, dict)
            and job["n_attempts"] == 1
            and job["n_concurrent_trials"] == 1
            and retry["max_retries"] == 0
            and environment["delete"] is True
            and environment["mounts"] == []
            and policy["verifier_network_mode"] == "no-network"
        )
        if not common:
            return False
        if provider == "docker":
            return True
        raw_kwargs = environment.get("kwargs")
        kwargs: dict[str, object] = raw_kwargs if isinstance(raw_kwargs, dict) else {}
        secrets = kwargs.get("secrets")
        image = policy["image"]
        return bool(
            provider == "daytona"
            and isinstance(image, str)
            and re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image)
            and isinstance(policy["cpus"], int)
            and policy["cpus"] >= 1
            and isinstance(policy["memory_mb"], int)
            and policy["memory_mb"] >= 1024
            and isinstance(policy["storage_mb"], int)
            and policy["storage_mb"] >= 4096
            and policy["network_mode"] == "allowlist"
            and isinstance(policy["allowed_hosts"], list)
            and bool(policy["allowed_hosts"])
            and environment.get("cpu_enforcement_policy") == "request"
            and environment.get("memory_enforcement_policy") == "request"
            and environment.get("env", {}) == {}
            and isinstance(secrets, dict)
            and bool(secrets)
            and all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in secrets.items()
            )
            and kwargs.get("attempt_id")
            and isinstance(kwargs.get("max_lifetime_minutes"), int)
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
    provider = _environment_provider(translation)
    if provider == "docker":
        docker = probes.find_executable("docker")
        if not docker:
            raise PreflightError("docker executable not found; install Docker and add it to PATH")
        result = probes.run_command([docker, "info"])
        if result.returncode != 0:
            raise PreflightError(
                "docker daemon is unreachable; start Docker and verify `docker info` succeeds"
            )
    elif provider == "daytona":
        if not probes.daytona_importable():
            raise PreflightError(
                "Daytona support is not installed; install mogil-bench[daytona]"
            )
        if not probes.daytona_credentials():
            raise PreflightError(
                "Daytona credentials unavailable: set DAYTONA_API_KEY or both "
                "DAYTONA_JWT_TOKEN and DAYTONA_ORGANIZATION_ID"
            )
        kwargs = translation.environment_config.get("kwargs")
        secrets = kwargs.get("secrets") if isinstance(kwargs, dict) else None
        secret_names = list(secrets) if isinstance(secrets, dict) else []
        if probes.plaintext_secret_present(secret_names):
            raise PreflightError(
                "Daytona rejects plaintext model credentials in the manager environment; "
                "use only configured organization secret references"
            )

    if not _security_invariants(translation):
        raise PreflightError(
            "Harbor security invariant failed: supported provider, one attempt, concurrency 1, "
            "retries 0, pinned policy, restricted secrets, delete true, and empty "
            "mounts are required"
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
            provider = _environment_provider(translation)
            project_labels: list[str] = []
            remaining: list[str] = []
            receipts: list[dict[str, Any]] = []
            cleanup_error: str | None = None
            if provider == "docker":
                project_labels = [
                    _sanitize_project_name(identifier) for identifier in identifiers
                ]
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
            elif provider == "daytona" and job is not None and isinstance(trial_name, str):
                receipt_dir = job.job_dir / trial_name / "mogil-daytona-cleanup"
                try:
                    for path in sorted(receipt_dir.glob("*.json")):
                        value = json.loads(path.read_text(encoding="utf-8"))
                        if not isinstance(value, dict):
                            raise ValueError("cleanup receipt is not an object")
                        receipts.append(value)
                    cleanup_sandbox_ids = {
                        item.get("sandbox_id") for item in receipts
                    }
                    valid = (
                        len(receipts) == 2
                        and all(
                            set(item)
                            == {
                                "attempt_id",
                                "session_id",
                                "sandbox_id",
                                "status",
                                "error",
                            }
                            for item in receipts
                        )
                        and all(item.get("attempt_id") == attempt_id for item in receipts)
                        and {item.get("session_id") for item in receipts}
                        == set(identifiers)
                        and all(item.get("status") == "confirmed" for item in receipts)
                        and all(item.get("error") is None for item in receipts)
                        and len(cleanup_sandbox_ids) == 2
                        and all(
                            isinstance(sandbox_id, str) and bool(sandbox_id)
                            for sandbox_id in cleanup_sandbox_ids
                        )
                    )
                    cleanup_status = "confirmed" if valid else "unknown"
                    if not valid:
                        cleanup_error = "Daytona deletion confirmation receipts are incomplete"
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exception:
                    cleanup_status = "unknown"
                    cleanup_error = (
                        "Daytona cleanup confirmation failed: "
                        f"{type(exception).__name__}"
                    )
            else:
                cleanup_status = "unknown"
                cleanup_error = "trial identifiers unavailable for cleanup confirmation"
            cleanup = {
                "requested": True,
                "started_at": cleanup_started,
                "ended_at": self._now().isoformat(),
                "status": cleanup_status,
                "resource_identifiers": identifiers,
                "remaining_resource_ids": remaining,
                "error": cleanup_error,
            }
            if provider == "docker":
                cleanup.update(
                    {
                        "project_identifiers": identifiers,
                        "compose_project_labels": project_labels,
                        "remaining_container_ids": remaining,
                    }
                )
            else:
                cleanup["deletion_receipts"] = receipts

            policy = _canonical_policy(translation)
            policy_receipt_paths: dict[str, Path] = {}
            if provider == "docker":
                effective_policy = policy
            else:
                policy_receipt_dir = (
                    job.job_dir / trial_name / "mogil-daytona-policy"
                    if job is not None and isinstance(trial_name, str)
                    else None
                )
                effective_policy, policy_receipt_paths = _daytona_effective_policy(
                    policy_receipt_dir,
                    attempt_id=attempt_id,
                    requested=policy,
                    cleanup_receipts=receipts,
                    expected_sessions={
                        "agent": identifiers[0],
                        "verifier": identifiers[1],
                    }
                    if len(identifiers) == 2
                    else {},
                )
            if effective_policy is None:
                policy_error = (
                    "provider-reported effective sandbox policy is missing, unverified, "
                    "or weaker than requested"
                )
                error = f"{error}; {policy_error}" if error else policy_error
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
            environment: dict[str, object] = {
                "schema_version": "1",
                "class": "isolated-sandbox",
                "provider": provider,
                "delete": True,
                "mounts": [],
                "requested": policy,
                "effective": effective_policy,
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
                for role, path in policy_receipt_paths.items():
                    sources[f"environment/provider-policy-{role}.json"] = str(
                        path.relative_to(source_root)
                    )
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
