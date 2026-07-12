from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
RUNTIME = ROOT / "runtime/daytona"
DOCKERFILE = RUNTIME / "Dockerfile"
BUILD_SCRIPT = ROOT / "scripts/build-daytona-runtime.sh"
INSPECT_SCRIPT = ROOT / "scripts/inspect-daytona-runtime.sh"
DOCS = ROOT / "docs/daytona-runtime-image.md"


def test_daytona_runtime_definition_is_pinned_and_context_is_closed() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    dockerignore = (RUNTIME / ".dockerignore").read_text(encoding="utf-8")

    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert len(from_lines) == 2
    assert all(re.search(r"@sha256:[0-9a-f]{64}(?:\s|$)", line) for line in from_lines)
    assert "python:3.12.11-slim-bookworm@sha256:" in dockerfile
    assert "node:22.19.0-bookworm-slim@sha256:" in dockerfile
    package = (RUNTIME / "package.json").read_text(encoding="utf-8")
    assert (
        '"@mariozechner/pi-coding-agent": '
        '"npm:@earendil-works/pi-coding-agent@0.80.6"' in package
    )
    assert "COPY --from=node-runtime" in dockerfile
    assert re.findall(r"^COPY\s+(?!--from=)(.*)$", dockerfile, re.MULTILINE) == [
        "package.json package-lock.json ./"
    ]
    assert not re.search(r"^ADD\s", dockerfile, re.MULTILINE)
    assert dockerignore == "**\n!package.json\n!package-lock.json\n"
    assert {path.name for path in RUNTIME.iterdir()} == {
        "Dockerfile",
        ".dockerignore",
        "package.json",
        "package-lock.json",
    }
    lock = (RUNTIME / "package-lock.json").read_text(encoding="utf-8")
    assert '"lockfileVersion": 3' in lock
    assert '"version": "0.80.6"' in lock
    assert '"integrity": "sha512-' in lock


def test_runtime_scripts_enforce_exact_public_contract() -> None:
    build = BUILD_SCRIPT.read_text(encoding="utf-8")
    inspect = INSPECT_SCRIPT.read_text(encoding="utf-8")

    assert 'CONTEXT="${ROOT}/runtime/daytona"' in build
    assert 'docker build --pull=false' in build
    assert '"${CONTEXT}"' in build
    assert '/usr/local/bin/python' in inspect
    assert 'sys.version_info[:2] == (3, 12)' in inspect
    assert "$(pi --version)" in inspect
    assert "$(pi --version | wc -l)" in inspect
    assert "value in (b" in inspect
    assert "0.80.6\\\\n" in inspect
    assert "/bin/sh" in inspect
    assert "$(node --version)" in inspect
    assert "v22.19.0" in inspect
    assert "$(npm --version)" in inspect
    assert "10.9.3" in inspect
    assert "docker history --no-trunc" in inspect


def test_documented_publish_flow_never_uses_latest_and_captures_digest() -> None:
    docs = DOCS.read_text(encoding="utf-8")

    assert "ghcr.io/mogilventures/mogil-bench-daytona-runtime" in docs
    assert "--push" in docs
    assert "@sha256:" in docs
    assert "docker buildx imagetools inspect" in docs
    assert ":latest" not in docs
    assert "Harbor 0.18.0" in docs
