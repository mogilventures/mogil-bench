from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pydantic import ValidationError

from .models import BlindBenchBatch, HarborRunRecord
from .uploads import (
    DEFAULT_UPLOAD_TIMEOUT_SECONDS,
    UploadResponseError,
    http_error_diagnostic,
    is_timeout_error,
    read_json_response,
    timeout_diagnostic,
    validate_upload_timeout,
)


class ArtifactError(ValueError):
    pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ArtifactError(f"cannot read JSON {path}: {error}") from error


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ArtifactError(f"path component must not be a symlink: {current.name}")


def _strict_bundle_path(run_dir: Path, bundle_reference: object) -> tuple[Path, Path]:
    if not isinstance(bundle_reference, str):
        raise ArtifactError("Harbor bundle reference must be a string")
    reference = Path(bundle_reference)
    if reference.is_absolute() or not reference.parts or any(
        part in {"", ".", ".."} for part in reference.parts
    ):
        raise ArtifactError("Harbor bundle reference must be a safe relative path")
    _reject_symlink_components(run_dir)
    try:
        root = run_dir.resolve(strict=True)
        current = run_dir
        for part in reference.parts:
            current = current / part
            if current.is_symlink():
                raise ArtifactError("Harbor bundle path must not contain symlinks")
        bundle = current.resolve(strict=True)
    except OSError as error:
        raise ArtifactError("Harbor bundle path cannot be resolved") from error
    if not bundle.is_relative_to(root):
        raise ArtifactError("Harbor bundle path escapes run root")
    return reference, bundle


def _harbor_record(
    run_dir: Path, manifest: dict[str, Any], summary: dict[str, Any]
) -> dict[str, Any]:
    reference, bundle = _strict_bundle_path(run_dir, summary["bundle"])
    try:
        run = HarborRunRecord.model_validate(_read_json(bundle / "run.json"))
    except ValidationError as error:
        raise ArtifactError(f"invalid Mogil Harbor run.json: {error}") from error
    environment_value = _read_json(bundle / "environment.json") if (
        bundle / "environment.json"
    ).is_file() else {"type": "docker"}
    environment_provider = environment_value.get(
        "provider", environment_value.get("type")
    )
    if environment_provider not in {"docker", "daytona"}:
        raise ArtifactError("unsupported Harbor environment provider")
    environment_name = (
        "harbor/docker"
        if environment_provider == "docker"
        else "harbor/isolated-sandbox"
    )
    if summary.get("logical_run_id", run.logical_run_id) != run.logical_run_id:
        raise ArtifactError("manifest logical run identity does not match bundle")
    if summary.get("attempt_id", run.attempt_id) != run.attempt_id:
        raise ArtifactError("manifest attempt identity does not match bundle")
    metadata = {
        "pack_id": manifest["pack"]["id"],
        "pack_revision": manifest["pack"]["revision"],
        "pack_fingerprint": manifest["pack"]["fingerprint"],
        "task_id": summary["task_id"],
        "configuration_id": summary["configuration_id"],
        "category": summary["category"],
        "logical_run_id": run.logical_run_id,
        "attempt_id": run.attempt_id,
        "attempt_number": summary.get("attempt_number", 1),
        "bundle_reference": reference.as_posix(),
        "evidence_status": run.evidence_status.value,
        "agent_outcome": run.agent_outcome.value,
        "verifier_outcome": run.verifier_outcome.value,
        "infrastructure_outcome": run.infrastructure_outcome.value,
    }
    return {
        "version": "1",
        "id": summary["id"],
        "timestamp": manifest["created_at"],
        "model": summary["model"],
        "provider": summary["provider"],
        "input": {"messages": [{"role": "user", "content": summary["prompt"]}]},
        "output": None,
        "product": "mogil-bench",
        "module": summary["lane"],
        "environment": environment_name,
        "harness": summary["harness"],
        "metadata": metadata,
        "privacy_class": summary["privacy_class"],
    }


