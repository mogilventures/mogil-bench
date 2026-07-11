# Issue #7: Harbor Foundation Implementation Plan

**Date:** 2026-07-11

**Branch:** `issue-7-harbor-foundation`
**Goal:** Prove one narrow, reproducible path from a Mogil coding task/configuration through Harbor 0.18.0 and local Docker to a retained Mogil evidence bundle and backward-compatible BlindBench `eval-record` v1 export.

## Phase 1 contract

```text
Mogil task/config
  -> deterministic Harbor task translation
  -> one Harbor local-Docker trial
  -> separate hidden verifier
  -> Mogil-owned retained evidence bundle
  -> existing BlindBench eval-record v1 JSON/JSONL
```

Phase 1 is one task, one configuration, one attempt, local Docker only. Job concurrency is `1`; Harbor retries are `0`. A retry is a new Mogil attempt with a new attempt ID, never a silent Harbor retry.

### Done when

1. A credential-free fixture performs a real Harbor 0.18.0 Docker trial, changes a known file, passes a separate no-network verifier, retains the required evidence, and confirms cleanup.
2. Translation tests prove a verifier-only random canary cannot enter the prompt, agent environment, agent configuration/logs, candidate workspace, or default BlindBench export.
3. Every execution has a stable logical run ID and unique attempt ID, and its bundle is classified as exactly `non_quality`, `insufficient`, or `fixture_complete`; Phase 1 cannot produce `quality_eligible`.
4. Existing mock behavior, BlindBench v1 validation, and guarded upload/count behavior remain compatible.
5. Exact unit, lint, type-check, and Docker smoke commands pass under Python 3.12.

### Stop conditions

- Stop rather than adding another Harbor environment, task type, attempt, scheduler, or model matrix.
- Stop if Harbor 0.18.0 cannot provide separate verifier isolation without patching Harbor; document the incompatibility and do not weaken isolation.
- Stop if a required artifact cannot be captured before teardown; classify the attempt `insufficient`, never infer completeness from reward.
- Do not broaden the PR to solve a deferred issue listed below.

## Fixed compatibility decisions

- Python: `>=3.12,<3.13`; CI runs Python 3.12 only.
- Harbor: direct exact dependency `harbor==0.18.0` (reviewed tag commit `527d50deb63a5d279e8c20593c18a2cbc7f61f9e`).
- Pi: container-side `@mariozechner/pi-coding-agent@0.80.6`, passed through Harbor Pi `AgentConfig.kwargs` as `{"version": "0.80.6"}`. Never use Harbor's `@latest` default.
- No `harbor[cloud]`, `harbor[all]`, Daytona extra, Harbor fork, or direct Harbor object in public Mogil schemas.
- Put `HARBOR_VERSION = "0.18.0"` and `PI_VERSION = "0.80.6"` in `src/mogil_bench/harbor_backend.py`. Assert the installed Harbor version at startup and record both pins in every Harbor bundle.
- Any Harbor or Pi version change is a dedicated compatibility PR. It must rerun all translation/ingestion tests and the real Docker smoke, and explicitly update parsing for changed lock/result layouts.

## Planned file surface

Create:

- `src/mogil_bench/harbor_backend.py` — pins, Harbor import/version boundary, Docker preflight, one-attempt job execution, Harbor result ingestion, cleanup confirmation.
- `src/mogil_bench/harbor_tasks.py` — deterministic Mogil-to-Harbor task tree translation and verifier wrapper generation.
- `src/mogil_bench/run_bundle.py` — Mogil-owned bundle models, evidence classification, safe artifact copying, manifests, patch, and checksums.
- `tests/test_harbor_translation.py` — task/config translation, identity, mount rejection, and canary tests.
- `tests/test_harbor_bundle.py` — bundle completeness, checksums, evidence states, and BlindBench projection tests.
- `tests/test_harbor_backend.py` — preflight, Harbor boundary, failure paths, and cleanup tests with controlled fakes.
- `tests/test_harbor_docker_smoke.py` — real credential-free Harbor + Docker smoke, marked `docker_smoke`.
- `tests/fixtures/harbor-coding-task/calculator.py` — intentionally incorrect candidate fixture.
- `tests/fixtures/harbor-coding-task/agent.py` — deterministic test-only Harbor agent that corrects the fixture and emits agent/artifact logs.
- `tests/fixtures/harbor-coding-task/verify.py` — hidden verifier template containing a per-run canary.

