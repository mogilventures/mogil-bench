# Daytona runtime image

This repository defines the credential-free runtime image used by Mogil Bench's
**Harbor 0.18.0** Daytona adapter. The intended registry repository is
`ghcr.io/mogilventures/mogil-bench-daytona-runtime`.

The image contract is intentionally narrow:

- Python 3.12.11 is available as `/usr/local/bin/python`;
- `/bin/sh`, Node 22.19.0, and npm 10.9.3 are available;
- `pi --version` returns exactly `0.80.6` (optionally followed by one newline);
- Pi is installed with Mogil's reviewed npm alias,
  `@mariozechner/pi-coding-agent@npm:@earendil-works/pi-coding-agent@0.80.6`;
- both upstream image indexes, the Dockerfile frontend, and all named versions
  are pinned;
- npm's full production graph is integrity-pinned by `package-lock.json`; and
- the closed build context contains only the Docker definition and npm manifests;
  only the non-secret manifests are copied into the image.

No credentials are needed to build or inspect it. Never pass build secrets,
registry credentials, auth files, benchmark fixtures, or repository source as
build arguments or context content.

## Local build and inspection

From the repository root, with Docker available:

```sh
scripts/build-daytona-runtime.sh mogil-bench-daytona-runtime:local
scripts/inspect-daytona-runtime.sh mogil-bench-daytona-runtime:local
pytest -q tests/test_daytona_runtime_image.py
```

The build script does not log in, push, or otherwise publish. It first fails if
anything has been added to the minimal build context. The inspection script runs
the literal Pi version command, validates its exact bytes, checks the Python path
and minor version, exercises Node/npm and the shell, and scans image configuration
and layer commands for credential-like material.

## Reviewed publish workflow

Publishing is a deliberate operator action, not CI behavior. Start from a clean,
reviewed commit and choose a unique release tag (for example a date plus short
source revision). Authenticate Docker to GHCR outside the build; do not put the
token in an environment variable consumed by Dockerfile instructions or in the
build context.

Build and load each target platform locally first, then run the inspection script
against it. After review, publish a multi-platform manifest directly with Buildx:

```sh
REPOSITORY=ghcr.io/mogilventures/mogil-bench-daytona-runtime
RELEASE=2026-07-12-abcdef0

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --file runtime/daytona/Dockerfile \
  --tag "${REPOSITORY}:${RELEASE}" \
  --provenance=true --sbom=true --push \
  runtime/daytona

docker buildx imagetools inspect "${REPOSITORY}:${RELEASE}"
```

Copy the resulting manifest digest from the inspection output and form the only
supported runtime reference:

```text
ghcr.io/mogilventures/mogil-bench-daytona-runtime@sha256:<64-hex-manifest-digest>
```

Run `scripts/inspect-daytona-runtime.sh` against that digest (Docker will inspect
the current platform), then use the same immutable reference as
`TERMINAL_DAYTONA_IMAGE` or the pack's `environment_policy.image`. Tags are only
publication handles; never put a tag-only reference in a benchmark pack. Record
the digest in the release/operations record. Do not retag a mutable channel or
configure Daytona from one.

The image contains runtime prerequisites only. Model credentials remain Daytona
organization-secret references attached by the adapter to the agent sandbox;
they are neither build inputs nor image content. The same image can therefore be
used for Harbor's separately created no-network verifier sandbox without exposing
agent credentials.
