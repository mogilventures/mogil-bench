from __future__ import annotations

import json
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mogil_bench.artifacts import validate_artifact
from mogil_bench.models import EvidenceStatus
from mogil_bench.run_bundle import REQUIRED_BUNDLE_FILES, validate_checksums
from mogil_bench.runner import run_pack

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests/fixtures"


@pytest.mark.docker_smoke
def test_real_harbor_docker_fixture_is_complete_and_leak_free(tmp_path: Path) -> None:
    canary = "HIDDEN_VERIFIER_CANARY_" + secrets.token_hex(16)
    observed_projects: set[str] = set()
    stop_polling = threading.Event()

    def observe_compose_labels() -> None:
        while not stop_polling.is_set():
            current = subprocess.run(
                ["docker", "ps", "-a", "--format", '{{.Label "com.docker.compose.project"}}'],
                capture_output=True,
                check=True,
                text=True,
            )
            observed_projects.update(current.stdout.split())
            time.sleep(0.1)

    observer = threading.Thread(target=observe_compose_labels, daemon=True)
    observer.start()
    sys.path.insert(0, str(FIXTURES / "harbor-coding-task"))
    try:
        run_dir = run_pack(
            FIXTURES / "harbor-smoke-v1.yaml",
            tmp_path / "run",
            allow_agents=True,
            canary_factory=lambda: canary,
            test_agent_import_path="agent:DeterministicTestAgent",
        )
    finally:
        sys.path.remove(str(FIXTURES / "harbor-coding-task"))
        stop_polling.set()
        observer.join(timeout=5)

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    bundle = run_dir / manifest["results"][0]["bundle"]
    run = json.loads((bundle / "run.json").read_text(encoding="utf-8"))
    cleanup = json.loads((bundle / "cleanup.json").read_text(encoding="utf-8"))
    reward = json.loads((bundle / "verifier/reward.json").read_text(encoding="utf-8"))

    assert run["evidence_status"] == EvidenceStatus.FIXTURE_COMPLETE.value
    assert run["agent_outcome"] == "succeeded"
    assert run["verifier_outcome"] == "passed"
    assert run["infrastructure_outcome"] == "succeeded"
    assert reward == {"command_exit": 1.0, "reward": 1.0, "stdout_assertion": 1.0}
    assert cleanup["status"] == "confirmed"
    assert cleanup["remaining_container_ids"] == []
    assert validate_checksums(bundle)
    assert all((bundle / relative).is_file() for relative in REQUIRED_BUNDLE_FILES)

    patch = (bundle / "workspace/patch.diff").read_text(encoding="utf-8")
    assert "left - right" in patch and "left + right" in patch
    assert "calculator.py" in patch
    assert validate_artifact(run_dir / "blindbench.json") == 1
    assert validate_artifact(run_dir / "blindbench.jsonl") == 1

    agent_visible = [
        bundle / "agent/pi.txt",
        bundle / "workspace/before-manifest.json",
        bundle / "workspace/after-manifest.json",
        bundle / "workspace/patch.diff",
        bundle / "workspace/changed-files.json",
        run_dir / "blindbench.json",
        run_dir / "blindbench.jsonl",
    ]
    assert all(canary.encode() not in path.read_bytes() for path in agent_visible)

    assert len(cleanup["compose_project_labels"]) == 2
    assert set(cleanup["compose_project_labels"]).issubset(observed_projects)
    for project in cleanup["compose_project_labels"]:
        leaked = subprocess.run(
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
            check=True,
            text=True,
        )
        assert leaked.stdout.strip() == ""