Modify:

- `pyproject.toml` — Python and dependency pins, Ruff/mypy 3.12 targets, pytest marker.
- `src/mogil_bench/models.py` — add the Harbor adapter/config fields and Mogil-owned run/attempt/evidence models only.
- `src/mogil_bench/runner.py` — dispatch eligible `pi-coding` work to `HarborBackend`; retain mock/command paths; Harbor must never call host `_pi()`.
- `src/mogil_bench/artifacts.py` — consume Mogil bundles and preserve `eval-record` v1; export safe summaries/references only.
- `tests/test_mogil_bench.py` — retain existing regressions and update only assertions affected by the new explicit backend/evidence metadata.
- `.github/workflows/ci.yml` — Python 3.12 unit job plus explicit Docker smoke job.
- `README.md` — prerequisites, security boundary, fixture smoke, bundle layout, cleanup command, and deferrals.
- `docs/architecture.md` — Harbor compatibility boundary and evidence flow.

Do not modify pack fixtures merely to make the smoke pass; the Harbor smoke owns its isolated test fixture under `tests/fixtures/`.

## Data and behavior contracts

### Configuration and identity

Extend configuration validation narrowly:

- `adapter="harbor"` is valid only for `lane="pi-coding"`, with `allow_agents: true` and operator `--allow-agents` still required.
- Phase 1 accepts only `backend="harbor"` and `environment_type="docker"` (defaults may be serialized explicitly). Reject every other environment and every non-empty mount list.
- Translate provider/model to Harbor `model_name=f"{provider}/{model}"` and Pi agent config to `name="pi"`, `kwargs={"version": PI_VERSION}`.
- Preserve the existing content-derived result ID as `logical_run_id`.
- Generate a UUIDv4/UUID7-style unique `attempt_id` immediately before each actual execution. It must not alter logical identity. Use it in Harbor `job_name`, trial naming, and the bundle path.
- Persist Harbor job UUID, trial UUID/name, task checksum/URI, and environment/session/project/container identifiers when present. Missing optional Harbor identifiers do not invent values.

### Task translation and verifier isolation

Generate a fresh temporary tree per attempt:

```text
<translation-root>/<attempt-id>/
  instruction.md
  task.toml
  environment/
    Dockerfile
    workspace/...
  tests/
    Dockerfile
    test.sh
    verify.py
```

Translation rules:

- `instruction.md` contains only the Mogil prompt and public task context. It must not contain verifier argv, expected exit code/text, hidden paths, assertion/reward logic, tests, or canary.
- Build the agent image from `environment/`, never from the generated task root. Copy fixture bytes; do not symlink or bind-mount the source pack/repository.
- Build the verifier image from `tests/`; configure Harbor task schema `1.3` with verifier `environment_mode="separate"` and verifier network `no-network`.
- Agent networking may be `public` only for real Pi provider API access. The deterministic fixture agent needs no credentials and should use no network where Harbor permits it.
- Agent and verifier timeouts come from Mogil task/verifier values without silent increases. Set explicit CPU, memory, and storage values and record requested and effective values.
- Only an allowlisted candidate workspace/artifact path crosses to the verifier. Verifier logs return only after execution through `/logs/verifier` and must never be copied into the candidate workspace or agent trajectory.
- Run verifier argv as an argv array with no shell interpolation. The wrapper records exit code, expected exit code, stdout check result, full bounded stdout/stderr with independent truncation flags, start/end/duration, and timeout.
- Emit Harbor `/logs/verifier/reward.json` with separate `reward`, `command_exit`, and `stdout_assertion` rewards. Missing, empty, or unparseable rewards are infrastructure/evidence failures, not score zero.
- Emit richer Mogil `/logs/verifier/verification.json`. Keep agent process, verifier outcome, infrastructure outcome, and evidence completeness independent. Never store a human label in the execution record.

