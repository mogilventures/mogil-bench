from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

if TYPE_CHECKING:
    from .harbor_backend import HarborAttemptResult
    from .harbor_tasks import HarborTranslation
    from .models import AttemptIdentity

from .models import Configuration, Task
from .packs import (
    canonical_hash,
    configuration_identity_v1,
    pack_fingerprint,
    resolve_fixture,
    task_prompt,
)

MAX_OUTPUT_BYTES = 32_768
PI_EXECUTABLE_ENV = "MOGIL_BENCH_PI_EXECUTABLE"
PI_SYSTEM_PROMPT = (
    "You are running one isolated coding benchmark. Work only on files in the current "
    "benchmark directory, do not access network services, and return a concise final result."
)
PROVIDER_CREDENTIAL_ENV = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "fireworks": ("FIREWORKS_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "xai": ("XAI_API_KEY",),
}
FORBIDDEN_EXECUTABLES = {
    "pi",
    "hermes",
    "git",
    "curl",
    "wget",
    "ssh",
    "scp",
    "rsync",
    "nc",
    "ncat",
    "docker",
    "kubectl",
}


@dataclass(frozen=True)
class Execution:
    status: str
    content: str
    duration_ms: float
    exit_code: int | None = None
    output_truncated: bool = False
    verification_passed: bool | None = None
    stderr: str = ""
    error: str | None = None


class _HarborBackend(Protocol):
    async def run_attempt(
        self,
        translation: HarborTranslation,
        identity: AttemptIdentity,
        output_root: Path,
        *,
        _fixture_profile: object | None = None,
    ) -> HarborAttemptResult: ...


@dataclass(frozen=True)
class RunResult:
    id: str
    task_id: str
    configuration_id: str
    lane: str
    category: str
    privacy_class: str
    provider: str
    model: str
    harness: dict[str, Any]
    prompt: str
    execution: Execution


def _fixture_profile_token() -> object:
    from .harbor_backend import _FIXTURE_PROFILE_TOKEN

    return _FIXTURE_PROFILE_TOKEN


def logical_run_id(fingerprint: str, task: Task, config: Configuration) -> str:
    return "mogil-" + canonical_hash(
        {
            "pack_fingerprint": fingerprint,
            "task": task.model_dump(mode="json"),
            "configuration": configuration_identity_v1(config),
        }
    )


def _bounded(data: bytes) -> tuple[str, bool]:
    truncated = len(data) > MAX_OUTPUT_BYTES
    return data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"), truncated


def _safe_argv(argv: list[str]) -> None:
    executable = Path(argv[0]).name.lower()
    if executable in FORBIDDEN_EXECUTABLES:
        raise ValueError(f"forbidden command executable: {executable}")


