# Architecture

## Ownership boundary

Mogil Bench owns local pack parsing, fixture execution, result capture, and artifact creation. BlindBench owns artifact import, blind human judgment, and result reuse. V1 has no hosted service, scheduler, direct Hermes orchestration, live action, or LLM judge.

## Flow

```text
versioned YAML pack + local fixtures
              |
       parse and validate
              |
 task x configuration matrix
       |               |                  |                 |
 deterministic mock   guarded command   trusted host Pi   Harbor 0.18/Docker
       |               |                  |                 |
       legacy per-result JSON             |       Mogil evidence bundle
                         \_________________|________________/
                                           |
                              immutable run manifest
                                           |
                         BlindBench v1 JSON + JSONL
```

`models.py` is the untrusted-input boundary for pack and export shapes. `packs.py` resolves fixtures within the pack directory and builds canonical SHA-256 fingerprints. `runner.py` preserves the three legacy adapters and dispatches the single Phase 1 Harbor task only after preflight. `harbor_tasks.py` creates isolated agent/verifier contexts; `harbor_backend.py` owns the exact Harbor 0.18 API boundary, one-attempt execution, ingestion, and cleanup confirmation. `run_bundle.py` owns safe copying, manifests, patches, checksums, and conservative evidence classification. `artifacts.py` projects Mogil-owned data—not live Harbor SDK objects or paths—to backward-compatible `eval-record` v1. `cli.py` remains a thin Typer shell.

## Identity and leakage controls

A pack fingerprint hashes canonical JSON of the validated pack plus every fixture file's bytes. A result ID hashes that fingerprint and the canonical task/configuration definitions; wall-clock time is excluded. Harness name/version/SDK, provider, and model remain explicit in every record.

Representative fixtures should be recent, self-contained, and behaviorally verifiable. Private PR-derived fixtures can reduce public benchmark leakage, but v1 does not mine PRs. Before creating such a fixture, copy only the minimum sanitized snapshot into a pack, remove `.git`, do not expose repository history to the runner, keep held-out checks outside reviewer-visible prompt/output, and update the revision. The shipped fixtures are fictional/public.

Verifier commands may determine `verification_passed`; their argv and expected text are not placed in BlindBench exports. For Harbor, `instruction.md`, the agent image/config/log, candidate workspace evidence, and default BlindBench export exclude hidden verifier code and canaries. Hidden code exists only in the verifier build context. Raw retained verifier logs are evidence and are not exported by default. The pack itself remains benchmark-author-only material when it contains held-out checks.

Harbor records keep the stable content-derived logical ID as the BlindBench record ID. Every execution gets a unique attempt ID used by the Harbor job and bundle path. Reviewer-safe metadata includes only the attempt ID, relative bundle reference, evidence state, and separate agent/verifier/infrastructure outcomes. Absolute paths, hidden argv/expectations, canaries, container/project identifiers, unrestricted verifier logs, and Harbor SDK payloads are excluded.

## Harbor compatibility and evidence boundary

The compatibility envelope is Python `>=3.12,<3.13`, `harbor==0.18.0`, and container Pi `0.80.6`. A pin change is a dedicated compatibility change requiring translation, ingestion, cleanup, and real Docker reruns. Harbor objects are converted immediately to Mogil-owned summaries; the retained `harbor/` files are raw evidence, not public schemas.

A Harbor attempt uses one task, one configuration, one attempt, concurrency 1, retries 0, local Docker, `delete: true`, and no user mounts. The agent image is built only from `environment/`; the hidden verifier image is built only from `tests/`, runs separately with `no-network`, and receives the explicitly collected `/workspace`. Workspace before/after manifests, mode/add/delete changes, deterministic patch, bounded verifier streams, rich verification output, Harbor configs/locks/result/log, raw agent bytes, artifact manifest, and cleanup inspection are copied before Harbor temporary files disappear.

The bundle state machine is deliberately conservative: legacy paths are `non_quality`; incomplete/corrupt Harbor evidence or unconfirmed cleanup is `insufficient`; only the deterministic credential-free fixture can become `fixture_complete`. Reward cannot override completeness. Real Pi remains `insufficient` because filtered Pi events are incomplete and #6 has not supplied an ATIF/quality gate.

Cleanup confirmation queries Docker using the exact Compose project labels derived from Harbor 0.18's observed session naming and records both session identifiers and labels. No remaining exact-label container is required for `confirmed`; query errors are `unknown`, and remaining containers are `failed`. Either state forces infrastructure failure.

## Safety boundaries

The command adapter requires pack opt-in plus operator acknowledgement, runs no shell, copies fixtures to a temporary directory, sanitizes environment variables, enforces timeout and output bounds, and denies known dangerous executable names.

The trusted host Pi adapter remains backward compatible and has an independent pack/operator gate. It invokes the configured provider/model in ephemeral print mode from the fixture workdir, permits only Pi's read/write/edit built-ins, disables discovered extensions/skills/templates/context and project approval, and accepts an executable override only as an explicit absolute executable path. Harbor configurations never route through this adapter.

Harbor preflight runs before final output creation and checks Python/Harbor versions, Docker executable/daemon, and all fixed security invariants. Fixture bytes are copied; the host repository, home, Docker socket, SSH/cloud configuration, credential files, and arbitrary paths are not mounted. Local Docker still shares the host kernel and is not VM-grade isolation. Real Pi provider access may require agent networking and credentials; the mandatory fixture uses neither.

Because local adapters do not expose authoritative tokenizer/provider billing data, exports omit token counts and cost rather than inserting word-count estimates or zero-cost claims. Run directories are created with overwrite refusal. Individual errors become statuses (`failed`, `denied`, `timed_out`, or `verification_failed`) so a matrix still yields reviewable partial results.

## Explicit deferrals

Issue #6 owns complete Pi event normalization, append-only trajectory/ATIF, credentialed real-Pi activation, and any `quality_eligible` gate. Issue #8 owns Daytona, remote sandbox lifecycle, and generalized cleanup/reaping. BlindBench #354 owns any run-level evidence schema or endpoint beyond `eval-record` v1. Fireworks, SFT/DPO/training export, multi-attempt orchestration, Harbor retries, pass@k, concurrency above one, and generalized sandbox abstractions are also out of scope.