The canary test creates `HIDDEN_VERIFIER_CANARY_<random>` only in verifier code, translates/runs the fixture, then recursively searches `instruction.md`, `environment/`, serialized agent config, raw Pi/agent logs, candidate workspace, agent-visible artifacts, and BlindBench JSON/JSONL. Any match fails. Raw retained verifier logs may contain it but are not reviewer-exported by default.

### Docker preflight and security

Run preflight before creating the final output run directory:

1. Running interpreter is Python 3.12.
2. Installed Harbor version is exactly 0.18.0.
3. `docker` executable exists.
4. `docker info` succeeds against the daemon.
5. Configuration is local Docker, concurrency 1, retries 0, delete true, and mounts empty.

Fail with setup guidance and leave no partial output directory. Never mount the host repository, home, Docker socket, SSH directory, cloud configuration, credential files, or arbitrary user paths. Local Docker shares the host kernel and is not a VM-grade trust boundary.

Use `delete=True`. In `finally`, retain cleanup request/start/end/error, Harbor Compose project/container identifiers, then query Docker by the exact identifier. Write `cleanup_status` as `confirmed`, `failed`, or `unknown`; a successful stop call alone is not confirmation. Cleanup failure changes infrastructure status and therefore prevents `fixture_complete`. Document an exact label/project-prefix `docker ps`/`docker rm` manual cleanup command; a general reaper is out of scope.

### Mogil evidence bundle

Copy Harbor output into a versioned Mogil-owned bundle before Harbor temporary files disappear:

```text
results/<logical-run-id>/<attempt-id>/
  run.json
  environment.json
  cleanup.json
  checksums.sha256
  harbor/
    job-config.json
    job-lock.json
    trial-config.json
    trial-lock.json
    trial-result.json
    trial.log
  agent/
    pi.txt
  workspace/
    before-manifest.json
    after-manifest.json
    patch.diff
    changed-files.json
  verifier/
    verification.json
    stdout.txt
    stderr.txt
    reward.json
  artifacts/
    harbor-manifest.json
```

- Capture before/after manifests, untracked files, deletions, permissions, and `patch.diff` inside the environment after agent termination and before teardown; publish them through `/logs/artifacts`.
- Preserve Harbor's `agent/pi.txt` bytes exactly and label its format `raw_filtered_pi_events`. Harbor 0.18.0 removes `message_update` events; do not call it complete trajectory or ATIF.
- Retain Harbor resolved config, locks, result, and log. Convert SDK objects immediately to Mogil-owned JSON; BlindBench must not depend on Harbor's on-disk layout.
- Hash every retained file in `checksums.sha256`. Safe collection rejects absolute/`..` paths, symlink escapes, device nodes, sockets, and configured per-file/total size limits.
- Missing any required Pi/agent log, verifier output/reward, before/after manifest, patch, Harbor lock/result, or confirmed cleanup makes evidence `insufficient`, even if reward is 1.

Evidence state is a closed Phase 1 enum:

- `non_quality`: mock, command, or final-answer-only legacy evidence.
- `insufficient`: a Harbor execution lacks, corrupts, or cannot confirm any required evidence.
- `fixture_complete`: only the deterministic credential-free Harbor+Docker fixture meets every required artifact, integrity, isolation, verifier, and cleanup check.

`quality_eligible` is reserved and must be rejected by Phase 1 validation. Real Pi output remains `insufficient` until #6 supplies and gates complete trajectory evidence.

### BlindBench v1 compatibility

Keep `BlindBenchRecord.version == "1"`, existing `/ingest/v1/traces`, JSON/JSONL shapes, validation, and upload count semantics. For Harbor records:

