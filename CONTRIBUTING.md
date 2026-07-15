# Contributing

Mogil Bench is a public-alpha project. Small, reviewable changes that preserve its fail-closed evidence and execution boundaries are welcome.

## Development setup

Python 3.12 and Docker are required for the full suite.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

Before opening a pull request, run:

```bash
pytest -q -m 'not daytona_smoke'
ruff check .
mypy
pytest -q -m docker_smoke tests/test_harbor_docker_smoke.py
git diff --check
```

Tests must not require paid provider calls unless they use the existing explicit live gate. Add behavior-first tests for changes to validation, publication, uploads, or evidence handling. Do not add credentials, customer data, verifier canaries, generated run output, or mutable runtime references.

## Pull requests

- Link the issue and explain the observable behavior change.
- Keep dependency and runtime-pin changes separate and explicitly approved.
- Update user or release documentation when a command or operational boundary changes.
- Do not weaken count reconciliation, identity checks, checksum checks, cleanup checks, or reviewer blinding to make a test pass.

By contributing, you agree that your contribution is licensed under the repository's MIT license.
