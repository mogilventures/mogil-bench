from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .harbor_backend import PI_VERSION
from .models import AttemptIdentity, Configuration, Task
from .packs import resolve_fixture

PYTHON_BASE_IMAGE = (
    "python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf"
)
AGENT_CPUS = 1
AGENT_MEMORY_MB = 2048
AGENT_STORAGE_MB = 4096
BUILD_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class HarborTranslation:
    task_dir: Path
    job_config: dict[str, object]
    agent_config: dict[str, object]
    environment_config: dict[str, object]


def _copy_candidate_fixture(source: Path, destination: Path) -> list[dict[str, str]]:
    destination.mkdir(parents=True)
    files = (
        [source]
        if source.is_file()
        else sorted(path for path in source.rglob("*") if path.is_file())
    )
    manifest: list[dict[str, str]] = []
    for path in files:
        relative = Path(path.name) if source.is_file() else path.relative_to(source)
        if relative.as_posix() in {"agent.py", "verify.py"} or relative.name.startswith(
            ".rendered-verify"
        ):
            continue
        if path.is_symlink():
            raise ValueError(f"fixture symlinks are not allowed: {relative.as_posix()}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        target.write_bytes(data)
        manifest.append(
            {"path": relative.as_posix(), "sha256": hashlib.sha256(data).hexdigest()}
        )
    return manifest


def _verifier_wrapper(task: Task, *, trusted_workspace_evidence: bool = True) -> str:
    if task.verifier is None:
        raise ValueError("Harbor coding tasks require a verifier")
    verifier = task.verifier
    return f'''from __future__ import annotations
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

MAX_STREAM_BYTES = 32768
logs = Path(os.environ.get("MOGIL_VERIFIER_LOGS", "/logs/verifier"))
logs.mkdir(parents=True, exist_ok=True)
started_at = datetime.now(timezone.utc)
started = time.monotonic()
timed_out = False
infrastructure_error = None
try:
    completed = subprocess.run(
        {verifier.argv!r}, capture_output=True, check=False, shell=False,
        timeout={verifier.timeout_seconds!r}
    )
    exit_code = completed.returncode
    stdout_bytes = completed.stdout
    stderr_bytes = completed.stderr
except subprocess.TimeoutExpired as error:
    timed_out = True
    exit_code = None
    stdout_bytes = error.stdout or b""
    stderr_bytes = error.stderr or b""
except OSError as error:
    infrastructure_error = f"{{type(error).__name__}}: {{error}}"
    exit_code = None
    stdout_bytes = b""
    stderr_bytes = b""
stdout_truncated = len(stdout_bytes) > MAX_STREAM_BYTES
stderr_truncated = len(stderr_bytes) > MAX_STREAM_BYTES
stdout_bytes = stdout_bytes[:MAX_STREAM_BYTES]
stderr_bytes = stderr_bytes[:MAX_STREAM_BYTES]
stdout = stdout_bytes.decode("utf-8", errors="replace")
expected_stdout = {verifier.stdout_contains!r}
exit_ok = (
    infrastructure_error is None
    and not timed_out
    and exit_code == {verifier.expected_exit_code}
)
stdout_ok = expected_stdout is None or expected_stdout in stdout
passed = exit_ok and stdout_ok
try:
    if {trusted_workspace_evidence!r}:
        trusted_evidence = logs / "workspace"
        shutil.rmtree(trusted_evidence, ignore_errors=True)
        evidence = subprocess.run(
            ["/usr/local/bin/python", "/mogil/capture_workspace.py",
             "/mogil/before-workspace", "/workspace", str(trusted_evidence)],
            capture_output=True,
            check=False,
            timeout={task.verifier.timeout_seconds!r},
            shell=False,
        )
    else:
        evidence = subprocess.CompletedProcess([], 0)
    if evidence.returncode != 0:
        infrastructure_error = "trusted workspace evidence generation failed"
except (OSError, subprocess.TimeoutExpired):
    infrastructure_error = "trusted workspace evidence generation failed"
passed = passed and infrastructure_error is None
ended_at = datetime.now(timezone.utc)
(logs / "stdout.txt").write_bytes(stdout_bytes)
(logs / "stderr.txt").write_bytes(stderr_bytes)
(logs / "verification.json").write_text(json.dumps({{
    "started_at": started_at.isoformat(),
    "ended_at": ended_at.isoformat(),
    "duration_ms": round((time.monotonic() - started) * 1000, 3),
    "timed_out": timed_out,
    "exit_code": exit_code,
    "expected_exit_code": {verifier.expected_exit_code},
    "command_exit_passed": exit_ok,
    "stdout_assertion_passed": stdout_ok,
    "stdout_truncated": stdout_truncated,
    "stderr_truncated": stderr_truncated,
    "verifier_outcome": "timed_out" if timed_out else ("passed" if passed else "failed"),
    "infrastructure_outcome": "failed" if infrastructure_error else "succeeded",
    "infrastructure_error": infrastructure_error,
}}, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
(logs / "reward.json").write_text(json.dumps({{
    "reward": float(passed),
    "command_exit": float(exit_ok),
    "stdout_assertion": float(stdout_ok),
}}, sort_keys=True) + "\\n", encoding="utf-8")
raise SystemExit(0 if passed else 1)
'''


def write_verifier_wrapper(
    task: Task, path: Path, *, trusted_workspace_evidence: bool = True
) -> None:
    path.write_text(
        _verifier_wrapper(task, trusted_workspace_evidence=trusted_workspace_evidence),
        encoding="utf-8",
    )


def _task_toml(task: Task, *, agent_network: str) -> str:
    if task.verifier is None:
        raise ValueError("Harbor coding tasks require a verifier")
    return f'''schema_version = "1.3"
artifacts = [{{ source = "/workspace", destination = "candidate-workspace" }}]

[agent]
timeout_sec = {task.timeout_seconds!r}
network_mode = "{agent_network}"

[environment]
build_timeout_sec = {BUILD_TIMEOUT_SECONDS}.0
cpus = {AGENT_CPUS}
memory_mb = {AGENT_MEMORY_MB}
storage_mb = {AGENT_STORAGE_MB}
network_mode = "{agent_network}"
workdir = "/workspace"

[verifier]
timeout_sec = {task.verifier.timeout_seconds!r}
environment_mode = "separate"
network_mode = "no-network"

[verifier.environment]
build_timeout_sec = {BUILD_TIMEOUT_SECONDS}.0
cpus = {AGENT_CPUS}
memory_mb = {AGENT_MEMORY_MB}
storage_mb = {AGENT_STORAGE_MB}
network_mode = "no-network"
workdir = "/workspace"
'''


def _workspace_capture_script() -> str:
    source = (Path(__file__).with_name("run_bundle.py")).read_text(encoding="utf-8")
    source = source.replace("from .models import EvidenceStatus\n", "")
    return source + '''

if __name__ == "__main__":
    import sys
    build_workspace_evidence(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
'''


def translate_harbor_task(
    pack_path: Path,
    task: Task,
    config: Configuration,
    identity: AttemptIdentity,
    translation_root: Path,
    *,
    hidden_verifier: Path,
    test_agent_import_path: str | None = None,
) -> HarborTranslation:
    """Translate one validated Mogil coding attempt into a Harbor 0.18 task tree."""
    if config.adapter != "harbor" or task.lane.value != "pi-coding":
        raise ValueError("Harbor translation requires adapter=harbor and lane=pi-coding")
    if config.mounts or config.backend is None or config.environment_type is None:
        raise ValueError("Harbor translation requires the validated no-mount Docker configuration")
    if not task.fixture:
        raise ValueError("Harbor coding tasks require a candidate fixture")

    task_dir = translation_root / identity.attempt_id
    task_dir.mkdir(parents=True, exist_ok=False)
    environment_dir = task_dir / "environment"
    tests_dir = task_dir / "tests"
    workspace_dir = environment_dir / "workspace"
    tests_dir.mkdir(parents=True)

    source = resolve_fixture(pack_path, task)
    manifest = _copy_candidate_fixture(source, workspace_dir)
    names = ", ".join(item["path"] for item in manifest)
    public_context = f"\n\nCandidate workspace files: {names}." if names else ""
    (task_dir / "instruction.md").write_text(
        (task.prompt or "Work on the provided candidate workspace.") + public_context + "\n",
        encoding="utf-8",
    )
    (environment_dir / "source-manifest.json").write_text(
        json.dumps({"files": manifest}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (environment_dir / "Dockerfile").write_text(
        f"FROM {PYTHON_BASE_IMAGE}\nCOPY workspace/ /workspace/\nWORKDIR /workspace\n",
        encoding="utf-8",
    )
    baseline_dir = tests_dir / "baseline"
    _copy_candidate_fixture(source, baseline_dir)
    (tests_dir / "capture_workspace.py").write_text(
        _workspace_capture_script(), encoding="utf-8"
    )
    (tests_dir / "Dockerfile").write_text(
        f"FROM {PYTHON_BASE_IMAGE}\n"
        "COPY . /tests/\n"
        "COPY baseline/ /mogil/before-workspace/\n"
        "COPY capture_workspace.py /mogil/capture_workspace.py\n"
        "WORKDIR /artifacts/candidate-workspace\n",
        encoding="utf-8",
    )
    write_verifier_wrapper(task, tests_dir / "verify.py")
    shutil.copyfile(hidden_verifier, tests_dir / "hidden_verify.py", follow_symlinks=False)
    test_script = tests_dir / "test.sh"
    test_script.write_text(
        "#!/bin/sh\nexec /usr/local/bin/python /tests/verify.py\n", encoding="utf-8"
    )
    test_script.chmod(0o755)

    test_agent = test_agent_import_path is not None
    agent_config: dict[str, object] = {
        "name": None if test_agent else "pi",
        "model_name": f"{config.provider}/{config.model}",
        "n_concurrent": 1,
        "override_timeout_sec": task.timeout_seconds,
        "kwargs": {"test_only": True} if test_agent else {"version": PI_VERSION},
    }
    if test_agent_import_path is not None:
        agent_config["import_path"] = test_agent_import_path
    agent_network = "no-network" if test_agent else "public"
    (task_dir / "task.toml").write_text(
        _task_toml(task, agent_network=agent_network), encoding="utf-8"
    )
    return HarborTranslation(
        task_dir=task_dir,
        job_config={
            "job_name": f"mogil-{identity.attempt_id}",
            "n_attempts": 1,
            "n_concurrent_trials": 1,
            "timeout_multiplier": 1.0,
            "agent_timeout_multiplier": 1.0,
            "verifier_timeout_multiplier": 1.0,
            "environment_build_timeout_multiplier": 1.0,
            "retry": {"max_retries": 0},
        },
        agent_config=agent_config,
        environment_config={
            "type": "docker",
            "delete": True,
            "mounts": [],
            "override_cpus": AGENT_CPUS,
            "override_memory_mb": AGENT_MEMORY_MB,
            "override_storage_mb": AGENT_STORAGE_MB,
        },
    )
