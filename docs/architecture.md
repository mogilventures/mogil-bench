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
       |               |                  |
 deterministic mock   guarded command   trusted local Pi
       |               |                  |
       per-result raw JSON
              |
 immutable-ish manifest (refuses overwrite)
              |
 BlindBench batch JSON + JSONL
```

`models.py` is the untrusted-input boundary for pack and export shapes. `packs.py` resolves fixtures within the pack directory and builds canonical SHA-256 fingerprints. `runner.py` owns three narrow adapters and continues across individual failures. `artifacts.py` maps both lanes to `eval-record` v1, validates JSON/JSONL, and owns the optional guarded upload. `cli.py` is a thin Typer shell.

## Identity and leakage controls

A pack fingerprint hashes canonical JSON of the validated pack plus every fixture file's bytes. A result ID hashes that fingerprint and the canonical task/configuration definitions; wall-clock time is excluded. Harness name/version/SDK, provider, and model remain explicit in every record.

Representative fixtures should be recent, self-contained, and behaviorally verifiable. Private PR-derived fixtures can reduce public benchmark leakage, but v1 does not mine PRs. Before creating such a fixture, copy only the minimum sanitized snapshot into a pack, remove `.git`, do not expose repository history to the runner, keep held-out checks outside reviewer-visible prompt/output, and update the revision. The shipped fixtures are fictional/public.

Verifier commands may determine `verification_passed`; their argv and expected text are not placed in BlindBench exports. Raw files contain outcomes, not hidden expected values. The pack itself remains benchmark-author-only material when it contains held-out checks.

## Safety boundaries

The command adapter requires pack opt-in plus operator acknowledgement, runs no shell, copies fixtures to a temporary directory, sanitizes environment variables, enforces timeout and output bounds, and denies known dangerous executable names.

The Pi adapter has an independent pack/operator gate. It invokes the configured provider/model in ephemeral print mode from the fixture workdir, permits only Pi's read/write/edit built-ins, disables discovered extensions/skills/templates/context and project approval, and accepts an executable override only as an explicit absolute executable path. It passes only known provider credential variables and optional Pi config location; values are never logged. Pi can still access host paths through its tools, and provider inference requires network access. These controls limit accidents but are not process, filesystem, or network isolation; an external sandbox is required for untrusted packs.

Because local adapters do not expose authoritative tokenizer/provider billing data, exports omit token counts and cost rather than inserting word-count estimates or zero-cost claims. Run directories are created with overwrite refusal. Individual errors become statuses (`failed`, `denied`, `timed_out`, or `verification_failed`) so a matrix still yields reviewable partial results.

## Extension points

V1 Pi integration captures the final print-mode output only. Follow-up can consume Pi JSON events to normalize tool calls, tool results, authoritative token usage, and provider-reported cost without changing pack identity or the BlindBench contract. Stronger external isolation and explicit network policy are also follow-up. A future Hermes adapter should return the same result fields; a full autonomous orchestration loop remains intentionally absent.
