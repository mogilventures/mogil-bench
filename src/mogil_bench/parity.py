from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .evidence import EvidenceError, HarborEvidence, validate_evidence_artifact
from .models import EnvironmentType, Pack
from .packs import load_pack, pack_fingerprint

PARITY_PACK_PATH = Path(__file__).parents[2] / "packs/daytona-provider-parity-v1.yaml"
PARITY_IMAGE = (
    "ghcr.io/mogilventures/mogil-bench-daytona-runtime@sha256:"
    "7728671c38220e066d23f63fd2544cc0722874ec40e1c86c883c8cc4d6c35dfe"
)
PARITY_PACK_FINGERPRINT = "9d2250f71f92b5f2500beff1a067ed96a914c1ada8e5d9573c0c209ab7ce5f29"
PARITY_SECRETS = {
    "mogil-anthropic-smoke": ("api.anthropic.com",),
    "mogil-openrouter-parity": ("openrouter.ai",),
}
_TASKS = {
    "correct-fictional-calculator": "fixtures/pi-activation/calculator",
    "normalize-widget-slugs": "fixtures/pi-activation/slugify",
    "summarize-fictional-inventory": "fixtures/pi-activation/inventory",
}
_CONFIGURATIONS = {
    "anthropic-direct": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "host": "api.anthropic.com",
        "secret_env": "ANTHROPIC_API_KEY",
        "secret_ref": "ref:mogil-anthropic-smoke",
    },
    "openrouter-routed": {
        "provider": "openrouter",
        "model": "anthropic/claude-sonnet-4.6",
        "host": "openrouter.ai",
        "secret_env": "OPENROUTER_API_KEY",
        "secret_ref": "ref:mogil-openrouter-parity",
    },
}


def validate_parity_pack(pack: Pack) -> None:
    """Fail closed if the shipped live matrix drifts from its reviewed boundary."""
    if (
        pack.id != "daytona-provider-parity-fictional-v1"
        or pack.revision != "1"
        or pack.allow_agents is not True
        or pack.allow_commands is not False
        or len(pack.tasks) != 3
        or len(pack.configurations) != 2
    ):
        raise ValueError("Daytona parity pack identity or matrix size is unreviewed")

    if [task.id for task in pack.tasks] != list(_TASKS):
        raise ValueError("Daytona parity task contract is unreviewed")
    for task in pack.tasks:
        verifier = task.verifier
        fixture = _TASKS[task.id]
        if (
            task.category != "coding"
            or task.lane.value != "pi-coding"
            or str(task.privacy_class) != "public"
            or task.fixture != fixture
            or not task.prompt
            or task.command is not None
            or task.timeout_seconds != 180
            or verifier is None
            or verifier.argv != ["python", "/tests/hidden_verify.py", "/workspace"]
            or verifier.timeout_seconds != 30
            or verifier.expected_exit_code != 0
            or verifier.stdout_contains != "passed"
        ):
            raise ValueError(f"Daytona parity task contract is unreviewed: {task.id}")
        fixture_root = PARITY_PACK_PATH.parent / fixture
        if not (fixture_root / "verify.py").is_file():
            raise ValueError(f"Daytona parity hidden verifier is absent: {task.id}")

    if [config.id for config in pack.configurations] != list(_CONFIGURATIONS):
        raise ValueError("Daytona parity configuration contract is unreviewed")
    for config in pack.configurations:
        expected = _CONFIGURATIONS[config.id]
        policy = config.environment_policy
        if (
            config.provider != expected["provider"]
            or config.model != expected["model"]
            or config.adapter != "harbor"
            or config.backend is None
            or config.backend.value != "harbor"
            or config.environment_type != EnvironmentType.DAYTONA
            or config.mounts != []
            or config.harness.model_dump(exclude_none=True)
            != {"name": "harbor-pi", "version": "0.18.0", "sdk": "pi-0.80.6"}
            or policy is None
            or policy.image != PARITY_IMAGE
            or policy.cpus != 2
            or policy.memory_mb != 4096
            or policy.storage_mb != 8192
            or policy.network_mode.value != "allowlist"
            or policy.allowed_hosts != [expected["host"]]
            or policy.secret_refs
            != {str(expected["secret_env"]): str(expected["secret_ref"])}
            or policy.max_lifetime_minutes != 60
        ):
            raise ValueError(
                f"Daytona parity configuration contract is unreviewed: {config.id}"
            )
    if pack_fingerprint(PARITY_PACK_PATH, pack) != PARITY_PACK_FINGERPRINT:
        raise ValueError("Daytona parity pack or fixture source fingerprint is unreviewed")


