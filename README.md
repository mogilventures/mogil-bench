# Mogil Bench

Mogil Bench v1 is a local Python CLI for running versioned, real-work-like benchmark packs. It keeps Hermes/text and Pi/coding tasks distinct while exporting both as BlindBench `eval-record` v1 JSON and JSONL for blind human review.

## Install

Python 3.12.x and a running local Docker daemon are required for Harbor runs. Mogil Bench pins `harbor==0.18.0`; Harbor's container-side Pi adapter is pinned to `@mariozechner/pi-coding-agent@0.80.6`. Because the original npm scope does not publish 0.80.6 and the release moved to the `@earendil-works` scope, Mogil's narrow Harbor Pi subclass installs that exact 0.80.6 distribution through npm package-alias syntax while preserving Harbor orchestration, runtime behavior, raw JSONL capture, and the reviewed package/version boundary. It does not read or copy host auth files. The mock and guarded command examples do not require Docker.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

## Verified quick start

Run from the repository root:

```bash
mogil-bench pack list packs
mogil-bench pack validate packs/sample-v1.yaml
mogil-bench run packs/sample-v1.yaml --output-dir /tmp/mogil-sample-run
mogil-bench artifact validate /tmp/mogil-sample-run/blindbench.json
mogil-bench artifact validate /tmp/mogil-sample-run/blindbench.jsonl
mogil-bench export blindbench /tmp/mogil-sample-run
```

The sample uses a deterministic mock and needs no credentials. A deliberately guarded local command smoke test is separate:

```bash
mogil-bench pack validate packs/command-smoke-v1.yaml
mogil-bench run packs/command-smoke-v1.yaml \
  --output-dir /tmp/mogil-command-run --allow-commands
```

Remove an existing output directory before repeating these exact commands. Runs refuse to overwrite an existing directory.

Each run contains:

- `manifest.json`: pack identity/fingerprint and result index;
- `results/*.json`: one raw execution result per task/configuration pair;
- `blindbench.json`: `{ "records": [...] }` batch;
- `blindbench.jsonl`: one `eval-record` v1 object per line.

Record IDs are deterministic hashes of canonical task, configuration, pack revision, and fixture content. Timestamps do not affect IDs, so retries deduplicate in BlindBench. V1 records measured duration. It omits token counts and cost unless an adapter has authoritative values; mock word counts are not reported as tokens.

## Pack format

A pack has `version`, stable `id`, explicit `revision`, metadata, tasks, and configurations. Tasks identify a `lane` (`hermes-text` or `pi-coding`), category, prompt and/or relative fixture, privacy class, timeout, and optional command/verifier. Configurations identify provider, model, harness name/version/SDK, and adapter (`mock`, `command`, `pi`, or `harbor`). Phase 1 Harbor configurations are restricted to `pi-coding`, `backend: harbor`, `environment_type: docker`, and an empty mount list.

See [`packs/sample-v1.yaml`](packs/sample-v1.yaml), [`packs/command-smoke-v1.yaml`](packs/command-smoke-v1.yaml), and the non-quick-start [`packs/pi-template-v1.yaml`](packs/pi-template-v1.yaml). Fixture references cannot escape the pack directory. Update the pack revision whenever task intent changes; fixture bytes are also fingerprinted.

### Trusted local Pi adapter

The Pi template is deliberately not credential-free and contains placeholder provider/model/harness values. Review and copy it before use, then run explicitly:

```bash
mogil-bench run path/to/reviewed-pi-pack.yaml \
  --output-dir /tmp/mogil-pi-run --allow-agents
```

The adapter resolves `pi` from `PATH`, or an absolute executable path named `pi` from `MOGIL_BENCH_PI_EXECUTABLE` (primarily for controlled testing). It invokes Pi directly with `--print`, `--no-session`, the configuration's `--provider` and `--model`, only the `read,write,edit` built-in tools, and a fixed benchmark system prompt that directs work to the temporary directory. It disables extensions, skills, prompt templates, context files, project approval, update checks, and telemetry. Known provider API-key variables and an explicitly set `PI_CODING_AGENT_DIR` are passed without being logged.

The legacy host adapter still captures only final stdout. Harbor Pi runs instead retain `pi.txt` byte-for-byte and strictly normalize its JSONL as described below.

## Harbor Docker foundation

Harbor execution requires both `allow_agents: true` in the reviewed pack and operator `--allow-agents`. Preflight runs before the final output directory is created and requires Python 3.12, exactly Harbor 0.18.0, a `docker` executable, a reachable daemon, one task/configuration/attempt, concurrency `1`, retries `0`, `delete: true`, and no user mounts. Harbor configurations never invoke host Pi.

