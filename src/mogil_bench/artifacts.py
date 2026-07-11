from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pydantic import ValidationError

from .models import BlindBenchBatch


class ArtifactError(ValueError):
    pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ArtifactError(f"cannot read JSON {path}: {error}") from error


def export_run(run_dir: Path) -> tuple[Path, Path]:
    manifest = _read_json(run_dir / "manifest.json")
    records: list[dict[str, Any]] = []
    for summary in manifest["results"]:
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


def upload_artifact(
    path: Path, endpoint: str, token: str, *, confirm: bool
) -> dict[str, Any] | None:
    validate_endpoint(endpoint)
    validate_artifact(path)
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
        with urlopen(request, timeout=30) as response:  # noqa: S310 - endpoint is strictly validated
            payload: Any = json.loads(response.read())
    except (HTTPError, URLError, json.JSONDecodeError) as error:
        raise ArtifactError(f"BlindBench upload failed: {type(error).__name__}") from error
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
    return {key: payload[key] for key in allowed if key in payload}
