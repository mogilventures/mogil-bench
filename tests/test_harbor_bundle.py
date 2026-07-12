from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mogil_bench.artifacts import export_run, validate_artifact
from mogil_bench.harbor_tasks import write_verifier_wrapper
from mogil_bench.models import EvidenceStatus, Task
from mogil_bench.run_bundle import (
    BundleError,
    build_workspace_evidence,
    classify_evidence,
    collect_files,
    finalize_real_pi_evidence,
    read_reward,
    validate_checksums,
    write_checksums,
)


def test_workspace_evidence_tracks_modified_added_deleted_and_modes(tmp_path: Path) -> None:
    before = tmp_path / "before"
    after = tmp_path / "after"
    output = tmp_path / "evidence"
    before.mkdir()
    after.mkdir()
    (before / "modified.txt").write_text("old\n", encoding="utf-8")
    (after / "modified.txt").write_text("new\n", encoding="utf-8")
    (before / "deleted.txt").write_text("gone\n", encoding="utf-8")
    (after / "added.txt").write_text("added\n", encoding="utf-8")
    (before / "mode.sh").write_text("echo ok\n", encoding="utf-8")
    (after / "mode.sh").write_text("echo ok\n", encoding="utf-8")
    os.chmod(before / "mode.sh", 0o644)
    os.chmod(after / "mode.sh", 0o755)

    build_workspace_evidence(before, after, output)

    changed = json.loads((output / "changed-files.json").read_text(encoding="utf-8"))
    assert changed == [
        {"path": "added.txt", "status": "added"},
        {"path": "deleted.txt", "status": "deleted"},
        {"path": "mode.sh", "status": "mode_changed"},
        {"path": "modified.txt", "status": "modified"},
    ]
    before_manifest = json.loads((output / "before-manifest.json").read_text())
    after_manifest = json.loads((output / "after-manifest.json").read_text())
    before_mode = next(
        item for item in before_manifest["files"] if item["path"] == "mode.sh"
    )["mode"]
    after_mode = next(
        item for item in after_manifest["files"] if item["path"] == "mode.sh"
    )["mode"]
    assert before_mode == "0644"
    assert after_mode == "0755"
    patch = (output / "patch.diff").read_text(encoding="utf-8")
    assert "--- a/modified.txt" in patch and "+new" in patch
    assert "--- a/deleted.txt" in patch and "-gone" in patch
    assert "+++ b/added.txt" in patch and "+added" in patch
    assert "old mode 100644" in patch and "new mode 100755" in patch


def test_workspace_evidence_is_bounded_and_marks_binary_and_oversized_files(
    tmp_path: Path,
) -> None:
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    (before / "binary.bin").write_bytes(b"\x00old")
    (after / "binary.bin").write_bytes(b"\x00new")
    (after / "a-large.txt").write_bytes(b"x" * 20)
    (after / "extra.txt").write_text("extra", encoding="utf-8")

    output = tmp_path / "evidence"
    build_workspace_evidence(
        before,
        after,
        output,
        max_files=2,
        max_file_bytes=8,
        max_total_bytes=32,
        max_diff_bytes=8,
    )

    after_manifest = json.loads((output / "after-manifest.json").read_text())
    assert after_manifest["complete"] is False
    assert after_manifest["omissions"]
    patch = (output / "patch.diff").read_text(encoding="utf-8")
    assert "Binary files a/binary.bin and b/binary.bin differ" in patch
    assert "MOGIL EVIDENCE BOUNDED" in patch

    total_output = tmp_path / "total-evidence"
    build_workspace_evidence(
        before,
        after,
        total_output,
        max_files=10,
        max_file_bytes=100,
        max_total_bytes=5,
    )
    total_manifest = json.loads((total_output / "after-manifest.json").read_text())
    assert any(
        omission["reason"] == "total_byte_limit"
        for omission in total_manifest["omissions"]
    )