def validate_secret_inventory(inventory: dict[str, tuple[str, ...]]) -> None:
    """Validate metadata only; this boundary never accepts or returns secret values."""
    for name, expected_hosts in PARITY_SECRETS.items():
        hosts = inventory.get(name)
        if hosts is None:
            raise ValueError(f"required Daytona organization secret is absent: {name}")
        if set(hosts) != set(expected_hosts) or len(hosts) != len(expected_hosts):
            raise ValueError(
                f"Daytona organization secret {name} must be restricted exactly to "
                + ",".join(expected_hosts)
            )


def daytona_secret_inventory() -> dict[str, tuple[str, ...]]:
    """Read secret metadata only; Daytona never returns plaintext secret values."""
    try:
        from daytona import Daytona

        client = Daytona()
        inventory: dict[str, tuple[str, ...]] = {}
        cursor: str | None = None
        while True:
            page = client.secret.list(cursor=cursor, limit=100)
            for secret in page.items:
                if secret.name in inventory:
                    raise RuntimeError("Daytona returned duplicate organization secret names")
                inventory[secret.name] = tuple(secret.hosts or ())
            cursor = page.next_cursor
            if not cursor:
                return inventory
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"cannot validate Daytona organization secret inventory: {type(error).__name__}"
        ) from error