- Use `environment="harbor/docker"`.
- Keep the stable logical run ID as the BlindBench record ID; put attempt ID and safe bundle references in metadata.
- Include evidence state and separate process/verifier/infrastructure summaries.
- Do not include hidden verifier argv, expected values, test names/content, canary, unrestricted verifier logs, host/container identifiers, absolute paths, or Harbor SDK payloads.
- Existing mock records remain valid and gain `evidence_status="non_quality"` without changing their deterministic IDs.
- Do not implement BlindBench's proposed run-level schema/endpoint in this issue.

## Bite-sized TDD sequence

Each task starts with the named focused test failing, implements the smallest production change, reruns that focused test, then runs the cumulative unit suite.

### Task 1 — Pin the compatibility envelope

**Files:** `pyproject.toml`, `src/mogil_bench/harbor_backend.py`, `tests/test_harbor_backend.py`, `.github/workflows/ci.yml`

1. Add failing `test_compatibility_versions_are_exact` and `test_rejects_incompatible_harbor_version`.
2. Change Python to `>=3.12,<3.13`, add `harbor==0.18.0`, and set Ruff `py312`/mypy `3.12`.
3. Add version constants plus a lazy Harbor import/version assertion so module import remains testable and incompatibility fails loudly.
4. Make CI's normal job Python 3.12 only; do not add the smoke job yet.
5. Run `pytest -q tests/test_harbor_backend.py -k 'compatibility or incompatible'`.

### Task 2 — Model the narrow backend and identities

**Files:** `src/mogil_bench/models.py`, `tests/test_harbor_backend.py`, `tests/test_mogil_bench.py`

1. Add failing tests for Harbor opt-in, unsupported lane/environment, non-empty mounts, stable logical ID, and unique attempt IDs.
2. Add only Mogil-owned enums/models for backend/environment, attempt identity, outcome dimensions, cleanup, and the three legal evidence states.
3. Add `adapter="harbor"`; reject unsupported combinations and `quality_eligible`.
4. Extract/reuse logical-ID calculation and add an injectable attempt-ID factory for deterministic unit tests.
5. Run `pytest -q tests/test_harbor_backend.py -k 'identity or config or mount or lane' tests/test_mogil_bench.py`.

### Task 3 — Translate a task without leaking the verifier

**Files:** `src/mogil_bench/harbor_tasks.py`, `tests/test_harbor_translation.py`, `tests/fixtures/harbor-coding-task/*`

1. Add failing tree/schema/config tests and a random hidden-canary recursive search.
2. Implement deterministic fixture copying, source manifesting, `instruction.md`, separate `environment/` and `tests/` build contexts, verifier wrapper, and schema-1.3 `task.toml`.
3. Assert one task/agent/attempt, `provider/model`, Pi 0.80.6, explicit resources/timeouts, separate verifier, no-network verifier, no mounts, concurrency 1, retries 0, and delete true.
4. Ensure the deterministic test agent is explicitly test-only and does not masquerade as Pi.
5. Run `pytest -q tests/test_harbor_translation.py`.

### Task 4 — Capture workspace and verifier evidence

**Files:** `src/mogil_bench/harbor_tasks.py`, `src/mogil_bench/run_bundle.py`, `tests/test_harbor_bundle.py`

1. Add failing tests for untracked/modified/deleted files, patch generation, bounded verifier streams/truncation, timeout, stdout assertion, malformed reward, and path/symlink/device/size rejection.
2. Implement in-container before/after manifest and patch scripts plus the verifier wrapper outputs.
3. Implement safe copying into the exact bundle layout and SHA-256 manifest validation.
4. Run `pytest -q tests/test_harbor_bundle.py -k 'workspace or verifier or checksum or unsafe or truncated'`.

### Task 5 — Classify evidence conservatively

**Files:** `src/mogil_bench/run_bundle.py`, `tests/test_harbor_bundle.py`

