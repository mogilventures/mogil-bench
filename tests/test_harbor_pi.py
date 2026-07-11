from __future__ import annotations

import inspect

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
