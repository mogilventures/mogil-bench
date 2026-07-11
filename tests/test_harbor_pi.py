from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest
from harbor.environments.base import ExecResult

from mogil_bench.harbor_backend import PI_VERSION
from mogil_bench.harbor_pi import HARBOR_PI_IMPORT_PATH, PI_NPM_INSTALL_SPEC, MogilPi0806


def test_mogil_pi_adapter_preserves_0806_and_harbor_pi_runtime() -> None:
    assert PI_VERSION == "0.80.6"
    assert HARBOR_PI_IMPORT_PATH == "mogil_bench.harbor_pi:MogilPi0806"
    assert PI_NPM_INSTALL_SPEC == (
        "@mariozechner/pi-coding-agent@npm:@earendil-works/pi-coding-agent@0.80.6"
    )
    source = inspect.getsource(MogilPi0806.install)
    assert "PI_NPM_INSTALL_SPEC" in source
    assert "pi --version" in source
    assert "auth.json" not in source


class VersionEnvironment:
    def __init__(self, result: ExecResult) -> None:
        self.result = result

    async def exec(self, **_kwargs: Any) -> ExecResult:
        return self.result


def preinstalled_pi(tmp_path: Path) -> MogilPi0806:
    return MogilPi0806(
        logs_dir=tmp_path,
        version=PI_VERSION,
        preinstalled=True,
    )


def test_preinstalled_pi_requires_exact_0806_output(tmp_path: Path) -> None:
    agent = preinstalled_pi(tmp_path)
    environment = VersionEnvironment(ExecResult(stdout="0.80.6\n", stderr="", return_code=0))
    asyncio.run(agent.install(environment))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "result",
    [
        ExecResult(stdout="0.80.5\n", stderr="", return_code=0),
        ExecResult(stdout="", stderr="", return_code=0),
        ExecResult(stdout="pi 0.80.6\n", stderr="", return_code=0),
        ExecResult(stdout="0.80.6\nextra\n", stderr="", return_code=0),
        ExecResult(stdout="0.80.6\n", stderr="missing", return_code=127),
    ],
)
def test_preinstalled_pi_rejects_wrong_missing_or_failed_version(
    tmp_path: Path, result: ExecResult
) -> None:
    with pytest.raises(RuntimeError):
        asyncio.run(
            preinstalled_pi(tmp_path).install(VersionEnvironment(result))  # type: ignore[arg-type]
        )