def _contains_provenance(value: Any) -> bool:
    if isinstance(value, dict):
        if any(str(key).lower() in {"provider", "model"} for key in value):
            return True
        return any(_contains_provenance(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_provenance(item) for item in value)
    if isinstance(value, str):
        lowered = value.lower()
        return any(
            marker in lowered
            for marker in (
                "anthropic",
                "openrouter",
                "claude-sonnet-4-6",
                "claude-sonnet-4.6",
            )
        )
    return False


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid parity output artifact: {path.name}") from error


def validate_parity_output(
    run_dir: Path,
    *,
    credential_values: Iterable[str] = (),
) -> None:
    """Require a complete, quality-eligible, blinded 18-attempt output."""
    manifest_path = run_dir / "manifest.json"
    json_path = run_dir / "mogil.harbor-evidence.json"
    jsonl_path = run_dir / "mogil.harbor-evidence.jsonl"
    if any(not path.is_file() for path in (manifest_path, json_path, jsonl_path)):
        raise ValueError("complete parity output is absent")
    manifest = _read_json(manifest_path)
    values = _read_json(json_path)
    if isinstance(values, list) and any(
        isinstance(value, dict) and _contains_provenance(value.get("reviewer"))
        for value in values
    ):
        raise ValueError("reviewer projection contains provenance")
    try:
        if validate_evidence_artifact(json_path) != 18:
            raise ValueError("parity JSON evidence must contain exactly 18 attempts")
        if validate_evidence_artifact(jsonl_path) != 18:
            raise ValueError("parity JSONL evidence must contain exactly 18 attempts")
    except EvidenceError as error:
        raise ValueError(f"parity evidence validation failed: {error}") from error

    if not isinstance(manifest, dict) or not isinstance(values, list):
        raise ValueError("parity output has an invalid top-level shape")
    try:
        jsonl_values = [
            json.loads(line)
            for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid parity JSONL evidence") from error
    if values != jsonl_values:
        raise ValueError("parity JSON and JSONL evidence do not match")
    results = manifest.get("results")
    if manifest.get("result_count") != 18 or not isinstance(results, list) or len(results) != 18:
        raise ValueError("parity manifest must contain exactly 18 attempts")

    expected_cells = {(config, task) for config in _CONFIGURATIONS for task in _TASKS}
    cells: dict[tuple[str, str], set[int]] = {}
    logical_ids: dict[tuple[str, str], set[str]] = {}
    manifest_attempts: dict[tuple[str, str], str] = {}
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("parity manifest result is invalid")
        cell = (str(result.get("configuration_id")), str(result.get("task_id")))
        number = result.get("attempt_number")
        logical = result.get("logical_run_id")
        attempt = result.get("attempt_id")
        if (
            cell not in expected_cells
            or not isinstance(number, int)
            or isinstance(number, bool)
            or not isinstance(logical, str)
            or not logical
            or not isinstance(attempt, str)
            or not attempt
            or (logical, attempt) in manifest_attempts
        ):
            raise ValueError("parity manifest attempt identity is invalid")
        cells.setdefault(cell, set()).add(number)
        logical_ids.setdefault(cell, set()).add(logical)
        manifest_attempts[(logical, attempt)] = cell[1]
    if set(cells) != expected_cells or any(numbers != {1, 2, 3} for numbers in cells.values()):
        raise ValueError("parity manifest does not contain 3 attempts for every matrix cell")
    stable_logical_ids = {
        next(iter(values)) for values in logical_ids.values() if len(values) == 1
    }
    if len(stable_logical_ids) != len(expected_cells):
        raise ValueError("parity logical identity is not stable and distinct by matrix cell")

    evidence_attempts: dict[tuple[str, str], str] = {}
    for value in values:
        artifact = HarborEvidence.model_validate(value)
        identity = (artifact.run.id, artifact.run.attempt)
        if artifact.run.status != "quality_eligible" or identity in evidence_attempts:
            raise ValueError("parity evidence attempts must be distinct and quality eligible")
        if _contains_provenance(value.get("reviewer") if isinstance(value, dict) else None):
            raise ValueError("reviewer projection contains provenance")
        evidence_attempts[identity] = artifact.reviewer.task.id
    if evidence_attempts != manifest_attempts:
        raise ValueError("parity evidence identities do not match the manifest")

    secrets = tuple(value.encode() for value in credential_values if value)
    if secrets:
        for path in run_dir.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                if any(secret in data for secret in secrets):
                    raise ValueError("manager credential was retained in parity output")


def run_live_parity(
    output_dir: Path,
    *,
    runner: Callable[..., Path],
    pack_path: Path = PARITY_PACK_PATH,
    inventory_loader: Callable[[], dict[str, tuple[str, ...]]] = daytona_secret_inventory,
) -> Path:
    if os.environ.get("MOGIL_RUN_DAYTONA_PARITY") != "1":
        raise PermissionError(
            "set MOGIL_RUN_DAYTONA_PARITY=1 to authorize the paid 18-attempt parity matrix"
        )
    credentials = tuple(
        value
        for name in ("DAYTONA_API_KEY", "DAYTONA_JWT_TOKEN", "DAYTONA_ORGANIZATION_ID")
        if (value := os.environ.get(name))
    )
    if not os.environ.get("DAYTONA_API_KEY") and not (
        os.environ.get("DAYTONA_JWT_TOKEN") and os.environ.get("DAYTONA_ORGANIZATION_ID")
    ):
        raise ValueError(
            "Daytona credentials unavailable: set DAYTONA_API_KEY or both "
            "DAYTONA_JWT_TOKEN and DAYTONA_ORGANIZATION_ID"
        )
    pack = load_pack(pack_path)
    validate_parity_pack(pack)
    validate_secret_inventory(inventory_loader())
    result = runner(pack_path, output_dir, allow_agents=True, attempts=3)
    validate_parity_output(result, credential_values=credentials)
    return result
