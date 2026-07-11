from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from .models import EvidenceStatus

DEFAULT_MAX_FILES = 4096
DEFAULT_MAX_FILE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_DIFF_BYTES = 512 * 1024
HASH_CHUNK_BYTES = 64 * 1024


class BundleError(ValueError):
    pass


def _files(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BundleError(f"symlinks are not allowed: {path.relative_to(root)}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = path
        elif not path.is_dir():
            raise BundleError(f"special files are not allowed: {path.relative_to(root)}")
    return result


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_paths(root: Path, max_files: int) -> tuple[list[tuple[str, Path]], bool]:
    found: list[tuple[str, Path]] = []
    pending = [root]
    scanned_nodes = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            for item in iterator:
                scanned_nodes += 1
                if scanned_nodes > max_files:
                    return sorted(found), True
                path = Path(item.path)
                relative = path.relative_to(root).as_posix()
                if item.is_symlink():
                    raise BundleError(f"symlinks are not allowed: {relative}")
                if item.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif item.is_file(follow_symlinks=False):
                    found.append((relative, path))
                else:
                    raise BundleError(f"special files are not allowed: {relative}")
    return sorted(found), False


def _bounded_manifest(
    root: Path, *, max_files: int, max_file_bytes: int, max_total_bytes: int
) -> tuple[dict[str, Path], dict[str, Any]]:
    files: dict[str, Path] = {}
    entries: list[dict[str, object]] = []
    omissions: list[dict[str, object]] = []
    candidates, count_limited = _bounded_paths(root, max_files)
    if count_limited:
        omissions.append({"path": "<additional-paths>", "reason": "file_count_limit"})
    total = 0
    for relative, path in candidates:
        metadata = path.stat()
        size = metadata.st_size
        base: dict[str, object] = {
            "path": relative,
            "size": size,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        }
        if size > max_file_bytes:
            omissions.append({**base, "reason": "per_file_byte_limit"})
            continue
        if total + size > max_total_bytes:
            omissions.append({**base, "reason": "total_byte_limit"})
            continue
        total += size
        digest = _hash_file(path)
        entries.append({**base, "sha256": digest})
        files[relative] = path
    return files, {
        "complete": not omissions,
        "files": entries,
        "omissions": omissions,
        "limits": {
            "max_files": max_files,
            "max_file_bytes": max_file_bytes,
            "max_total_bytes": max_total_bytes,
        },
        "hashed_bytes": total,
    }


def _text_lines(data: bytes) -> list[str] | None:
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def _patch_entry(
    relative: str, before: Path | None, after: Path | None, *, max_diff_bytes: int
) -> str:
    before_size = before.stat().st_size if before is not None else 0
    after_size = after.stat().st_size if after is not None else 0
    before_data = (
        before.read_bytes() if before is not None and before_size <= max_diff_bytes else b""
    )
    after_data = (
        after.read_bytes() if after is not None and after_size <= max_diff_bytes else b""
    )
    before_mode = stat.S_IMODE(before.stat().st_mode) if before is not None else None
    after_mode = stat.S_IMODE(after.stat().st_mode) if after is not None else None
    lines = [f"diff --git a/{relative} b/{relative}\n"]
    if before_mode != after_mode:
        if before_mode is None:
            lines.append(f"new file mode 100{after_mode:03o}\n")
        elif after_mode is None:
            lines.append(f"deleted file mode 100{before_mode:03o}\n")
        else:
            lines.extend(
                [f"old mode 100{before_mode:03o}\n", f"new mode 100{after_mode:03o}\n"]
            )
    if before_size > max_diff_bytes or after_size > max_diff_bytes:
        lines.append(
            f"MOGIL EVIDENCE BOUNDED: a/{relative} or b/{relative} exceeds "
            f"diff limit {max_diff_bytes} bytes\n"
        )
        return "".join(lines)
    before_lines = _text_lines(before_data)
    after_lines = _text_lines(after_data)
    if before_data != after_data:
        if before_lines is None or after_lines is None:
            lines.append(f"Binary files a/{relative} and b/{relative} differ\n")
        else:
            lines.extend(
                difflib.unified_diff(
                    before_lines,
                    after_lines,
                    fromfile=f"a/{relative}" if before is not None else "/dev/null",
                    tofile=f"b/{relative}" if after is not None else "/dev/null",
                )
            )
    return "".join(lines)


def build_workspace_evidence(
    before: Path,
    after: Path,
    output: Path,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
) -> None:
    before_files, before_manifest = _bounded_manifest(
        before,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )
    after_files, after_manifest = _bounded_manifest(
        after,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )
    output.mkdir(parents=True, exist_ok=False)
    before_entries = {item["path"]: item for item in before_manifest["files"]}
    after_entries = {item["path"]: item for item in after_manifest["files"]}
    changed: list[dict[str, str]] = []
    patches: list[str] = []
    for relative in sorted(before_files.keys() | after_files.keys()):
        old = before_files.get(relative)
        new = after_files.get(relative)
        if old is None:
            status = "added"
        elif new is None:
            status = "deleted"
        elif before_entries[relative]["sha256"] != after_entries[relative]["sha256"]:
            status = "modified"
        elif before_entries[relative]["mode"] != after_entries[relative]["mode"]:
            status = "mode_changed"
        else:
            continue
        changed.append({"path": relative, "status": status})
        patches.append(_patch_entry(relative, old, new, max_diff_bytes=max_diff_bytes))
    omissions = [*before_manifest["omissions"], *after_manifest["omissions"]]
    for omission in omissions:
        changed.append(
            {
                "path": str(omission["path"]),
                "status": "evidence_omitted",
                "reason": str(omission["reason"]),
            }
        )
        patches.append(
            f"MOGIL EVIDENCE BOUNDED: {omission['path']} omitted "
            f"({omission['reason']})\n"
        )
    for name, value in (
        ("before-manifest.json", before_manifest),
        ("after-manifest.json", after_manifest),
        ("changed-files.json", changed),
    ):
        (output / name).write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    (output / "patch.diff").write_text("".join(patches), encoding="utf-8")


def _relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise BundleError(f"unsafe relative path: {value!r}")
    return path


def collect_files(
    source_root: Path,
    bundle_root: Path,
    files: dict[str, str],
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> None:
    source_root = source_root.resolve(strict=True)
    planned: list[tuple[Path, Path, int]] = []
    total = 0
    for destination_name, source_name in files.items():
        destination_relative = _relative_path(destination_name)
        source_relative = _relative_path(source_name)
        source = source_root.joinpath(*source_relative.parts)
        try:
            metadata = source.lstat()
        except OSError as error:
            raise BundleError(f"cannot inspect source path: {source_name}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise BundleError(f"source is a symlink: {source_name}")
        if not stat.S_ISREG(metadata.st_mode):
            raise BundleError(f"source is not a regular file: {source_name}")
        resolved = source.resolve(strict=True)
        if not resolved.is_relative_to(source_root):
            raise BundleError(f"source path escapes collection root: {source_name}")
        if metadata.st_size > max_file_bytes:
            raise BundleError(f"source exceeds per-file size limit: {source_name}")
        total += metadata.st_size
        if total > max_total_bytes:
            raise BundleError("sources exceed total size limit")
        planned.append(
            (resolved, bundle_root.joinpath(*destination_relative.parts), metadata.st_size)
        )
    bundle_root.mkdir(parents=True, exist_ok=True)
    for source, destination, expected_size in planned:
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        if len(data) != expected_size:
            raise BundleError(f"source changed during collection: {source.name}")
        destination.write_bytes(data)


def write_checksums(bundle_root: Path) -> Path:
    checksum_path = bundle_root / "checksums.sha256"
    entries: list[str] = []
    for relative, path in _files(bundle_root).items():
        if relative == "checksums.sha256":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append(f"{digest}  {relative}\n")
    checksum_path.write_text("".join(entries), encoding="utf-8")
    return checksum_path


def validate_checksums(bundle_root: Path) -> bool:
    checksum_path = bundle_root / "checksums.sha256"
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
        expected: dict[str, str] = {}
        for line in lines:
            digest, relative = line.split("  ", 1)
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                return False
            _relative_path(relative)
            if relative in expected:
                return False
            expected[relative] = digest
        actual = {
            relative: hashlib.sha256(path.read_bytes()).hexdigest()
            for relative, path in _files(bundle_root).items()
            if relative != "checksums.sha256"
        }
        return expected == actual
    except (BundleError, OSError, UnicodeError, ValueError):
        return False


def read_reward(path: Path) -> dict[str, float]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        required = {"reward", "command_exit", "stdout_assertion"}
        if not isinstance(raw, dict) or not required.issubset(raw):
            raise ValueError("missing reward dimensions")
        result: dict[str, float] = {}
        for key in required:
            value = raw[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError("non-numeric reward")
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError("non-finite reward")
            result[key] = numeric
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise BundleError(f"invalid verifier reward: {error}") from error


REQUIRED_BUNDLE_FILES = (
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
)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path.name}")
    return value


def classify_evidence(
    bundle_root: Path,
    *,
    harbor_execution: bool = True,
    deterministic_fixture: bool = False,
) -> EvidenceStatus:
    if not harbor_execution:
        return EvidenceStatus.NON_QUALITY
    if not deterministic_fixture:
        return EvidenceStatus.INSUFFICIENT
    try:
        if any(not (bundle_root / relative).is_file() for relative in REQUIRED_BUNDLE_FILES):
            return EvidenceStatus.INSUFFICIENT
        if not validate_checksums(bundle_root):
            return EvidenceStatus.INSUFFICIENT
        for relative in REQUIRED_BUNDLE_FILES:
            if relative.endswith(".json"):
                json.loads((bundle_root / relative).read_text(encoding="utf-8"))
        run = _read_object(bundle_root / "run.json")
        environment = _read_object(bundle_root / "environment.json")
        cleanup = _read_object(bundle_root / "cleanup.json")
        verification = _read_object(bundle_root / "verifier/verification.json")
        reward = read_reward(bundle_root / "verifier/reward.json")
        if run.get("bundle_version") != "1":
            return EvidenceStatus.INSUFFICIENT
        run_identifiers = (
            "logical_run_id",
            "attempt_id",
            "harbor_job_id",
            "harbor_trial_id",
            "trial_name",
            "task_checksum",
        )
        if any(not isinstance(run.get(field), str) or not run[field] for field in run_identifiers):
            return EvidenceStatus.INSUFFICIENT
        if run.get("harbor_version") != "0.18.0" or run.get("pi_version") != "0.80.6":
            return EvidenceStatus.INSUFFICIENT
        if run.get("agent_outcome") != "succeeded" or run.get("verifier_outcome") != "passed":
            return EvidenceStatus.INSUFFICIENT
        if run.get("infrastructure_outcome") != "succeeded":
            return EvidenceStatus.INSUFFICIENT
        if environment.get("type") != "docker" or environment.get("mounts") != []:
            return EvidenceStatus.INSUFFICIENT
        if environment.get("delete") is not True:
            return EvidenceStatus.INSUFFICIENT
        if not isinstance(environment.get("requested"), dict) or not isinstance(
            environment.get("effective"), dict
        ):
            return EvidenceStatus.INSUFFICIENT
        if cleanup.get("status") != "confirmed" or cleanup.get("remaining_container_ids") != []:
            return EvidenceStatus.INSUFFICIENT
        if cleanup.get("error") is not None or not cleanup.get("project_identifiers"):
            return EvidenceStatus.INSUFFICIENT
        if not cleanup.get("compose_project_labels"):
            return EvidenceStatus.INSUFFICIENT
        if verification.get("verifier_outcome") != "passed":
            return EvidenceStatus.INSUFFICIENT
        if verification.get("infrastructure_outcome") != "succeeded":
            return EvidenceStatus.INSUFFICIENT
        verification_fields = (
            "started_at",
            "ended_at",
            "duration_ms",
            "timed_out",
            "exit_code",
            "expected_exit_code",
            "command_exit_passed",
            "stdout_assertion_passed",
            "stdout_truncated",
            "stderr_truncated",
        )
        if any(field not in verification for field in verification_fields):
            return EvidenceStatus.INSUFFICIENT
        before = _read_object(bundle_root / "workspace/before-manifest.json")
        after = _read_object(bundle_root / "workspace/after-manifest.json")
        changed = json.loads(
            (bundle_root / "workspace/changed-files.json").read_text(encoding="utf-8")
        )
        if (
            before.get("complete") is not True
            or after.get("complete") is not True
            or not isinstance(before.get("files"), list)
            or not isinstance(after.get("files"), list)
        ):
            return EvidenceStatus.INSUFFICIENT
        if not isinstance(changed, list) or not changed:
            return EvidenceStatus.INSUFFICIENT
        if not (bundle_root / "workspace/patch.diff").read_bytes():
            return EvidenceStatus.INSUFFICIENT
        if reward != {"reward": 1.0, "command_exit": 1.0, "stdout_assertion": 1.0}:
            return EvidenceStatus.INSUFFICIENT
        if not (bundle_root / "agent/pi.txt").read_bytes():
            return EvidenceStatus.INSUFFICIENT
        return EvidenceStatus.FIXTURE_COMPLETE
    except (BundleError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return EvidenceStatus.INSUFFICIENT
