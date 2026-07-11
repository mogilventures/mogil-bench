from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path

import pytest

from mogil_bench.models import EvidenceStatus
from mogil_bench.run_bundle import validate_checksums
from mogil_bench.runner import run_pack

ROOT = Path(__file__).parents[1]


@pytest.mark.daytona_smoke
def test_live_harbor_daytona_fixture_is_complete_and_deleted(tmp_path: Path) -> None:
    if os.environ.get("MOGIL_RUN_DAYTONA_SMOKE") != "1":
        pytest.skip("set MOGIL_RUN_DAYTONA_SMOKE=1 to authorize the live Daytona smoke")
    has_credentials = bool(os.environ.get("DAYTONA_API_KEY")) or bool(
        os.environ.get("DAYTONA_JWT_TOKEN") and os.environ.get("DAYTONA_ORGANIZATION_ID")
    )
    if not has_credentials:
        pytest.fail(
            "Daytona credentials unavailable: set DAYTONA_API_KEY or both "
            "DAYTONA_JWT_TOKEN and DAYTONA_ORGANIZATION_ID",
            pytrace=False,
        )
    image = os.environ.get("TERMINAL_DAYTONA_IMAGE", "")
    if "@sha256:" not in image:
        pytest.fail(
            "TERMINAL_DAYTONA_IMAGE must be an immutable image digest with Pi 0.80.6",
            pytrace=False,
        )
    secret_ref = os.environ.get("MOGIL_DAYTONA_SECRET_REF", "")
    if not secret_ref:
        pytest.fail(
            "MOGIL_DAYTONA_SECRET_REF must name an existing host-restricted "
            "Daytona organization secret",
            pytrace=False,
        )

    fixture = tmp_path / "fictional-calculator"
    shutil.copytree(ROOT / "tests/fixtures/harbor-coding-task", fixture)
    pack = tmp_path / "daytona-live.yaml"
    pack.write_text(
        f"""version: '1'
id: daytona-fictional-live
revision: '1'
name: Fictional Daytona lifecycle smoke
allow_agents: true
tasks:
  - id: calculator
    category: coding
    lane: pi-coding
    privacy_class: public
    prompt: Correct add() so it returns the sum of its two arguments.
    fixture: fictional-calculator
    timeout_seconds: 180
    verifier:
      argv: [python, /tests/hidden_verify.py, /workspace]
      timeout_seconds: 30
      stdout_contains: fixture passed
configurations:
  - id: harbor-daytona
    provider: anthropic
    model: claude-sonnet-4-6
    adapter: harbor
    backend: harbor
    environment_type: daytona
    environment_policy:
      image: {image!r}
      cpus: 2
      memory_mb: 4096
      storage_mb: 8192
      network_mode: allowlist
      allowed_hosts: [api.anthropic.com]
      secret_refs:
        ANTHROPIC_API_KEY: {"ref:" + secret_ref!r}
      max_lifetime_minutes: 60
    harness: {{name: harbor, version: '0.18.0'}}
""",
        encoding="utf-8",
    )

    canary = "HIDDEN_VERIFIER_CANARY_" + secrets.token_hex(16)
    run_dir = run_pack(
        pack,
        tmp_path / "run",
        allow_agents=True,
        canary_factory=lambda: canary,
    )
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    bundle = run_dir / manifest["results"][0]["bundle"]
    run = json.loads((bundle / "run.json").read_text(encoding="utf-8"))
    cleanup = json.loads((bundle / "cleanup.json").read_text(encoding="utf-8"))

    assert run["evidence_status"] == EvidenceStatus.QUALITY_ELIGIBLE.value
    assert run["infrastructure_outcome"] == "succeeded"
    assert cleanup["status"] == "confirmed"
    assert cleanup["remaining_resource_ids"] == []
    assert len(cleanup["deletion_receipts"]) == 2
    assert validate_checksums(bundle)

    credential_values = [
        value.encode()
        for name in (
            "DAYTONA_API_KEY",
            "DAYTONA_JWT_TOKEN",
            "DAYTONA_ORGANIZATION_ID",
        )
        if (value := os.environ.get(name))
    ]
    credential_pattern = re.compile(
        rb"(?i)(?:bearer\s+[a-z0-9._-]{12,}|sk-[a-z0-9_-]{16,}|"
        rb"eyJ[a-z0-9_-]{12,}\.eyJ[a-z0-9_-]{12,}\.[a-z0-9_-]{12,}|"
        rb"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY)"
    )
    retained = [path for path in run_dir.rglob("*") if path.is_file()]
    assert retained
    for path in retained:
        data = path.read_bytes()
        assert canary.encode() not in data, path
        assert credential_pattern.search(data) is None, path
        assert all(value not in data for value in credential_values), path