def export_run(run_dir: Path) -> tuple[Path, Path]:
    manifest = _read_json(run_dir / "manifest.json")
    records: list[dict[str, Any]] = []
    for summary in manifest["results"]:
        if "bundle" in summary:
            records.append(_harbor_record(run_dir, manifest, summary))
            continue
        raw = _read_json(run_dir / summary["raw_artifact"])
        execution = raw["execution"]
        records.append(
            {
                "version": "1",
                "id": raw["id"],
                "timestamp": manifest["created_at"],
                "model": raw["model"],
                "provider": raw["provider"],
                "input": {"messages": [{"role": "user", "content": raw["prompt"]}]},
                "output": {"content": execution["content"]},
                "usage": {"duration_ms": execution["duration_ms"]},
                "product": "mogil-bench",
                "module": raw["lane"],
                "environment": "local",
                "harness": raw["harness"],
                "metadata": {
                    "pack_id": manifest["pack"]["id"],
                    "pack_revision": manifest["pack"]["revision"],
                    "pack_fingerprint": manifest["pack"]["fingerprint"],
                    "task_id": raw["task_id"],
                    "configuration_id": raw["configuration_id"],
                    "category": raw["category"],
                    "status": execution["status"],
                    "output_truncated": execution["output_truncated"],
                    "verification_passed": execution["verification_passed"],
                    "evidence_status": "non_quality",
                },
                "privacy_class": raw["privacy_class"],
            }
        )
    batch = BlindBenchBatch.model_validate({"records": records})
    batch_data = batch.model_dump(mode="json", exclude_none=True)
    json_path = run_dir / "blindbench.json"
    jsonl_path = run_dir / "blindbench.jsonl"
    json_path.write_text(json.dumps(batch_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    jsonl_path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in batch_data["records"]),
        encoding="utf-8",
    )
    return json_path, jsonl_path


def validate_artifact(path: Path) -> int:
    try:
        if path.suffix == ".jsonl":
            records = [
                json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
            ]
            batch = BlindBenchBatch.model_validate({"records": records})
        else:
            batch = BlindBenchBatch.model_validate(_read_json(path))
        for record in batch.records:
            datetime.fromisoformat(record.timestamp.replace("Z", "+00:00"))
        return len(batch.records)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as error:
        raise ArtifactError(f"invalid BlindBench artifact {path}: {error}") from error


def validate_endpoint(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".convex.site")
        or parsed.netloc != parsed.hostname
        or parsed.hostname == ".convex.site"
        or parsed.path != "/ingest/v1/traces"
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ArtifactError(
            "endpoint must be exactly https://<deployment>.convex.site/ingest/v1/traces"
        )


def validate_ingest_counts(payload: Any, expected_records: int) -> dict[str, Any]:
    """Validate BlindBench's counts-only response before calling an upload successful."""
    if not isinstance(payload, dict):
        raise ArtifactError("BlindBench returned an invalid counts response")
    allowed = {
        "traces",
        "imported",
        "deduped",
        "steps",
        "requestMissing",
        "responseMissing",
        "invalid",
        "truncated",
    }
    counts = {key: payload[key] for key in allowed if key in payload}
    required = {"imported", "deduped", "invalid", "truncated"}
    if not required.issubset(counts):
        raise ArtifactError("BlindBench counts response is missing required fields")
    if counts["invalid"] != 0:
        raise ArtifactError(f"BlindBench rejected {counts['invalid']} record(s) as invalid")
    if counts["truncated"] is not False:
        raise ArtifactError("BlindBench truncated the ingest batch; resend the unprocessed suffix")
    accepted = counts["imported"] + counts["deduped"]
    if accepted != expected_records:
        raise ArtifactError(
            f"BlindBench accepted {accepted} of {expected_records} intended record(s)"
        )
    return counts


def upload_artifact(
    path: Path,
    endpoint: str,
    token: str,
    *,
    confirm: bool,
    timeout: float = DEFAULT_UPLOAD_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    try:
        validated_timeout = validate_upload_timeout(timeout)
    except ValueError as error:
        raise ArtifactError(str(error)) from error
    validate_endpoint(endpoint)
    expected_records = validate_artifact(path)
    if not confirm:
        return None
    if not token:
        raise ArtifactError("BLINDBENCH_INGEST_TOKEN is required with --confirm")
    if path.suffix == ".jsonl":
        records = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        ]
        body = json.dumps({"records": records}, separators=(",", ":")).encode()
    else:
        body = path.read_bytes()
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(  # noqa: S310 - endpoint is strictly validated
            request, timeout=validated_timeout
        ) as response:
            payload: Any = read_json_response(response)
    except HTTPError as error:
        diagnostic = http_error_diagnostic(error, body=body, token=token)
        raise ArtifactError(f"BlindBench upload failed: {diagnostic}") from error
    except (TimeoutError, URLError) as error:
        if is_timeout_error(error):
            raise ArtifactError(timeout_diagnostic("BlindBench upload")) from error
        raise ArtifactError(f"BlindBench upload failed: {type(error).__name__}") from error
    except UploadResponseError as error:
        raise ArtifactError(f"BlindBench upload failed: {error}") from error
    except OSError as error:
        raise ArtifactError(f"BlindBench upload failed: {type(error).__name__}") from error
    return validate_ingest_counts(payload, expected_records)
