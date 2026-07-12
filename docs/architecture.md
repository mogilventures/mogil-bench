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
 deterministic mock   guarded command   trusted host Pi   Harbor 0.18 sandbox
       |               |                  |                 |
       legacy per-result JSON             |       Mogil evidence bundle
                         \_________________|________________/
                                           |
                              immutable run manifest
                                           |
                         BlindBench v1 JSON + JSONL
```

`models.py` is the untrusted-input boundary for pack and export shapes. `packs.py` resolves fixtures within the pack directory and builds canonical SHA-256 fingerprints. `runner.py` preserves the three legacy adapters and dispatches Harbor tasks only after provider-specific preflight. `harbor_tasks.py` creates isolated agent/verifier contexts and translates provider-neutral policy into Harbor 0.18 configuration; `harbor_backend.py` owns the exact Harbor API boundary, one-attempt execution, ingestion, effective-policy validation, and cleanup confirmation. `harbor_daytona.py` is a tightly local Harbor environment adapter for labels, confirmed deletion, and bounded reaping; Daytona SDK values never cross that adapter. `run_bundle.py` owns safe copying, manifests, patches, checksums, and conservative evidence classification. `artifacts.py` projects Mogil-owned data—not live Harbor or Daytona SDK objects or paths—to backward-compatible `eval-record` v1. `cli.py` remains a thin Typer shell.

## Identity and leakage controls

A pack fingerprint hashes canonical JSON of the validated pack plus every fixture file's bytes. A result ID hashes that fingerprint and the canonical task/configuration definitions; wall-clock time is excluded. Harness name/version/SDK, provider, and model remain explicit in private analysis records. The run-evidence reviewer projection deliberately omits provider/model provenance.

Representative fixtures should be recent, self-contained, and behaviorally verifiable. Private PR-derived fixtures can reduce public benchmark leakage, but v1 does not mine PRs. Before creating such a fixture, copy only the minimum sanitized snapshot into a pack, remove `.git`, do not expose repository history to the runner, keep held-out checks outside reviewer-visible prompt/output, and update the revision. The shipped fixtures are fictional/public.

Verifier commands may determine `verification_passed`; their argv and expected text are not placed in BlindBench exports. For Harbor, `instruction.md`, the agent image/config/log, candidate workspace evidence, and default BlindBench export exclude hidden verifier code and canaries. Hidden code exists only in the verifier build context. Raw retained verifier logs are evidence and are not exported by default. The pack itself remains benchmark-author-only material when it contains held-out checks.

Harbor records keep the stable content-derived logical ID as the BlindBench record ID. Every execution gets a unique attempt ID used by the Harbor job and bundle path. Reviewer-safe metadata includes only the attempt ID, relative bundle reference, evidence state, and separate agent/verifier/infrastructure outcomes. Absolute paths, hidden argv/expectations, canaries, container/project identifiers, unrestricted verifier logs, and Harbor SDK payloads are excluded.

## Harbor compatibility and evidence boundary

The compatibility envelope is Python `>=3.12,<3.13`, `harbor==0.18.0`, optional Daytona SDK `0.196.0`, and container Pi `0.80.6`. Harbor 0.18's built-in installer requests an unpublished `@mariozechner/...@0.80.6`; `MogilPi0806` is a narrow subclass that changes only installation, using npm alias syntax to resolve the maintained `@earendil-works` 0.80.6 distribution under the reviewed package name. Harbor still owns orchestration, isolated environment execution, provider environment forwarding, Pi invocation, and raw JSONL capture; no host executable or auth file enters the container. A pin change is a dedicated compatibility change requiring translation, ingestion, cleanup, and real Docker reruns. Harbor objects are converted immediately to Mogil-owned summaries; the retained `harbor/` files are raw evidence, not public schemas.

A Harbor attempt uses one task, one configuration, one attempt, concurrency 1, retries 0, an explicitly selected Docker or Daytona environment, `delete: true`, and no user mounts. The agent image is built only from `environment/`; the hidden verifier image is built only from `tests/`, runs separately with `no-network`, and receives the explicitly collected `/workspace`. Workspace before/after manifests, mode/add/delete changes, deterministic patch, bounded verifier streams, rich verification output, Harbor configs/locks/result/log, raw agent bytes, artifact manifest, and cleanup inspection are copied before Harbor temporary files disappear.

The bundle state machine is deliberately conservative: legacy paths are `non_quality`; incomplete/corrupt Harbor evidence or unconfirmed cleanup is `insufficient`; the deterministic credential-free fixture remains `fixture_complete`; and a real Pi run becomes `quality_eligible` only after finalized messages, tool lifecycle linkage, final output, termination, workspace/verifier evidence, checksums, and cleanup all validate. Reward cannot override completeness.

`trajectory.py` parses the actual Harbor 0.18 Pi JSONL boundary. Incremental `message_update` records are filtered by Harbor, so canonical state is derived only from retained finalized `message_end` and tool lifecycle events. Pi 0.80.6 `agent_settled` is required exactly once after `agent_end`; only then may Mogil append canonical termination. Assistant `stopReason` is retained, and exactly one final text response with `stopReason == "stop"` must occur after all tool results; tool-use commentary is never promoted. Pi `Date.now()` epoch-millisecond message timestamps are normalized to timezone-aware UTC ISO-8601; ISO strings and legacy epoch seconds are validated and normalized too. Invalid, inconsistent start/end, or non-monotonic timestamps fail closed, and timestamp remains absent only for source events that lack one. Every canonical event has a content-derived stable ID and contiguous sequence. Raw bytes are retained separately. Unsupported, malformed, non-newline-terminated, lifecycle-incomplete, unlinked, or final-output-free streams fail closed.

`evidence.py` owns strict `mogil.harbor-evidence` v1.0 JSON/JSONL. The private envelope contains analysis provenance while its reviewer projection contains only blinded environment/harness classes, ordered redacted events, separate process/verifier/infrastructure/completeness outcomes, and bounded patch/verifier evidence with hashes. Reviewer-inline references require raw `sha256` plus `reviewer_sha256`; the latter binds the exact sanitized UTF-8 string or canonical changed-files JSON and is recomputed during producer validation without any redaction-marker bypass. Raw paths and hashes remain private at the consumer projection boundary. `quality_eligible` is schema-bound to successful complete outcomes, full rewards, completed termination, complete reviewer integrity, and canonical nonnegative UTC run chronology. Final evidence JSON, JSONL, run state, and checksum manifest use same-directory atomic replacement; checksums are written after all final bytes and validated before the bundle is returned for publication. The public upload seam accepts only exact `/ingest/v1/eval-runs` HTTPS endpoints or literal HTTP loopback, authenticates with a project Automation bearer token, and requires exact complete counts without logging sensitive bodies.

Cleanup confirmation is provider-specific behind a canonical result. Docker queries exact Compose project labels derived from Harbor 0.18's observed session naming. Daytona's Harbor environment adapter labels both agent and verifier sandboxes with the attempt and expiry, deletes in Harbor's `finally` paths, confirms each ID is absent with bounded exponential polling, and writes private receipts. Provider auto-deletion/already-not-found is successful absence. Exactly two confirmed receipts with the expected role-specific session IDs and unique matching sandbox IDs are required. Query errors, missing receipts, remaining resources, or missing/weaker provider-returned effective policy force infrastructure failure and insufficient evidence. The bounded reaper lists only Mogil-managed labels, then freshly fetches and revalidates both managed labels and expiry immediately before deletion. Already-absent and concurrent deletion are idempotent success; changed labels or renewed expiry are skipped. It caps scans/deletions and confirms absence.

Daytona uses Harbor 0.18's direct prebuilt-image path. The immutable digest-pinned image must contain Pi 0.80.6; candidate and hidden-verifier contexts are uploaded separately. CPU and memory use Daytona-supported `request` enforcement, disk is explicit, agent networking is a non-empty allowlist, and verifier networking is `no-network`. Harbor 0.18.0's direct Daytona adapter maps these policies into Daytona sandbox-create API fields (`domain_allow_list`/`network_allow_list` for allowlists and `network_block_all: true` for no-network); provider capability tests assert those actual create parameters rather than trusting the pack request alone. After creation, the custom Harbor adapter calls Daytona's provider-backed `refresh_data()` and records bounded returned CPU, RAM, disk, network, labels, and sandbox identity for both roles. Each sandbox must also prove Python 3.12 at `/usr/local/bin/python` and `/bin/sh`; the agent install phase accepts the preinstalled Pi runtime only when `pi --version` exits zero and emits exactly `0.80.6`. The backend requires requested-policy compatibility, exact agent allowlist, provider-returned verifier no-network, matching cleanup identities, and create-parameter confirmation that secrets are attached only to the agent. A missing provider field is explicitly unverified and cannot become effective evidence; Harbor's serialized lock is retained as raw input evidence but is never projected as provider-effective state. Only Daytona organization Secret references are accepted. The shared Harbor environment config is stripped of model secrets when Harbor constructs the verifier sandbox. Public reviewer evidence reports only `isolated-sandbox`; provider and raw resource identifiers remain private.

## Safety boundaries

The command adapter requires pack opt-in plus operator acknowledgement, runs no shell, copies fixtures to a temporary directory, sanitizes environment variables, enforces timeout and output bounds, and denies known dangerous executable names.

The trusted host Pi adapter remains backward compatible and has an independent pack/operator gate. It invokes the configured provider/model in ephemeral print mode from the fixture workdir, permits only Pi's read/write/edit built-ins, disables discovered extensions/skills/templates/context and project approval, and accepts an executable override only as an explicit absolute executable path. Harbor configurations never route through this adapter.

Harbor preflight runs before final output creation and checks Python/Harbor versions, Docker executable/daemon, and all fixed security invariants. Fixture bytes are copied; the host repository, home, Docker socket, SSH/cloud configuration, credential files, and arbitrary paths are not mounted. Local Docker still shares the host kernel and is not VM-grade isolation. Real Pi provider access may require agent networking and credentials; the mandatory fixture uses neither.

Because local adapters do not expose authoritative tokenizer/provider billing data, exports omit token counts and cost rather than inserting word-count estimates or zero-cost claims. Run directories are created with overwrite refusal. Individual errors become statuses (`failed`, `denied`, `timed_out`, or `verification_failed`) so a matrix still yields reviewable partial results.

## Explicit deferrals

ATIF and OTLP projections remain deferred. Other cloud providers and generalized multi-provider lifecycle management remain deferred. BlindBench owns storage/UI activation behind the public run-evidence endpoint. Fireworks, SFT/DPO/training export, Harbor retries, pass@k, concurrency above one, and generalized sandbox abstractions are also out of scope.