The agent and hidden verifier use separate Docker build contexts. Fixture bytes are copied into the agent image; the repository, home directory, Docker socket, SSH/cloud configuration, and credential files are never mounted. The verifier runs in a separate `no-network` container and receives only the allowlisted `/workspace` artifact. Real provider-backed Pi may use public agent networking, but the deterministic fixture agent uses no network or credentials.

Local Docker shares the host kernel and is **not** a VM-grade trust boundary. Run only reviewed packs on a suitably isolated machine.

Each Harbor attempt is retained under `results/<logical-run-id>/<attempt-id>/`:

```text
run.json                 environment.json          cleanup.json
checksums.sha256
harbor/{job-config,job-lock,trial-config,trial-lock,trial-result}.json
harbor/trial.log         agent/pi.txt
workspace/{before-manifest,after-manifest,changed-files}.json
workspace/patch.diff
verifier/{verification,reward}.json
verifier/{stdout,stderr}.txt
artifacts/harbor-manifest.json
```

`agent/pi.txt` is preserved byte-for-byte. Harbor 0.18.0 filters incremental `message_update` records, but retains finalized `message_end`, tool lifecycle, `agent_end`, and Pi 0.80.6's final `agent_settled` record. Mogil parses only that pinned shape, requires a newline-terminated JSON object on every line, validates message/tool/lifecycle linkage, requires exactly one in-order `agent_settled` after `agent_end`, and fails closed if a stream is malformed, truncated, unsupported, missing final output, or incomplete. Pi numeric message timestamps are epoch milliseconds (`Date.now()`); they are normalized to timezone-aware UTC ISO-8601 strings. ISO string timestamps are parsed and normalized as UTC, epoch seconds remain accepted for older emitters, and invalid or non-monotonic timestamps fail closed. Event timestamps are omitted only when the corresponding retained Pi event genuinely has none. Assistant `stopReason` is preserved. Exactly one terminal assistant text response with `stopReason: "stop"` must follow all linked tool evidence; pre-tool commentary and `toolUse` responses can never become final output. Stable canonical events explicitly distinguish messages, reasoning, tool calls/results/errors, final output, and termination. Raw bytes remain separate and hashed.

A complete real Pi attempt additionally writes `mogil.harbor-evidence.json` and `.jsonl`. Both use strict schema `mogil.harbor-evidence` version `1.0`; JSONL contains one complete run per line. The private envelope retains analysis-only provider/model metadata. Its `reviewer` projection omits provenance and redacts credentials, host/workspace paths, verifier canaries, and hidden verifier details. Patch and verifier streams are bounded and carry integrity references. Every reviewer-inline reference requires both `sha256` for the immutable retained raw artifact and `reviewer_sha256` for the exact sanitized inline UTF-8 value. Patch/stdout/stderr hash their exact strings; changed files hash canonical JSON (`sort_keys`, compact separators, UTF-8). Consumers must verify `reviewer_sha256` even when inline content contains `[REDACTED]` or path-redaction markers, while keeping raw `sha256` values and paths private from guests.

Evidence states are:

- `non_quality`: mock, command, and legacy final-answer-only evidence;
- `insufficient`: any Harbor attempt with missing/corrupt evidence, failed infrastructure, failed/unconfirmed cleanup, or real Pi evidence pending #6;
- `fixture_complete`: only the credential-free deterministic fixture with complete artifacts, passing isolated verifier, integrity checks, and confirmed cleanup;
- `quality_eligible`: only a real Pi run with successful complete outcomes, all verifier rewards equal to 1, complete linked events, one terminal `stop` final output, completed termination, canonical chronological run timestamps, trusted workspace/reviewer evidence, valid hashes, and confirmed cleanup.

A reward of 1 never overrides trajectory, evidence, integrity, or cleanup failure.

Run the mandatory real integration check from the repository root:

```bash
python --version
# Python 3.12.x

docker info
pytest -q -m docker_smoke tests/test_harbor_docker_smoke.py
```

A skip is not accepted. The test uses no model/provider credentials, fixes the known calculator fixture, verifies it in a separate no-network container, validates the retained bundle and BlindBench v1 export, and confirms Docker cleanup.

If `cleanup.json` reports a leaked `compose_project_labels` value, inspect and remove only that exact project:

```bash
docker ps -a --filter 'label=com.docker.compose.project=<exact-label>'
docker rm -f $(docker ps -aq --filter 'label=com.docker.compose.project=<exact-label>')
```