def test_safe_collection_rejects_unsafe_paths_links_special_files_and_sizes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "raw.bin").write_bytes(b"\x00raw\xff")
    bundle = tmp_path / "bundle"
    collect_files(source, bundle, {"agent/pi.txt": "raw.bin"})
    assert (bundle / "agent/pi.txt").read_bytes() == b"\x00raw\xff"

    for destination, source_name in (
        ("../escape", "raw.bin"),
        ("/absolute", "raw.bin"),
        ("copy", "../outside"),
        ("copy", "/etc/passwd"),
    ):
        with pytest.raises(BundleError, match="path"):
            collect_files(
                source,
                tmp_path / destination.replace("/", "_"),
                {destination: source_name},
            )

    (source / "link").symlink_to(source / "raw.bin")
    with pytest.raises(BundleError, match="symlink"):
        collect_files(source, tmp_path / "links", {"copy": "link"})
    fifo = source / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(BundleError, match="regular"):
        collect_files(source, tmp_path / "special", {"copy": "fifo"})
    with pytest.raises(BundleError, match="per-file"):
        collect_files(source, tmp_path / "large", {"copy": "raw.bin"}, max_file_bytes=2)
    with pytest.raises(BundleError, match="total"):
        collect_files(
            source,
            tmp_path / "total",
            {"one": "raw.bin", "two": "raw.bin"},
            max_total_bytes=7,
        )


def test_checksums_cover_retained_files_and_detect_corruption(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "nested").mkdir(parents=True)
    (bundle / "run.json").write_text("{}\n", encoding="utf-8")
    (bundle / "nested/raw.bin").write_bytes(b"raw")

    checksum_path = write_checksums(bundle)

    lines = checksum_path.read_text(encoding="utf-8").splitlines()
    assert [line.split("  ", 1)[1] for line in lines] == ["nested/raw.bin", "run.json"]
    assert validate_checksums(bundle) is True
    (bundle / "nested/raw.bin").write_bytes(b"changed")
    assert validate_checksums(bundle) is False


def test_verifier_streams_are_independently_bounded_and_rich(tmp_path: Path) -> None:
    task = Task.model_validate(
        {
            "id": "verify",
            "category": "coding",
            "lane": "pi-coding",
            "prompt": "x",
            "verifier": {
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys; print('A' * 40000); print('B' * 50000, file=sys.stderr)",
                ],
                "timeout_seconds": 2,
                "stdout_contains": "AAAA",
            },
        }
    )
    wrapper = tmp_path / "verify.py"
    logs = tmp_path / "logs"
    write_verifier_wrapper(task, wrapper, trusted_workspace_evidence=False)

    completed = subprocess.run(
        [sys.executable, str(wrapper)],
        env={**os.environ, "MOGIL_VERIFIER_LOGS": str(logs)},
        check=False,
        capture_output=True,
    )

    assert completed.returncode == 0
    verification = json.loads((logs / "verification.json").read_text())
    assert verification["timed_out"] is False
    assert verification["exit_code"] == verification["expected_exit_code"] == 0
    assert verification["stdout_assertion_passed"] is True
    assert verification["stdout_truncated"] is True
    assert verification["stderr_truncated"] is True
    assert len((logs / "stdout.txt").read_bytes()) == 32768
    assert len((logs / "stderr.txt").read_bytes()) == 32768
    assert read_reward(logs / "reward.json")["reward"] == 1.0


def test_verifier_matches_raw_stdout_but_redacts_canaries_from_retained_streams(
    tmp_path: Path,
) -> None:
    canary = "HIDDEN_VERIFIER_CANARY_deadbeef1234"
    task = Task.model_validate(
        {
            "id": "verify-redaction",
            "category": "coding",
            "lane": "pi-coding",
            "prompt": "x",
            "verifier": {
                "argv": [
                    sys.executable,
                    "-c",
                    (
                        "import sys; "
                        f"print('fixture passed {canary}'); "
                        f"print('diagnostic {canary}', file=sys.stderr)"
                    ),
                ],
                "timeout_seconds": 2,
                "stdout_contains": f"fixture passed {canary}",
            },
        }
    )
    wrapper = tmp_path / "verify.py"
    logs = tmp_path / "logs"
    write_verifier_wrapper(task, wrapper, trusted_workspace_evidence=False)

    completed = subprocess.run(
        [sys.executable, str(wrapper)],
        env={**os.environ, "MOGIL_VERIFIER_LOGS": str(logs)},
        check=False,
        capture_output=True,
    )

    assert completed.returncode == 0
    verification = json.loads((logs / "verification.json").read_text())
    assert verification["stdout_assertion_passed"] is True
    assert (logs / "stdout.txt").read_bytes() == b"fixture passed [REDACTED]\n"
    assert (logs / "stderr.txt").read_bytes() == b"diagnostic [REDACTED]\n"
    assert canary.encode() not in (logs / "stdout.txt").read_bytes()
    assert canary.encode() not in (logs / "stderr.txt").read_bytes()


