from __future__ import annotations

from typing import override

from harbor.agents.installed.node_install import (  # type: ignore[import-untyped]
    nvm_node_install_snippet,
)
from harbor.agents.installed.pi import Pi  # type: ignore[import-untyped]
from harbor.environments.base import BaseEnvironment  # type: ignore[import-untyped]

from .harbor_backend import PI_VERSION

HARBOR_PI_IMPORT_PATH = "mogil_bench.harbor_pi:MogilPi0806"
# The original scope does not publish 0.80.6. npm alias syntax preserves Harbor's
# reviewed package name while resolving the maintained 0.80.6 distribution.
PI_NPM_INSTALL_SPEC = "@mariozechner/pi-coding-agent@npm:@earendil-works/pi-coding-agent@0.80.6"


class MogilPi0806(Pi):  # type: ignore[misc]
    """Harbor 0.18 Pi adapter with a narrow, exact 0.80.6 install boundary."""

    def __init__(self, *args: object, preinstalled: bool = False, **kwargs: object) -> None:
        self._preinstalled = preinstalled
        super().__init__(*args, **kwargs)

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        if self._version != PI_VERSION:
            raise RuntimeError(f"Mogil Pi adapter requires exact version {PI_VERSION}")
        if self._preinstalled:
            result = await self.exec_as_agent(environment, command="pi --version")
            stdout = result.stdout
            if result.return_code != 0:
                raise RuntimeError("preinstalled Pi version check failed")
            if not isinstance(stdout, str) or stdout not in {
                PI_VERSION,
                f"{PI_VERSION}\n",
            }:
                raise RuntimeError(
                    f"preinstalled Pi must report exact version {PI_VERSION}"
                )
            if len(stdout.splitlines()) != 1:
                raise RuntimeError("preinstalled Pi version output must be one exact line")
            return
        await self.exec_as_root(
            environment,
            command="apt-get update && apt-get install -y curl",
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                f"{nvm_node_install_snippet()} && "
                f"npm install -g {PI_NPM_INSTALL_SPEC} && "
                "pi --version"
            ),
        )
