#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CONTEXT="${ROOT}/runtime/daytona"
IMAGE=${1:-mogil-bench-daytona-runtime:local}

# This context is intentionally closed: no repository source, fixtures, auth files,
# or credentials can become build inputs.
actual=$(find "${CONTEXT}" -mindepth 1 -maxdepth 1 -type f -printf '%f\n' | LC_ALL=C sort)
expected=$(printf '%s\n' .dockerignore Dockerfile package-lock.json package.json | LC_ALL=C sort)
test "${actual}" = "${expected}"
test "$(find "${CONTEXT}" -mindepth 1 -maxdepth 1 ! -type f -print -quit)" = ""

docker build --pull=false --tag "${IMAGE}" --file "${CONTEXT}/Dockerfile" "${CONTEXT}"
printf 'built %s (local only; not pushed)\n' "${IMAGE}"
