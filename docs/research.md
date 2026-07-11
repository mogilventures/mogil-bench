# Research notes and v1 decisions

## Representative coding benchmarks

Databricks describes an internal benchmark built from recent, representative pull requests in a multi-million-line private codebase, with human-authored self-contained tasks, held-out behavioral tests, sealed Git history, and comparison of model/harness combinations. It emphasizes end-to-end cost per solved task over token price alone and shows that harness choice affects quality and cost.

Mogil Bench adopts explicit model+harness identity, pack/fixture revision hashes, behavioral verifier hooks, privacy-safe recent fixtures, no-history guidance, duration/token/cost fields, and partial matrix results. V1 deliberately does **not** mine pull requests or expose Git history.

Source: [Benchmarking Coding Agents on Databricks' Multi-Million-Line Codebase](https://www.databricks.com/blog/benchmarking-coding-agents-databricks-multi-million-line-codebase)

## Small agent harness prior art

[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) demonstrates the value of a small, composable harness and isolated task execution. Mogil Bench borrows the preference for narrow components and explicit environments, but does not vendor it or recreate an agent loop. V1 invokes deterministic mocks, explicitly acknowledged local argv commands, or an independently acknowledged one-shot Pi adapter.

Pi's official [coding-agent CLI documentation](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent) documents print mode, ephemeral sessions, provider/model selection, resource-disabling flags, tool allowlists, and context-file controls. The v1 adapter uses those controls directly instead of wrapping Pi in a shell or recreating its loop.

## Dataset / runner / scorer separation

[Vellum Evals](https://github.com/vellum-ai/evals) is useful prior art for separating datasets, runners, and scoring. Similar evaluation systems reinforce keeping inputs/configurations independent from execution and evaluation. Mogil Bench represents dataset inputs as packs/fixtures and execution as adapters, then delegates subjective scoring to blind humans in BlindBench. It adds neither an LLM-as-judge nor a heavyweight eval framework.

Additional authoritative contract used: BlindBench's local `docs/native-ingest.md` (read from the sibling repository during implementation) defines `eval-record` version `"1"`, batching, deduplication IDs, privacy classes, endpoint constraints, and counts-only responses. Mogil Bench does not modify BlindBench.

## Product decisions

- Keep `hermes-text` and `pi-coding` as explicit lanes but normalize both to one artifact shape.
- Ship fictional, credential-free fixtures and a deterministic mock for reproducible local use.
- Keep the command adapter opt-in twice and visibly document that subprocess guardrails are not a hardened sandbox.
- Export raw run data separately from reviewer artifacts; do not export verifier argv or expected values.
- Make stable identity content-based and timestamps observational.
- Omit tokens and cost when adapters lack authoritative provider usage; never substitute word counts or assumed zero cost.
- Invoke Pi narrowly in ephemeral print mode with explicit provider/model identity and discovered resources disabled; capture final output now and defer JSON trajectory normalization.
- Keep generic-command and Pi-agent opt-ins independent.
- Keep upload optional, dry-run by default, endpoint-constrained, and token-from-environment only.