def _environment(home: Path) -> dict[str, str]:
    result = {"HOME": str(home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    if "PATH" in os.environ:
        result["PATH"] = os.environ["PATH"]
    return result


def _pi_environment(home: Path, provider: str) -> dict[str, str]:
    result = _environment(home)
    result.update({"PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0"})
    for name in (*PROVIDER_CREDENTIAL_ENV.get(provider.lower(), ()), "PI_CODING_AGENT_DIR"):
        if name in os.environ:
            result[name] = os.environ[name]
    return result


def _invoke(
    argv: list[str],
    cwd: Path,
    timeout: float,
    *,
    environment: dict[str, str] | None = None,
    guard_command: bool = True,
) -> tuple[int, str, str, bool, bool]:
    if guard_command:
        _safe_argv(argv)
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=environment or _environment(cwd),
            capture_output=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
        stdout, stdout_cut = _bounded(completed.stdout)
        stderr, stderr_cut = _bounded(completed.stderr)
        return completed.returncode, stdout, stderr, stdout_cut or stderr_cut, False
    except subprocess.TimeoutExpired as error:
        stdout, stdout_cut = _bounded(error.stdout or b"")
        stderr, stderr_cut = _bounded(error.stderr or b"")
        message = "command timed out"
        if stdout or stderr:
            message += f"\nstdout:\n{stdout}\nstderr:\n{stderr}"
        return 124, "", message, stdout_cut or stderr_cut, True


def _copy_fixture(pack_path: Path, task: Task, workdir: Path) -> None:
    if not task.fixture:
        return
    source = resolve_fixture(pack_path, task)
    destination = workdir / source.name
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def _mock(prompt: str, task: Task) -> Execution:
    digest = canonical_hash({"lane": task.lane.value, "prompt": prompt})[:12]
    if task.lane.value == "hermes-text":
        content = f"Mock draft [{digest}]: concise response prepared for human review."
    else:
        content = f"Mock coding result [{digest}]: fixture analyzed; behavioral checks proposed."
    return Execution(status="succeeded", content=content, duration_ms=0)


def _command(pack_path: Path, task: Task) -> Execution:
    if not task.command:
        return Execution(status="failed", content="", duration_ms=0, error="task has no command")
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="mogil-bench-") as temporary:
            workdir = Path(temporary)
            _copy_fixture(pack_path, task, workdir)
            exit_code, stdout, stderr, truncated, timed_out = _invoke(
                task.command, workdir, task.timeout_seconds
            )
            status = "timed_out" if timed_out else ("succeeded" if exit_code == 0 else "failed")
            verification_passed: bool | None = None
            if status == "succeeded" and task.verifier:
                verifier = task.verifier
                verify_code, verify_stdout, _, verify_cut, _ = _invoke(
                    verifier.argv, workdir, verifier.timeout_seconds
                )
                verification_passed = verify_code == verifier.expected_exit_code and (
                    verifier.stdout_contains is None or verifier.stdout_contains in verify_stdout
                )
                truncated = truncated or verify_cut
                if not verification_passed:
                    status = "verification_failed"
            return Execution(
                status=status,
                content=stdout,
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                exit_code=exit_code,
                output_truncated=truncated,
                verification_passed=verification_passed,
                stderr=stderr,
            )
    except (OSError, ValueError) as error:
        return Execution(
            status="denied" if isinstance(error, ValueError) else "failed",
            content="",
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            error=str(error),
        )


def _resolve_pi_executable() -> Path:
    override = os.environ.get(PI_EXECUTABLE_ENV)
    if override:
        candidate = Path(override)
        if not candidate.is_absolute():
            raise ValueError(f"{PI_EXECUTABLE_ENV} must be an absolute path")
        if candidate.name.lower() not in {"pi", "pi.exe"}:
            raise ValueError(f"{PI_EXECUTABLE_ENV} must identify an executable named pi")
    else:
        discovered = shutil.which("pi")
        if not discovered:
            raise ValueError("pi executable not found on PATH")
        candidate = Path(discovered)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError("configured pi executable does not exist") from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("configured pi executable must be an executable file")
    return resolved


def _pi(pack_path: Path, task: Task, config: Configuration, prompt: str) -> Execution:
    started = time.monotonic()
    try:
        executable = _resolve_pi_executable()
        with tempfile.TemporaryDirectory(prefix="mogil-bench-pi-") as temporary:
            workdir = Path(temporary)
            _copy_fixture(pack_path, task, workdir)
            argv = [
                str(executable),
                "--print",
                "--no-session",
                "--provider",
                config.provider,
                "--model",
                config.model,
                "--tools",
                "read,write,edit",
                "--system-prompt",
                PI_SYSTEM_PROMPT,
                "--no-extensions",
                "--no-skills",
                "--no-prompt-templates",
                "--no-context-files",
                "--no-approve",
                prompt,
            ]
            exit_code, stdout, stderr, truncated, timed_out = _invoke(
                argv,
                workdir,
                task.timeout_seconds,
                environment=_pi_environment(workdir, config.provider),
                guard_command=False,
            )
            status = "timed_out" if timed_out else ("succeeded" if exit_code == 0 else "failed")
            verification_passed: bool | None = None
            if status == "succeeded" and task.verifier:
                verifier = task.verifier
                verify_code, verify_stdout, _, verify_cut, _ = _invoke(
                    verifier.argv, workdir, verifier.timeout_seconds
                )
                verification_passed = verify_code == verifier.expected_exit_code and (
                    verifier.stdout_contains is None or verifier.stdout_contains in verify_stdout
                )
                truncated = truncated or verify_cut
                if not verification_passed:
                    status = "verification_failed"
            return Execution(
                status=status,
                content=stdout,
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                exit_code=exit_code,
                output_truncated=truncated,
                verification_passed=verification_passed,
                stderr=stderr,
            )
    except (OSError, ValueError) as error:
        return Execution(
            status="denied" if isinstance(error, ValueError) else "failed",
            content="",
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            error=str(error),
        )


def _run_pack_to_directory(
    pack_path: Path,
    output_dir: Path,
    *,
    final_output_dir: Path,
    allow_commands: bool = False,
    allow_agents: bool = False,
    harbor_backend: _HarborBackend | None = None,
    harbor_preflight: Callable[[Path, HarborTranslation], None] | None = None,
    host_pi: Callable[[Path, Task, Configuration, str], Execution] = _pi,
    attempt_id_factory: Callable[[], object] | None = None,
    canary_factory: Callable[[], str] | None = None,
    _test_agent_import_path: str | None = None,
) -> Path:
    from .packs import load_pack

    pack = load_pack(pack_path)
    fingerprint = pack_fingerprint(pack_path, pack)
    if any(config.adapter == "command" for config in pack.configurations) and not allow_commands:
        raise PermissionError("command configurations require --allow-commands acknowledgement")
    agent_configs = [config for config in pack.configurations if config.adapter in {"pi", "harbor"}]
    if agent_configs and not allow_agents:
        raise PermissionError("pi and harbor configurations require --allow-agents acknowledgement")
    harbor_configs = [config for config in pack.configurations if config.adapter == "harbor"]
    if harbor_configs:
        if len(harbor_configs) != 1 or len(pack.configurations) != 1:
            raise ValueError("Harbor runs require exactly one configuration")
        from .harbor_backend import HarborBackend, preflight
        from .harbor_tasks import translate_harbor_task
        from .models import create_attempt_identity

        config = harbor_configs[0]
        with tempfile.TemporaryDirectory(prefix="mogil-harbor-translation-") as temporary:
            temporary_root = Path(temporary)
            prepared: list[tuple[Task, str, Any, Any]] = []
            for task in pack.tasks:
                result_id = logical_run_id(fingerprint, task, config)
                identity = create_attempt_identity(
                    result_id, attempt_id_factory=attempt_id_factory or uuid4
                )
                fixture = resolve_fixture(pack_path, task)
                verifier_template = fixture / "verify.py" if fixture.is_dir() else None
                if verifier_template is None or not verifier_template.is_file():
                    raise ValueError(
                        "Harbor tasks require a hidden verify.py beside the candidate fixture"
                    )
                canary = (
                    canary_factory or (lambda: "HIDDEN_VERIFIER_CANARY_" + secrets.token_hex(16))
                )()
                hidden_verifier = temporary_root / f"hidden-{task.id}-verify.py"
                hidden_verifier.write_text(
                    verifier_template.read_text(encoding="utf-8").replace("__CANARY__", canary),
                    encoding="utf-8",
                )
                translation = translate_harbor_task(
                    pack_path,
                    task,
                    config,
                    identity,
                    temporary_root / "translation",
                    hidden_verifier=hidden_verifier,
                    test_agent_import_path=_test_agent_import_path,
                )
                (harbor_preflight or preflight)(final_output_dir, translation)
                prepared.append((task, result_id, identity, translation))

            output_dir.mkdir(parents=True, exist_ok=False)
            (output_dir / "results").mkdir()
            backend = harbor_backend or HarborBackend()
            summaries: list[dict[str, Any]] = []
            for task, result_id, identity, translation in prepared:
                attempt = asyncio.run(
                    backend.run_attempt(
                        translation,
                        identity,
                        output_dir / "results",
                        _fixture_profile=(
                            _fixture_profile_token()
                            if _test_agent_import_path is not None
                            else None
                        ),
                    )
                )
                bundle_dir = attempt.bundle_dir
                run = attempt.run
                prompt = (
                    (translation.task_dir / "instruction.md")
                    .read_text(encoding="utf-8")
                    .rstrip("\n")
                )
                if _test_agent_import_path is None:
                    from .run_bundle import finalize_real_pi_evidence

                    termination_reason = (
                        "timed_out"
                        if run.get("agent_outcome") == "timed_out"
                        else ("completed" if run.get("agent_outcome") == "succeeded" else "failed")
                    )
                    evidence_status = finalize_real_pi_evidence(
                        bundle_dir,
                        task={
                            "id": task.id,
                            "revision": pack.revision,
                            "privacy_class": task.privacy_class.value,
                            "prompt": prompt,
                        },
                        analysis_metadata={
                            "provider": config.provider,
                            "model": config.model,
                            "harness": config.harness.model_dump(mode="json", exclude_none=True),
                        },
                        termination_reason=termination_reason,
                    )
                    run["evidence_status"] = evidence_status.value
                summaries.append(
                    {
                        "id": result_id,
                        "task_id": task.id,
                        "configuration_id": config.id,
                        "category": task.category,
                        "lane": task.lane.value,
                        "privacy_class": task.privacy_class.value,
                        "provider": config.provider,
                        "model": config.model,
                        "harness": config.harness.model_dump(exclude_none=True),
                        "prompt": prompt,
                        "status": run["infrastructure_outcome"],
                        "bundle": bundle_dir.relative_to(output_dir).as_posix(),
                    }
                )
            evidence_values: list[dict[str, Any]] = []
            for summary in summaries:
                evidence_path = output_dir / summary["bundle"] / "mogil.harbor-evidence.json"
                if evidence_path.is_file():
                    value = json.loads(evidence_path.read_text(encoding="utf-8"))
                    if not isinstance(value, dict):
                        raise ValueError("generated Harbor evidence must be a JSON object")
                    evidence_values.append(value)
            if evidence_values:
                (output_dir / "mogil.harbor-evidence.json").write_text(
                    json.dumps(evidence_values, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                (output_dir / "mogil.harbor-evidence.jsonl").write_text(
                    "".join(
                        json.dumps(value, sort_keys=True) + "\n" for value in evidence_values
                    ),
                    encoding="utf-8",
                )

            created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            manifest = {
                "schema_version": "1",
                "created_at": created_at,
                "pack": {
                    "id": pack.id,
                    "revision": pack.revision,
                    "fingerprint": fingerprint,
                },
                "result_count": len(summaries),
                "results": summaries,
            }
            (output_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            from .artifacts import export_run

            export_run(output_dir)
            return output_dir
    output_dir.mkdir(parents=True, exist_ok=False)
    raw_dir = output_dir / "results"
    raw_dir.mkdir()
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    results: list[RunResult] = []
    for task in pack.tasks:
        prompt = task_prompt(pack_path, task)
        for config in pack.configurations:
            result_id = logical_run_id(fingerprint, task, config)
            if config.adapter == "mock":
                execution = _mock(prompt, task)
            elif config.adapter == "command":
                execution = _command(pack_path, task)
            else:
                execution = host_pi(pack_path, task, config, prompt)
            result = RunResult(
                id=result_id,
                task_id=task.id,
                configuration_id=config.id,
                lane=task.lane.value,
                category=task.category,
                privacy_class=task.privacy_class.value,
                provider=config.provider,
                model=config.model,
                harness=config.harness.model_dump(exclude_none=True),
                prompt=prompt,
                execution=execution,
            )
            results.append(result)
            (raw_dir / f"{task.id}--{config.id}.json").write_text(
                json.dumps(asdict(result), indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
    manifest = {
        "schema_version": "1",
        "created_at": created_at,
        "pack": {"id": pack.id, "revision": pack.revision, "fingerprint": fingerprint},
        "result_count": len(results),
        "results": [
            {
                "id": item.id,
                "task_id": item.task_id,
                "configuration_id": item.configuration_id,
                "status": item.execution.status,
                "raw_artifact": f"results/{item.task_id}--{item.configuration_id}.json",
            }
            for item in results
        ],
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    from .artifacts import export_run

    export_run(output_dir)
    return output_dir


def _atomic_run(
    pack_path: Path,
    output_dir: Path,
    **kwargs: Any,
) -> Path:
    if output_dir.exists():
        raise FileExistsError(f"output path already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent)
    )
    staging = staging_root / "complete"
    try:
        _run_pack_to_directory(
            pack_path,
            staging,
            final_output_dir=output_dir,
            **kwargs,
        )
        staging.replace(output_dir)
        staging_root.rmdir()
        return output_dir
    except BaseException:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise


_SHIPPED_FIXTURE_FINGERPRINT = "856415e01003df1ae8461f2bff251fd040c25e2dd9d0c3079531a8ce02c10317"
_SHIPPED_FIXTURE_HASHES = {
    "agent.py": "e4310f9e2fb61b11040b5e2d3bbcf5cf4ad070f467a30887c1871d32e09e506c",
    "calculator.py": "405aaadb887d880ae2e13d576ebd4524dd12a001bf832d778a05d243779dafcc",
    "verify.py": "07f5cc0e302a62a5f99863034abb1c3fb03756daa27c82178522a86e684c0455",
}
_SHIPPED_FIXTURE_AGENT = "agent:DeterministicTestAgent"
_SHIPPED_FIXTURE_AGENT_VERSION = "test-only-1"


def _run_shipped_fixture_pack(
    pack_path: Path,
    output_dir: Path,
    *,
    canary_factory: Callable[[], str] | None = None,
) -> Path:
    """Run only the source-bound, credential-free repository smoke fixture."""
    from .packs import load_pack

    pack = load_pack(pack_path)
    fingerprint = pack_fingerprint(pack_path, pack)
    if fingerprint != _SHIPPED_FIXTURE_FINGERPRINT or len(pack.tasks) != 1:
        raise ValueError("pack is not the immutable shipped Harbor fixture profile")
    fixture = resolve_fixture(pack_path, pack.tasks[0])
    actual_hashes = {
        name: hashlib.sha256((fixture / name).read_bytes()).hexdigest()
        for name in _SHIPPED_FIXTURE_HASHES
    }
    agent_source = (fixture / "agent.py").read_text(encoding="utf-8")
    if (
        actual_hashes != _SHIPPED_FIXTURE_HASHES
        or f'return "{_SHIPPED_FIXTURE_AGENT_VERSION}"' not in agent_source
    ):
        raise ValueError("shipped Harbor fixture source hash or version mismatch")
    previous_bytecode_setting = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return _atomic_run(
            pack_path,
            output_dir,
            allow_agents=True,
            canary_factory=canary_factory,
            _test_agent_import_path=_SHIPPED_FIXTURE_AGENT,
        )
    finally:
        sys.dont_write_bytecode = previous_bytecode_setting


def run_pack(
    pack_path: Path,
    output_dir: Path,
    *,
    allow_commands: bool = False,
    allow_agents: bool = False,
    harbor_backend: _HarborBackend | None = None,
    harbor_preflight: Callable[[Path, HarborTranslation], None] | None = None,
    host_pi: Callable[[Path, Task, Configuration, str], Execution] = _pi,
    attempt_id_factory: Callable[[], object] | None = None,
    canary_factory: Callable[[], str] | None = None,
) -> Path:
    """Build a complete run privately and publish it with one atomic rename."""
    return _atomic_run(
        pack_path,
        output_dir,
        allow_commands=allow_commands,
        allow_agents=allow_agents,
        harbor_backend=harbor_backend,
        harbor_preflight=harbor_preflight,
        host_pi=host_pi,
        attempt_id_factory=attempt_id_factory,
        canary_factory=canary_factory,
    )
