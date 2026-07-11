# Mogil Bench

Mogil Bench v1 is a local Python CLI for running versioned, real-work-like benchmark packs. It keeps Hermes/text and Pi/coding tasks distinct while exporting both as BlindBench `eval-record` v1 JSON and JSONL for blind human review.

## Install

Python 3.11+ is required.

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

A pack has `version`, stable `id`, explicit `revision`, metadata, tasks, and configurations. Tasks identify a `lane` (`hermes-text` or `pi-coding`), category, prompt and/or relative fixture, privacy class, timeout, and optional command/verifier. Configurations identify provider, model, harness name/version/SDK, and adapter (`mock`, `command`, or `pi`).

See [`packs/sample-v1.yaml`](packs/sample-v1.yaml), [`packs/command-smoke-v1.yaml`](packs/command-smoke-v1.yaml), and the non-quick-start [`packs/pi-template-v1.yaml`](packs/pi-template-v1.yaml). Fixture references cannot escape the pack directory. Update the pack revision whenever task intent changes; fixture bytes are also fingerprinted.

### Trusted local Pi adapter

The Pi template is deliberately not credential-free and contains placeholder provider/model/harness values. Review and copy it before use, then run explicitly:

```bash
mogil-bench run path/to/reviewed-pi-pack.yaml \
  --output-dir /tmp/mogil-pi-run --allow-agents
```

The adapter resolves `pi` from `PATH`, or an absolute executable path named `pi` from `MOGIL_BENCH_PI_EXECUTABLE` (primarily for controlled testing). It invokes Pi directly with `--print`, `--no-session`, the configuration's `--provider` and `--model`, only the `read,write,edit` built-in tools, and a fixed benchmark system prompt that directs work to the temporary directory. It disables extensions, skills, prompt templates, context files, project approval, update checks, and telemetry. Known provider API-key variables and an explicitly set `PI_CODING_AGENT_DIR` are passed without being logged.

V1 captures Pi's final stdout plus bounded stderr/status/duration; it does not normalize Pi's JSON event stream, tool-call trajectory, authoritative token usage, or provider cost. Those are follow-up work. Provider/model/harness fields in exported records are the same configuration values used for invocation.

## Command safety

Commands are deny-by-default and need **both** `allow_commands: true` in the pack and CLI `--allow-commands`. Pi runs use the separate `allow_agents: true` plus `--allow-agents` gate; one acknowledgement never enables the other. Execution uses argv arrays with `shell=False`, a fresh temporary work directory, fixture copies, a small environment allowlist, timeout, and bounded stdout/stderr. The generic command adapter still forbids Pi, Hermes, Git, common network clients, Docker, and kubectl. A failure, denial, timeout, or verifier failure is recorded and does not stop the rest of the matrix.

These are guardrails, not a hardened OS sandbox: an allowed interpreter can run arbitrary code, and Pi's read/write/edit tools are not filesystem-confined by the operating system. Only acknowledge trusted packs and run them in an external sandbox when stronger isolation is required. Production-grade untrusted execution is out of scope.

## BlindBench upload

Upload is dry-run by default and validates both artifact and endpoint without making a request:

```bash
mogil-bench artifact upload /tmp/mogil-sample-run/blindbench.json \
  --endpoint https://DEPLOYMENT.convex.site/ingest/v1/traces
```

A real upload additionally requires `BLINDBENCH_INGEST_TOKEN` and `--confirm`. Only HTTPS `*.convex.site/ingest/v1/traces` endpoints are accepted. The CLI never prints the token or record content and reports only response counts. Tests make no network calls.

Prompts and outputs are reviewer-visible and free text is not automatically scrubbed by BlindBench. Never benchmark secrets or customer data; set `privacy_class` accurately. Hidden verifier expectations are not exported.

## Development

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/mypy
```

See [architecture](docs/architecture.md) and [research](docs/research.md).