1. Add table-driven failing tests that remove each required artifact in turn.
2. Implement `non_quality`, `insufficient`, and fixture-only `fixture_complete`; make absent/corrupt artifacts and unconfirmed cleanup `insufficient`.
3. Add an explicit test that reward 1 cannot override missing evidence and `quality_eligible` cannot be emitted.
4. Run `pytest -q tests/test_harbor_bundle.py -k 'evidence or missing or fixture_complete or quality_eligible'`.

### Task 6 — Add Docker preflight before output creation

**Files:** `src/mogil_bench/harbor_backend.py`, `tests/test_harbor_backend.py`

1. Add failing tests for wrong Python, wrong Harbor, missing Docker executable, unreachable daemon, and preflight failure leaving no output.
2. Implement injectable command/version probes and clear setup errors.
3. Validate security invariants immediately before constructing the Harbor job.
4. Run `pytest -q tests/test_harbor_backend.py -k 'preflight or docker or output'`.

### Task 7 — Execute and ingest one Harbor attempt

**Files:** `src/mogil_bench/harbor_backend.py`, `src/mogil_bench/run_bundle.py`, `tests/test_harbor_backend.py`

1. Add controlled-fake failing tests for exact `JobConfig`/`TaskConfig`/`AgentConfig`, one attempt, concurrency 1, retries 0, Pi pin, result parsing, and incompatible Harbor layouts.
2. Implement `HarborBackend.run_attempt(...)` behind the lazy compatibility boundary and immediately project Harbor objects into Mogil JSON.
3. Retain config/lock/result/log files and exposed identifiers without inventing missing values.
4. Cover setup exception, agent non-zero/exception/timeout, verifier failure/timeout, artifact collection failure, and cancellation if Harbor exposes a deterministic cancellation hook.
5. Run `pytest -q tests/test_harbor_backend.py -k 'job or result or failure or timeout or artifact or cancellation'`.

### Task 8 — Confirm cleanup on every exit path

**Files:** `src/mogil_bench/harbor_backend.py`, `src/mogil_bench/run_bundle.py`, `tests/test_harbor_backend.py`

1. Add failing cleanup tests for success, agent failure, verifier failure, timeout, and simulated cleanup failure.
2. Implement `finally` cleanup recording and exact Docker identifier post-inspection.
3. Ensure cleanup error survives ingestion, changes infrastructure outcome, and prevents `fixture_complete`.
4. Run `pytest -q tests/test_harbor_backend.py -k 'cleanup'`.

### Task 9 — Dispatch Harbor without host Pi

**Files:** `src/mogil_bench/runner.py`, `tests/test_harbor_backend.py`, `tests/test_mogil_bench.py`

1. Add failing dispatch test that monkeypatches `_pi` to raise and proves a Harbor config never calls it.
2. Preflight all Harbor attempts before creating the run output, then dispatch one coding task/configuration to `HarborBackend`.
3. Preserve mock and guarded command behavior and existing matrix continuation semantics.
4. Run `pytest -q tests/test_harbor_backend.py -k 'dispatch or host_pi' tests/test_mogil_bench.py`.

### Task 10 — Project bundles to BlindBench v1

**Files:** `src/mogil_bench/artifacts.py`, `tests/test_harbor_bundle.py`, `tests/test_mogil_bench.py`

1. Add failing tests for `harbor/docker`, logical/attempt IDs, evidence/outcome metadata, canary absence, and both JSON/JSONL validation.
2. Read the Mogil bundle rather than Harbor's live directory and emit reviewer-safe summaries/references.
3. Mark legacy/mock records `non_quality` while preserving stable IDs and all v1 upload/count behavior.
4. Run `pytest -q tests/test_harbor_bundle.py -k 'blindbench or canary' tests/test_mogil_bench.py -k 'export or artifact or upload or mock'`.

### Task 11 — Real credential-free Harbor + Docker smoke

**Files:** `tests/test_harbor_docker_smoke.py`, `tests/fixtures/harbor-coding-task/*`, `.github/workflows/ci.yml`

