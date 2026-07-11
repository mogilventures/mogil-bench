# __CANARY__
import sys
from pathlib import Path

workspace = Path(sys.argv[1])
namespace: dict[str, object] = {}
exec((workspace / "calculator.py").read_text(encoding="utf-8"), namespace)
assert namespace["add"](2, 3) == 5
print("fixture passed")