def test_verifier_retains_ordinary_stdout_and_stderr_unchanged(tmp_path: Path) -> None:
    task = Task.model_validate(
        {
            "id": "verify-ordinary-streams",
            "category": "coding",
            "lane": "pi-coding",
            "prompt": "x",
            "verifier": {
                "argv": [
                    sys.executable,
                    "-c",
                    (
                        "import sys; print('fixture passed'); "
                        "print('note: 2 + 3 = 5', file=sys.stderr)"
                    ),
                ],
                "timeout_seconds": 2,
                "stdout_contains": "fixture passed",
            },
        }
    )
    wrapper = tmp_path / "verify.py"
    logs = tmp_path / "logs"
    write_verifier_wrapper(task, wrapper, trusted_workspace_evidence=False)

    completed = subprocess.run(
        [sys.executable, str(wrapper)],
        env={**os.environ, "MOGIL_VERIFIER_LOGS": str(logs)},
        check=False,
        capture_output=True,
    )

    assert completed.returncode == 0
    assert (logs / "stdout.txt").read_bytes() == b"fixture passed\n"
    assert (logs / "stderr.txt").read_bytes() == b"note: 2 + 3 = 5\n"


def test_verifier_timeout_is_recorded_and_malformed_reward_rejected(tmp_path: Path) -> None:
    task = Task.model_validate(
        {
            "id": "timeout",
            "category": "coding",
            "lane": "pi-coding",
            "prompt": "x",
            "verifier": {
                "argv": [sys.executable, "-c", "import time; time.sleep(2)"],
                "timeout_seconds": 0.05,
            },
        }
    )
    wrapper = tmp_path / "verify.py"
    logs = tmp_path / "logs"
    write_verifier_wrapper(task, wrapper, trusted_workspace_evidence=False)

    completed = subprocess.run(
        [sys.executable, str(wrapper)],
        env={**os.environ, "MOGIL_VERIFIER_LOGS": str(logs)},
        check=False,
    )

    assert completed.returncode == 1
    verification = json.loads((logs / "verification.json").read_text())
    assert verification["timed_out"] is True
    assert verification["exit_code"] is None
    assert verification["verifier_outcome"] == "timed_out"
    (logs / "reward.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(BundleError, match="reward"):
        read_reward(logs / "reward.json")


REQUIRED_BUNDLE_FILES = [
    "run.json",
    "environment.json",
    "cleanup.json",
    "harbor/job-config.json",
    "harbor/job-lock.json",
    "harbor/trial-config.json",
    "harbor/trial-lock.json",
    "harbor/trial-result.json",
    "harbor/trial.log",
    "agent/pi.txt",
    "workspace/before-manifest.json",
    "workspace/after-manifest.json",
    "workspace/patch.diff",
    "workspace/changed-files.json",
    "verifier/verification.json",
    "verifier/stdout.txt",
    "verifier/stderr.txt",
    "verifier/reward.json",
    "artifacts/harbor-manifest.json",
]


def complete_bundle(root: Path) -> Path:
    json_values: dict[str, object] = {
        "run.json": {
            "bundle_version": "1",
            "logical_run_id": "logical",
            "attempt_id": "attempt",
            "harbor_job_id": "job-id",
            "harbor_trial_id": "trial-id",
            "trial_name": "trial",
            "task_checksum": "sha256:task",
            "harbor_version": "0.18.0",
            "pi_version": "0.80.6",
            "agent_outcome": "succeeded",
            "verifier_outcome": "passed",
            "infrastructure_outcome": "succeeded",
        },
        "environment.json": {
            "schema_version": "1",
            "class": "isolated-sandbox",
            "provider": "docker",
            "mounts": [],
            "delete": True,
            "requested": {
                "provider": "docker",
                "image": "python@sha256:" + "a" * 64,
                "cpus": 1,
                "memory_mb": 2048,
                "storage_mb": 4096,
                "network_mode": "public",
                "allowed_hosts": [],
                "verifier_network_mode": "no-network",
                "secret_transport": "host_environment",
                "delete": True,
                "mounts": [],
            },
            "effective": {
                "provider": "docker",
                "image": "python@sha256:" + "a" * 64,
                "cpus": 1,
                "memory_mb": 2048,
                "storage_mb": 4096,
                "network_mode": "public",
                "allowed_hosts": [],
                "verifier_network_mode": "no-network",
                "secret_transport": "host_environment",
                "delete": True,
                "mounts": [],
            },
        },
        "cleanup.json": {
            "status": "confirmed",
            "resource_identifiers": ["trial__env"],
            "remaining_resource_ids": [],
            "project_identifiers": ["trial__env"],
            "compose_project_labels": ["trial__env"],
            "remaining_container_ids": [],
            "error": None,
        },
        "verifier/verification.json": {
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
        },
        "workspace/before-manifest.json": {"complete": True, "files": [], "omissions": []},
        "workspace/after-manifest.json": {"complete": True, "files": [], "omissions": []},
        "workspace/changed-files.json": [{"path": "calculator.py", "status": "modified"}],
        "verifier/reward.json": {
            "reward": 1.0,
            "command_exit": 1.0,
            "stdout_assertion": 1.0,
        },
    }
    for relative in REQUIRED_BUNDLE_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative in json_values:
            path.write_text(json.dumps(json_values[relative]) + "\n", encoding="utf-8")
        elif relative.endswith(".json"):
            path.write_text("{}\n", encoding="utf-8")
        else:
            path.write_bytes(b"retained raw bytes\n")
    write_checksums(root)
    return root


def test_fixture_complete_requires_every_valid_artifact_and_confirmed_cleanup(
    tmp_path: Path,
) -> None:
    complete = complete_bundle(tmp_path / "complete")
    assert (
        classify_evidence(complete, deterministic_fixture=True)
        == EvidenceStatus.FIXTURE_COMPLETE
    )
    assert classify_evidence(complete, deterministic_fixture=False) == EvidenceStatus.INSUFFICIENT
    assert classify_evidence(complete, harbor_execution=False) == EvidenceStatus.NON_QUALITY

    for index, relative in enumerate(REQUIRED_BUNDLE_FILES):
        candidate = complete_bundle(tmp_path / f"missing-{index}")
        (candidate / relative).unlink()
        assert (
            classify_evidence(candidate, deterministic_fixture=True)
            == EvidenceStatus.INSUFFICIENT
        )


def test_real_pi_is_quality_eligible_only_with_complete_canonical_evidence(
    tmp_path: Path,
) -> None:
    from test_pi_evidence import _real_pi_stream

    bundle = complete_bundle(tmp_path / "real")
    (bundle / "agent/pi.txt").write_bytes(_real_pi_stream())
    stale = b'{"stale":"pre-reviewer-hash"}\n'
    (bundle / "mogil.harbor-evidence.json").write_bytes(stale)
    (bundle / "mogil.harbor-evidence.jsonl").write_bytes(stale)
    write_checksums(bundle)
    assert validate_checksums(bundle)
    status = finalize_real_pi_evidence(
        bundle,
        task={"id": "task", "revision": "1", "privacy_class": "public", "prompt": "Fix it"},
        analysis_metadata={"provider": "private-provider", "model": "private-model"},
        termination_reason="completed",
    )
    assert status == EvidenceStatus.QUALITY_ELIGIBLE
    run = json.loads((bundle / "run.json").read_text())
    assert run["evidence_status"] == "quality_eligible"
    assert validate_checksums(bundle)
    private_bytes = (bundle / "mogil.harbor-evidence.json").read_bytes()
    jsonl_bytes = (bundle / "mogil.harbor-evidence.jsonl").read_bytes()
    assert private_bytes != jsonl_bytes
    assert json.loads(private_bytes) == json.loads(jsonl_bytes)
    assert private_bytes != stale and jsonl_bytes != stale
    expected = {
        relative: digest
        for digest, relative in (
            line.split("  ", 1)
            for line in (bundle / "checksums.sha256").read_text().splitlines()
        )
    }
    assert expected["mogil.harbor-evidence.json"] == hashlib.sha256(private_bytes).hexdigest()
    assert expected["mogil.harbor-evidence.jsonl"] == hashlib.sha256(jsonl_bytes).hexdigest()

    malformed = complete_bundle(tmp_path / "malformed-real")
    (malformed / "agent/pi.txt").write_bytes(b'{"type":"session"}')
    write_checksums(malformed)
    assert finalize_real_pi_evidence(
        malformed,
        task={"id": "task", "revision": "1", "privacy_class": "public", "prompt": "Fix it"},
        analysis_metadata={},
        termination_reason="failed",
    ) == EvidenceStatus.INSUFFICIENT


def test_reward_one_cannot_override_corruption_or_cleanup_failure(tmp_path: Path) -> None:
    corrupt = complete_bundle(tmp_path / "corrupt")
    (corrupt / "workspace/patch.diff").write_text("tampered", encoding="utf-8")
    assert classify_evidence(corrupt, deterministic_fixture=True) == EvidenceStatus.INSUFFICIENT

    cleanup_failed = complete_bundle(tmp_path / "cleanup-failed")
    (cleanup_failed / "cleanup.json").write_text('{"status":"failed"}\n', encoding="utf-8")
    write_checksums(cleanup_failed)
    assert (
        classify_evidence(cleanup_failed, deterministic_fixture=True)
        == EvidenceStatus.INSUFFICIENT
    )

    malformed = complete_bundle(tmp_path / "malformed")
    (malformed / "harbor/trial-result.json").write_text("not json", encoding="utf-8")
    write_checksums(malformed)
    assert classify_evidence(malformed, deterministic_fixture=True) == EvidenceStatus.INSUFFICIENT


def test_harbor_bundle_projects_to_reviewer_safe_blindbench_v1(tmp_path: Path) -> None:
    canary = "HIDDEN_VERIFIER_CANARY_export_must_not_leak"
    run_dir = tmp_path / "run"
    bundle = run_dir / "results/logical/attempt"
    bundle.mkdir(parents=True)
    (bundle / "run.json").write_text(
        json.dumps(
            {
                "bundle_version": "1",
                "logical_run_id": "logical",
                "attempt_id": "attempt",
                "harbor_version": "0.18.0",
                "pi_version": "0.80.6",
                "agent_log_format": "deterministic_test_agent_log",
                "evidence_status": "fixture_complete",
                "agent_outcome": "succeeded",
                "verifier_outcome": "passed",
                "infrastructure_outcome": "succeeded",
                "harbor_job_id": "hidden-host-id",
            }
        ),
        encoding="utf-8",
    )
    (bundle / "cleanup.json").write_text(
        json.dumps({"project_identifiers": ["hidden-project"]}), encoding="utf-8"
    )
    verifier = bundle / "verifier"
    verifier.mkdir()
    (verifier / "stdout.txt").write_text(canary, encoding="utf-8")
    manifest = {
        "schema_version": "1",
        "created_at": "2026-07-11T00:00:00Z",
        "pack": {"id": "pack", "revision": "1", "fingerprint": "fingerprint"},
        "result_count": 1,
        "results": [
            {
                "id": "logical",
                "task_id": "task",
                "configuration_id": "harbor",
                "category": "coding",
                "lane": "pi-coding",
                "privacy_class": "internal",
                "provider": "test",
                "model": "deterministic",
                "harness": {"name": "harbor", "version": "0.18.0"},
                "prompt": "Fix the calculator.",
                "bundle": "results/logical/attempt",
            }
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    json_path, jsonl_path = export_run(run_dir)

    assert validate_artifact(json_path) == validate_artifact(jsonl_path) == 1
    record = json.loads(json_path.read_text(encoding="utf-8"))["records"][0]
    assert record["version"] == "1"
    assert record["id"] == "logical"
    assert record["environment"] == "harbor/docker"
    assert record["metadata"] == {
        "attempt_id": "attempt",
        "bundle_reference": "results/logical/attempt",
        "category": "coding",
        "configuration_id": "harbor",
        "evidence_status": "fixture_complete",
        "agent_outcome": "succeeded",
        "verifier_outcome": "passed",
        "infrastructure_outcome": "succeeded",
        "pack_fingerprint": "fingerprint",
        "pack_id": "pack",
        "pack_revision": "1",
        "task_id": "task",
    }
    serialized = json_path.read_text() + jsonl_path.read_text()
    assert canary not in serialized
    assert "hidden-host-id" not in serialized
    assert "hidden-project" not in serialized
    assert str(tmp_path) not in serialized

    (bundle / "environment.json").write_text(
        json.dumps({"schema_version": "1", "provider": "daytona"}),
        encoding="utf-8",
    )
    export_run(run_dir)
    daytona_record = json.loads(json_path.read_text(encoding="utf-8"))["records"][0]
    assert daytona_record["environment"] == "harbor/isolated-sandbox"


def test_harbor_export_rejects_symlink_components_and_malformed_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "run.json").write_text("{}", encoding="utf-8")
    (run_dir / "results").mkdir(parents=True)
    (run_dir / "results/link").symlink_to(outside, target_is_directory=True)
    manifest = {
        "schema_version": "1",
        "created_at": "2026-07-11T00:00:00Z",
        "pack": {"id": "p", "revision": "1", "fingerprint": "f"},
        "result_count": 1,
        "results": [{"bundle": "results/link"}],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="symlink"):
        export_run(run_dir)

    (run_dir / "results/link").unlink()
    malformed = run_dir / "results/bundle"
    malformed.mkdir()
    (malformed / "run.json").write_text(
        '{"attempt_id":"attempt","unexpected":true}', encoding="utf-8"
    )
    manifest["results"][0]["bundle"] = "results/bundle"
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid Mogil Harbor run.json"):
        export_run(run_dir)
