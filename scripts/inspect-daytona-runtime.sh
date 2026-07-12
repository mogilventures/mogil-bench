#!/bin/sh
set -eu

IMAGE=${1:?usage: scripts/inspect-daytona-runtime.sh IMAGE_OR_DIGEST}

# Check the exact executable/runtime boundary Harbor 0.18.0's Daytona adapter uses.
docker run --rm --entrypoint /bin/sh "${IMAGE}" -ceu '
  test -x /usr/local/bin/python
  /usr/local/bin/python -c "import sys; assert sys.version_info[:2] == (3, 12); assert sys.executable == '\''/usr/local/bin/python'\''"
  test -x /bin/sh
  test "$(node --version)" = '\''v22.19.0'\''
  test "$(npm --version)" = '\''10.9.3'\''
  test "$(pi --version)" = '\''0.80.6'\''
  test "$(pi --version | wc -l)" -le 1
  /usr/local/bin/python -c "import subprocess; value = subprocess.check_output(['\''pi'\'', '\''--version'\'']); assert value in (b'\''0.80.6'\'', b'\''0.80.6\\n'\''), value"
  test ! -e /root/.npmrc
  test ! -e /root/.npm
  test ! -e /root/.ssh
  test ! -e /root/.config/gcloud
  test ! -e /root/.aws
'

# Image configuration and layer commands must not carry common credential material.
if docker image inspect "${IMAGE}" --format '{{json .Config.Env}}' \
  | grep -Eiq '(api[_-]?key|auth[_-]?token|password|secret=|credentials)'; then
  echo 'credential-like image environment entry detected' >&2
  exit 1
fi
if docker history --no-trunc "${IMAGE}" --format '{{.CreatedBy}}' \
  | grep -Eiq '(api[_-]?key|auth[_-]?token|password=|secret=|BEGIN [A-Z ]*PRIVATE KEY)'; then
  echo 'credential-like layer command detected' >&2
  exit 1
fi

printf 'inspected %s: Daytona runtime contract satisfied\n' "${IMAGE}"
