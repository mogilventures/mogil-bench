# v0.1.0 release runbook

This is an operator checklist, not authorization to publish. A human maintainer must review the release candidate and explicitly approve tagging and the GitHub release. PyPI publication is deferred.

## 1. Establish a clean release candidate

- [ ] Confirm the implementation PR is reviewed, merged, and all required GitHub checks pass.
- [ ] Start from an up-to-date, clean `main`; `git status --short` must be empty.
- [ ] Confirm `pyproject.toml`, `CHANGELOG.md`, and the intended tag all say `0.1.0` / `v0.1.0`.
- [ ] Confirm Python 3.12, Harbor 0.18.0, Daytona SDK 0.196.0, and container Pi 0.80.6 pins are unchanged.
- [ ] Run `git diff --check`.

## 2. Run the release gates

From a fresh Python 3.12 environment:

```bash
python -m pip install -e '.[dev,daytona]'
pytest -q -m 'not daytona_smoke'
ruff check .
mypy
docker info
pytest -q -m docker_smoke tests/test_harbor_docker_smoke.py
```

The Docker smoke must pass; a skip is not success. The paid/manual Daytona smoke is not a release requirement.

## 3. Build and inspect distributions

```bash
rm -rf build dist
python -m pip install build
python -m build
python -m zipfile --list dist/mogil_bench-0.1.0-py3-none-any.whl
```

- [ ] Both wheel and sdist exist.
- [ ] The wheel includes `mogil_bench/py.typed` and no tests, credentials, run output, or customer data.
- [ ] Package metadata and project URLs are accurate.

Create a clean environment and exercise the installed wheel from the repository checkout:

```bash
python -m venv /tmp/mogil-v010-wheel
/tmp/mogil-v010-wheel/bin/python -m pip install dist/*.whl
/tmp/mogil-v010-wheel/bin/mogil-bench --help
rm -rf /tmp/mogil-v010-sample
/tmp/mogil-v010-wheel/bin/mogil-bench pack validate packs/sample-v1.yaml
/tmp/mogil-v010-wheel/bin/mogil-bench run packs/sample-v1.yaml \
  --output-dir /tmp/mogil-v010-sample
/tmp/mogil-v010-wheel/bin/mogil-bench artifact validate \
  /tmp/mogil-v010-sample/blindbench.json
```

## 4. Security and immutable-runtime review

- [ ] Run the organization-approved secret scan over the checkout **and Git history**; investigate every finding without printing credential values.
- [ ] Confirm fixtures and docs contain only fictional/public data.
- [ ] Confirm `packs/daytona-provider-parity-v1.yaml` uses the reviewed immutable `image@sha256` reference.
- [ ] Follow `docs/daytona-runtime-image.md` to verify that immutable runtime reference and its Python 3.12, `/bin/sh`, and Pi 0.80.6 contract. Do not rebuild or repoint it as part of this release.
- [ ] Confirm the release commit is exactly the reviewed clean `main` commit.

## 5. Publish after explicit approval

- [ ] Create the annotated `v0.1.0` tag at the reviewed release commit and push it without rewriting history.
- [ ] Create the GitHub release from that tag, mark it as a pre-release/public alpha, and use the `CHANGELOG.md` notes.
- [ ] Attach the freshly built wheel and sdist and record their SHA-256 digests.
- [ ] Recheck the tag, GitHub release assets, and immutable runtime reference from a fresh client.
- [ ] Do not publish to PyPI until package-data policy and trusted publishing receive separate approval.
