from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_public_alpha_package_metadata_is_complete_and_pins_are_preserved() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert metadata["version"] == "0.1.0"
    assert metadata["authors"] == [{"name": "Mogil Ventures"}]
    assert metadata["maintainers"] == [{"name": "Noah Mogil"}]
    assert metadata["urls"] == {
        "Homepage": "https://github.com/mogilventures/mogil-bench",
        "Repository": "https://github.com/mogilventures/mogil-bench",
        "Issues": "https://github.com/mogilventures/mogil-bench/issues",
        "Changelog": "https://github.com/mogilventures/mogil-bench/blob/main/CHANGELOG.md",
    }
    assert {
        "Development Status :: 3 - Alpha",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3.12",
        "Typing :: Typed",
    }.issubset(metadata["classifiers"])
    assert metadata["dependencies"] == [
        "harbor==0.18.0",
        "pydantic>=2.7,<3",
        "PyYAML>=6.0,<7",
        "typer>=0.12,<1",
    ]
    assert metadata["optional-dependencies"]["daytona"] == [
        "daytona==0.196.0",
        "harbor[daytona]==0.18.0",
    ]


def test_public_release_documents_and_distribution_ci_are_present() -> None:
    for relative in (
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CHANGELOG.md",
        "docs/release.md",
    ):
        value = (ROOT / relative).read_text(encoding="utf-8")
        assert value.strip(), relative

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "public alpha" in readme.lower()
    assert "export paired-comparison" in readme
    assert "--timeout 120" in readme
    release = (ROOT / "docs/release.md").read_text(encoding="utf-8")
    for required in (
        "pytest -q -m 'not daytona_smoke'",
        "ruff check .",
        "mypy",
        "docker_smoke",
        "python -m build",
        "git diff --check",
        "secret",
        "tag",
        "GitHub release",
        "immutable",
    ):
        assert required in release

    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "python -m build" in ci
    assert "dist/*.whl" in ci
    assert "mogil-bench --help" in ci
    assert "packs/sample-v1.yaml" in ci
