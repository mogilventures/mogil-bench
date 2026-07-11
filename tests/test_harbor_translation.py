from __future__ import annotations

import json
import secrets
import tomllib
from pathlib import Path

from harbor.models.job.config import JobConfig as HarborJobConfig
from harbor.models.task.config import TaskConfig as HarborTaskConfig
from harbor.models.trial.config import AgentConfig as HarborAgentConfig
from harbor.models.trial.config import EnvironmentConfig as HarborEnvironmentConfig
from harbor.models.trial.config import TaskConfig as HarborTrialTaskConfig

from mogil_bench.harbor_backend import PI_VERSION
from mogil_bench.harbor_tasks import translate_harbor_task
from mogil_bench.models import Configuration, Harness, Task, create_attempt_identity

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/harbor-coding-task"


def inputs(canary: str, tmp_path: Path) -> tuple[Path, Task, Configuration, Path]:
    task = Task.model_validate(
        {
            "id": "calculator-fix",
            "category": "coding",
            "lane": "pi-coding",
            "prompt": "Correct add() so it returns the sum of its two arguments.",
            "fixture": "harbor-coding-task",
            "timeout_seconds": 37,
            "verifier": {
                "argv": [
                    "/usr/local/bin/python",
                    "/tests/hidden_verify.py",
                    "/workspace",
                ],
                "timeout_seconds": 11,
                "stdout_contains": "fixture passed",
            },
        }
    )
    config = Configuration(
        id="harbor-pi",
        provider="anthropic",
        model="claude-test",
        adapter="harbor",
        harness=Harness(name="harbor", version="0.18.0"),
    )
    verifier = FIXTURE / "verify.py"
    rendered = verifier.read_text(encoding="utf-8").replace("__CANARY__", canary)
    rendered_path = tmp_path / "hidden-verify.py"
    rendered_path.write_text(rendered, encoding="utf-8")
    return FIXTURE.parent / "fixture-pack.yaml", task, config, rendered_path


def test_translation_builds_harbor_1_3_task_and_pinned_job(tmp_path: Path) -> None:
    canary = "HIDDEN_VERIFIER_CANARY_" + secrets.token_hex(12)
    pack_path, task, config, rendered_verifier = inputs(canary, tmp_path)
    translation = translate_harbor_task(
        pack_path,
        task,
        config,
        create_attempt_identity("logical", attempt_id_factory=lambda: "attempt-1"),
        tmp_path,
        hidden_verifier=rendered_verifier,
    )

    assert translation.task_dir == tmp_path / "attempt-1"
    generated_files = {
        path.relative_to(translation.task_dir).as_posix()
        for path in translation.task_dir.rglob("*")
        if path.is_file()
    }
    assert generated_files >= {
        "instruction.md",
        "task.toml",
        "environment/Dockerfile",
        "environment/workspace/calculator.py",
        "environment/source-manifest.json",
        "tests/Dockerfile",
        "tests/baseline/calculator.py",
        "tests/capture_workspace.py",
        "tests/test.sh",
        "tests/verify.py",
        "tests/hidden_verify.py",
    }
    raw = tomllib.loads((translation.task_dir / "task.toml").read_text(encoding="utf-8"))
    parsed = HarborTaskConfig.model_validate(raw)
    assert parsed.schema_version == "1.3"
    assert parsed.agent.timeout_sec == 37
    assert parsed.verifier.timeout_sec == 11
    assert parsed.verifier.environment_mode.value == "separate"
    assert parsed.verifier.network_mode.value == "no-network"
    assert parsed.verifier.environment is not None
    assert parsed.verifier.environment.network_mode.value == "no-network"
    assert parsed.environment.cpus == 1
    assert parsed.environment.memory_mb == 2048
    assert parsed.environment.storage_mb == 4096
    assert parsed.verifier.collect == []
    environment_dockerfile = (translation.task_dir / "environment/Dockerfile").read_text()
    assert "/mogil/before-workspace" not in environment_dockerfile
    assert "capture_workspace.py" not in environment_dockerfile
    tests_dockerfile = (translation.task_dir / "tests/Dockerfile").read_text()
    assert "baseline/ /mogil/before-workspace/" in tests_dockerfile
    assert "capture_workspace.py /mogil/capture_workspace.py" in tests_dockerfile

    harbor_job = HarborJobConfig(
        **translation.job_config,
        tasks=[HarborTrialTaskConfig(path=translation.task_dir)],
        agents=[HarborAgentConfig(**translation.agent_config)],
        environment=HarborEnvironmentConfig(**translation.environment_config),
    )
    assert harbor_job.job_name == "mogil-attempt-1"
    assert harbor_job.n_attempts == 1
    assert harbor_job.n_concurrent_trials == 1
    assert harbor_job.retry.max_retries == 0
    assert harbor_job.environment.type.value == "docker"
    assert harbor_job.environment.delete is True
    assert harbor_job.environment.mounts == []
    assert len(harbor_job.tasks) == len(harbor_job.agents) == 1
    assert harbor_job.agents[0].name == "pi"
    assert harbor_job.agents[0].model_name == "anthropic/claude-test"
    assert harbor_job.agents[0].kwargs == {"version": PI_VERSION}


def test_hidden_verifier_canary_is_not_agent_visible(tmp_path: Path) -> None:
    canary = "HIDDEN_VERIFIER_CANARY_" + secrets.token_hex(16)
    pack_path, task, config, rendered_verifier = inputs(canary, tmp_path)
    translation = translate_harbor_task(
        pack_path,
        task,
        config,
        create_attempt_identity("logical", attempt_id_factory=lambda: "attempt-canary"),
        tmp_path,
        hidden_verifier=rendered_verifier,
    )

    agent_visible = [
        translation.task_dir / "instruction.md",
        *sorted((translation.task_dir / "environment").rglob("*")),
    ]
    for path in agent_visible:
        if path.is_file():
            assert canary.encode() not in path.read_bytes(), path
    assert canary not in json.dumps(translation.agent_config, sort_keys=True)
    assert canary.encode() in (translation.task_dir / "tests/hidden_verify.py").read_bytes()


def test_deterministic_fixture_agent_is_explicitly_test_only(tmp_path: Path) -> None:
    canary = "HIDDEN_VERIFIER_CANARY_fixture"
    pack_path, task, config, rendered_verifier = inputs(canary, tmp_path)
    translation = translate_harbor_task(
        pack_path,
        task,
        config,
        create_attempt_identity("logical", attempt_id_factory=lambda: "attempt-fixture"),
        tmp_path,
        hidden_verifier=rendered_verifier,
        test_agent_import_path="tests.fixtures.harbor-coding-task.agent:DeterministicTestAgent",
    )

    assert translation.agent_config["name"] is None
    assert translation.agent_config["import_path"].endswith(":DeterministicTestAgent")
    assert translation.agent_config["kwargs"] == {"test_only": True}
    assert translation.agent_config["name"] != "pi"
    assert PI_VERSION not in json.dumps(translation.agent_config)