## Command safety

Commands are deny-by-default and need **both** `allow_commands: true` in the pack and CLI `--allow-commands`. Pi runs use the separate `allow_agents: true` plus `--allow-agents` gate; one acknowledgement never enables the other. Execution uses argv arrays with `shell=False`, a fresh temporary work directory, fixture copies, a small environment allowlist, timeout, and bounded stdout/stderr. The generic command adapter still forbids Pi, Hermes, Git, common network clients, Docker, and kubectl. A failure, denial, timeout, or verifier failure is recorded and does not stop the rest of the matrix.

These are guardrails, not a hardened OS sandbox: an allowed interpreter can run arbitrary code, and Pi's read/write/edit tools are not filesystem-confined by the operating system. Only acknowledge trusted packs and run them in an external sandbox when stronger isolation is required. Production-grade untrusted execution is out of scope.

## Pi activation pack

[`packs/pi-activation-v1.yaml`](packs/pi-activation-v1.yaml) contains three short fictional/public coding tasks with deterministic hidden verifiers and no customer data. The activation model is `anthropic/claude-sonnet-4-6`; provider/model provenance remains private and is omitted from the reviewer projection. One Harbor configuration executes the three tasks as three independent attempts:

```bash
mogil-bench pack validate packs/pi-activation-v1.yaml
mogil-bench run packs/pi-activation-v1.yaml \
  --output-dir /tmp/mogil-pi-activation --allow-agents
```

Real execution requires a credential supported by Harbor's Pi adapter. Keep verifier sources private; they are copied only into the separate no-network verifier context.

## BlindBench upload

Upload is dry-run by default and validates both artifact and endpoint without making a request:

```bash
mogil-bench artifact upload /tmp/mogil-sample-run/blindbench.json \
  --endpoint https://DEPLOYMENT.convex.site/ingest/v1/traces
```

A real upload additionally requires `BLINDBENCH_INGEST_TOKEN` and `--confirm`. Only HTTPS `*.convex.site/ingest/v1/traces` endpoints are accepted. The CLI never prints the token or record content and reports only response counts. It treats `invalid > 0`, `truncated: true`, a malformed counts response, or an imported-plus-deduped count that differs from the intended batch size as an upload failure. Tests make no network calls.

Prompts and outputs are reviewer-visible and free text is not automatically scrubbed by legacy BlindBench exports. Never benchmark secrets or customer data; set `privacy_class` accurately. Hidden verifier expectations are not exported.

### Run-evidence upload

Validate strict run evidence locally:

```bash
mogil-bench evidence validate /tmp/mogil-pi-activation/results/RUN/ATTEMPT/mogil.harbor-evidence.json
mogil-bench evidence validate /tmp/mogil-pi-activation/results/RUN/ATTEMPT/mogil.harbor-evidence.jsonl
```

Upload is dry-run by default. The public endpoint must be HTTPS (HTTP is accepted only for literal loopback development), have no URL credentials/query/fragment, and use exactly `/ingest/v1/eval-runs`:

```bash
mogil-bench evidence upload EVIDENCE.json \
  --endpoint https://blindbench.example/ingest/v1/eval-runs
BLINDBENCH_AUTOMATION_TOKEN='project-token' mogil-bench evidence upload EVIDENCE.json \
  --endpoint https://blindbench.example/ingest/v1/eval-runs --confirm
```

The request body is a bounded batch of complete authoritative Pydantic artifacts, not reviewer projections or legacy trace records:

```json
{
  "runs": [
    {
      "schema": "mogil.harbor-evidence",
      "version": "1.0",
      "run": { "id": "mogil-run-id", "attempt": "attempt-id" },
      "...": "remaining strict artifact fields"
    }
  ]
}
```

A successful consumer response uses exactly these completion counters (additional response metadata is ignored):

```json
{
  "complete": 3,
  "imported": 2,
  "deduped": 1,
  "invalid": 0
}
```

`complete` must equal the submitted `runs` count, `imported + deduped` must equal that same count, and `invalid` must be zero. A conflict or partial batch must not report a complete count. The token is a project Automation token and is never printed. Errors disclose only exception classes, never token, response body, prompts, or outputs.

## Explicit deferrals

This implementation does not add ATIF/OTLP projections, #8 Daytona/remote sandbox lifecycle, BlindBench storage/UI changes, Fireworks/training export, retries, pass@k, or concurrency above one.

## Development

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src
```

See [architecture](docs/architecture.md) and [research](docs/research.md).