1. Add a `docker_smoke` test that runs the deterministic test agent in a real Harbor Docker environment; no model/provider credential may be read.
2. Assert actual Docker use, known `calculator.py` correction, reward 1 from a separate verifier, retained rich verifier logs, all Harbor config/lock/result files, manifests/patch, canary absence, checksum validity, confirmed cleanup, valid BlindBench v1, and `fixture_complete` only.
3. Add an explicit Docker CI job. If Docker is expected but unavailable, fail; never skip to green. Keep Docker-independent unit tests in the normal job.
4. Run `pytest -q -m docker_smoke tests/test_harbor_docker_smoke.py`.

### Task 12 — Document operations and boundaries

**Files:** `README.md`, `docs/architecture.md`

1. Document Python/Docker prerequisites, exact pins, preflight, local-kernel risk, no-mount policy, bundle layout, evidence states, smoke command, and manual cleanup command.
2. Replace trusted host-Pi architecture claims for Harbor configurations while documenting that legacy paths remain for compatibility.
3. Document filtered Pi events as incomplete and link deferrals by issue number.
4. Execute every documented credential-free command from the repository root in a fresh temporary output path.

## Exact verification gates

Run from repository root with Python 3.12 and a clean Docker-capable environment:

```bash
python --version  # must be 3.12.x
python -m pip install -e '.[dev]'
pytest -q tests/test_harbor_translation.py
pytest -q tests/test_harbor_bundle.py
pytest -q tests/test_harbor_backend.py
pytest -q tests/test_mogil_bench.py
pytest -q
ruff check .
mypy src
pytest -q -m docker_smoke tests/test_harbor_docker_smoke.py
rm -rf /tmp/mogil-bench-foundation-smoke
mogil-bench pack validate packs/sample-v1.yaml
mogil-bench run packs/sample-v1.yaml --output-dir /tmp/mogil-bench-foundation-smoke
mogil-bench artifact validate /tmp/mogil-bench-foundation-smoke/blindbench.json
mogil-bench artifact validate /tmp/mogil-bench-foundation-smoke/blindbench.jsonl
git diff --check
```

The broad `pytest -q` excludes no mandatory Docker-independent test. The Docker smoke is a separate mandatory command/job and a skip is a failure in that job. Before merge, inspect one fresh fixture bundle, verify `checksums.sha256`, search all agent-visible/default-export files for the recorded canary, and confirm `docker ps -a` contains no exact trial project/container identifier.

## Rollback and upgrade boundary

The feature remains isolated behind `adapter="harbor"`; mock and command behavior and BlindBench v1 stay intact. If Phase 1 must be rolled back, remove Harbor dispatch/dependency/config support and its new tests/fixtures while leaving existing v1 record IDs, artifact validation, and upload endpoint unchanged. Existing Harbor bundles are immutable evidence and must not be deleted or rewritten; the exporter should either continue reading their versioned Mogil schema or fail with an explicit unsupported-bundle-version error.

Do not loosen exact pins to fix resolution. A Harbor/Pi upgrade requires a dedicated PR that updates the compatibility constants, lock/result parsers and expected fixture output, then reruns translation canary tests, every failure/cleanup test, the full suite, and the real Docker smoke. No compatibility shim may silently accept an unreviewed Harbor layout.

## Explicit deferrals

- **#6:** Pi event normalization, complete append-only trajectory, ATIF v1.7, credentialed real-Pi activation, OTLP, and any `quality_eligible` gate.
- **#8:** Daytona, provider conformance, remote sandbox lifecycle, and a general resource reaper.
- **BlindBench #354:** new run-level evidence schema/endpoint, UI/storage changes, and migration beyond backward-compatible `eval-record` v1.
- Fireworks, SFT/DPO export, training approval, paid model comparisons, and all training pipelines.
- Multi-attempt orchestration, Harbor retries, pass@k, resume, concurrency above 1, generalized sandbox interfaces, customer/private fixtures, and production quality claims.
