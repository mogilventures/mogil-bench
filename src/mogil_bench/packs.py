from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import Pack, Task


class PackError(ValueError):
    pass


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def load_pack(path: Path) -> Pack:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        pack = Pack.model_validate(raw)
        for task in pack.tasks:
            if task.fixture:
                resolve_fixture(path, task)
        return pack
    except (OSError, yaml.YAMLError, ValidationError, TypeError) as error:
        raise PackError(f"invalid pack {path}: {error}") from error


def resolve_fixture(pack_path: Path, task: Task) -> Path:
    if not task.fixture:
        raise PackError(f"task {task.id} has no fixture")
    root = pack_path.parent.resolve()
    candidate = (root / task.fixture).resolve()
    if not candidate.is_relative_to(root):
        raise PackError(f"task {task.id} fixture escapes pack directory")
    if not candidate.exists():
        raise PackError(f"task {task.id} fixture does not exist: {task.fixture}")
    return candidate


def task_prompt(pack_path: Path, task: Task) -> str:
    parts = [task.prompt] if task.prompt else []
    if task.fixture:
        fixture = resolve_fixture(pack_path, task)
        if fixture.is_file():
            parts.append(f"Fixture {task.fixture}:\n{fixture.read_text(encoding='utf-8')}")
        else:
            names = sorted(
                str(item.relative_to(fixture)) for item in fixture.rglob("*") if item.is_file()
            )
            parts.append(f"Fixture directory {task.fixture} contains: {', '.join(names)}")
    return "\n\n".join(parts)


def pack_fingerprint(pack_path: Path, pack: Pack) -> str:
    fixture_hashes: dict[str, str] = {}
    for task in pack.tasks:
        if not task.fixture:
            continue
        fixture = resolve_fixture(pack_path, task)
        files = (
            [fixture]
            if fixture.is_file()
            else sorted(item for item in fixture.rglob("*") if item.is_file())
        )
        for item in files:
            key = f"{task.id}/{item.relative_to(fixture) if fixture.is_dir() else fixture.name}"
            fixture_hashes[key] = hashlib.sha256(item.read_bytes()).hexdigest()
    return canonical_hash({"pack": pack.model_dump(mode="json"), "fixtures": fixture_hashes})
