from __future__ import annotations

import csv
import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mogil_bench.cli import app
from mogil_bench.comparisons import ComparisonExportError, export_paired_comparison

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/completed-parity-run"
EXPECTED = ROOT / "tests/fixtures/completed-parity-comparison.csv"
CLI = CliRunner()
EXPECTED_HEADERS = [
    "case_id",
    "context",
    "candidate_a",
    "candidate_b",
    "candidate_a_model",
    "candidate_b_model",
    "candidate_a_harness",
    "candidate_b_harness",
    "product",
    "environment",
    "privacy_class",
    "segment",
]


def _copy_fixture(tmp_path: Path) -> Path:
    destination = tmp_path / "run"
    shutil.copytree(FIXTURE, destination)
    return destination


def _export_cli(run_dir: Path, destination: Path) -> object:
    return CLI.invoke(
        app,
        [
            "export",
            "paired-comparison",
            str(run_dir),
            "--candidate-a",
            "anthropic-direct",
            "--candidate-b",
            "openrouter-routed",
            "--output",
            str(destination),
        ],
    )


def test_completed_three_by_two_by_three_run_exports_nine_deterministic_rows(
    tmp_path: Path,
) -> None:
    run_dir = _copy_fixture(tmp_path)
    destination = tmp_path / "comparison.csv"

    first = _export_cli(run_dir, destination)

    assert first.exit_code == 0, first.stdout
    expected = EXPECTED.read_bytes()
    assert destination.read_bytes() == expected
    with destination.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
    assert reader.fieldnames == EXPECTED_HEADERS
    assert len(rows) == 9
    assert [row["case_id"] for row in rows] == [
        f"{task_id}--attempt-{attempt:02d}"
        for task_id in (
            "correct-fictional-calculator",
            "normalize-widget-slugs",
            "summarize-fictional-inventory",
        )
        for attempt in (1, 2, 3)
    ]
    assert {row["candidate_a_model"] for row in rows} == {
        "anthropic/claude-sonnet-4-6"
    }
    assert {row["candidate_b_model"] for row in rows} == {
        "openrouter/anthropic/claude-sonnet-4.6"
    }
    assert {row["candidate_a_harness"] for row in rows} == {
        "harbor-pi@0.18.0 (pi-0.80.6)"
    }
    assert {row["candidate_b_harness"] for row in rows} == {
        "harbor-pi@0.18.0 (pi-0.80.6)"
    }
    reviewer_fields = "\n".join(
        value
        for row in rows
        for key, value in row.items()
        if key in {"case_id", "context", "candidate_a", "candidate_b", "segment"}
    ).lower()
    assert "anthropic" not in reviewer_fields
    assert "openrouter" not in reviewer_fields
    assert "claude" not in reviewer_fields
    assert "credential" not in reviewer_fields
    assert "hidden_verifier_canary" not in reviewer_fields
    assert "/root/" not in reviewer_fields

    destination.write_bytes(b"different prior bytes")
    second = _export_cli(run_dir, destination)
    assert second.exit_code == 0, second.stdout
    assert destination.read_bytes() == expected


@pytest.mark.parametrize(
    "malformation",
    ["missing", "duplicate", "non_quality", "mismatched", "revision_mismatch"],
)
def test_malformed_comparison_matrix_fails_without_replacing_destination(
    tmp_path: Path, malformation: str
) -> None:
    run_dir = _copy_fixture(tmp_path)
    manifest_path = run_dir / "manifest.json"
    evidence_path = run_dir / "mogil.harbor-evidence.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    if malformation == "missing":
        manifest["results"].pop()
        manifest["result_count"] -= 1
    elif malformation == "duplicate":
        manifest["results"][-1] = dict(manifest["results"][0])
    elif malformation == "non_quality":
        evidence[0]["run"]["status"] = "insufficient"
    elif malformation == "mismatched":
        evidence[0]["reviewer"]["task"]["id"] = "wrong-task"
    else:
        evidence[0]["reviewer"]["task"]["revision"] = "2"

    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    destination = tmp_path / "comparison.csv"
    destination.write_bytes(b"keep-this")

    result = _export_cli(run_dir, destination)

    assert result.exit_code == 1
    assert destination.read_bytes() == b"keep-this"


def test_provider_qualified_model_is_not_double_prefixed(tmp_path: Path) -> None:
    run_dir = _copy_fixture(tmp_path)
    manifest_path = run_dir / "manifest.json"
    evidence_path = run_dir / "mogil.harbor-evidence.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    for result in manifest["results"]:
        if result["configuration_id"] == "anthropic-direct":
            result["model"] = "anthropic/claude-sonnet-4-6"
    for artifact in evidence:
        if artifact["analysis_metadata"]["provider"] == "anthropic":
            artifact["analysis_metadata"]["model"] = "anthropic/claude-sonnet-4-6"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    destination = tmp_path / "comparison.csv"

    result = _export_cli(run_dir, destination)

    assert result.exit_code == 0, result.stdout
    with destination.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert {row["candidate_a_model"] for row in rows} == {
        "anthropic/claude-sonnet-4-6"
    }
    assert "anthropic/anthropic/" not in destination.read_text(encoding="utf-8")


def test_comparison_publication_restores_destination_on_observed_replace_failure(
    tmp_path: Path,
) -> None:
    run_dir = _copy_fixture(tmp_path)
    destination = tmp_path / "comparison.csv"
    destination.write_bytes(b"old-bytes")
    calls = 0

    def replace_then_fail(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        os.replace(source, target)
        if calls == 1:
            raise OSError("injected publication failure")

    with pytest.raises(ComparisonExportError, match="restored"):
        export_paired_comparison(
            run_dir,
            candidate_a="anthropic-direct",
            candidate_b="openrouter-routed",
            output_path=destination,
            replace_file=replace_then_fail,
        )

    assert destination.read_bytes() == b"old-bytes"
    assert not list(tmp_path.glob(".comparison.csv.*.tmp"))
