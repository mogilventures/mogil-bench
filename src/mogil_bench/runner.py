from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Configuration, Task
from .packs import canonical_hash, pack_fingerprint, resolve_fixture, task_prompt

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


def run_pack(
    pack_path: Path,
    output_dir: Path,
    *,
    allow_commands: bool = False,
    allow_agents: bool = False,
) -> Path:
    from .packs import load_pack

    pack = load_pack(pack_path)
    fingerprint = pack_fingerprint(pack_path, pack)
    if any(config.adapter == "command" for config in pack.configurations) and not allow_commands:
        raise PermissionError("command configurations require --allow-commands acknowledgement")
    if any(config.adapter == "pi" for config in pack.configurations) and not allow_agents:
        raise PermissionError("pi configurations require --allow-agents acknowledgement")
    output_dir.mkdir(parents=True, exist_ok=False)
    raw_dir = output_dir / "results"
    raw_dir.mkdir()
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    results: list[RunResult] = []
    for task in pack.tasks:
        prompt = task_prompt(pack_path, task)
        for config in pack.configurations:
            result_id = "mogil-" + canonical_hash(
                {
                    "pack_fingerprint": fingerprint,
                    "task": task.model_dump(mode="json"),
                    "configuration": config.model_dump(mode="json"),
                }
            )
            if config.adapter == "mock":
                execution = _mock(prompt, task)
            elif config.adapter == "command":
                execution = _command(pack_path, task)
            else:
                execution = _pi(pack_path, task, config, prompt)
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
