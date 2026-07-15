# Security Policy

## Supported versions

Mogil Bench is a public alpha. Security fixes are applied to the latest code on `main`; no older release line is currently supported.

## Reporting a vulnerability

Do **not** open a public issue with vulnerability details, credentials, customer data, or benchmark evidence. Use the repository's **Security** tab to submit a private vulnerability report. If private reporting is unavailable, contact the repository owner, [@nmogil](https://github.com/nmogil), privately and ask for a secure reporting channel before sharing details.

Include the affected version/commit, impact, minimal reproduction, and any suggested mitigation. Remove all live credentials and private evidence from the report. Maintainers will acknowledge receipt, coordinate validation and remediation privately, and publish an advisory when appropriate.

## Operational boundaries

Mogil Bench intentionally treats benchmark packs, provider credentials, retained trajectories, hidden verifiers, and upload destinations as sensitive boundaries. The local Docker backend is not a VM-grade isolation boundary. Follow the README's explicit execution gates and never place secret values in packs, command arguments, logs, fixtures, or repository files.
