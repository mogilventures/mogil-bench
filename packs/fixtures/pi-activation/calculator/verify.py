import runpy
import sys
from pathlib import Path

CANARY = "__CANARY__"
assert CANARY.startswith("HIDDEN_VERIFIER_CANARY_")
calculate_total = runpy.run_path(Path(sys.argv[1]) / "calculator.py")["calculate_total"]
assert calculate_total(7, 5) == 12
assert calculate_total(7, -2) == 5
assert calculate_total(0, 0) == 0
print("passed")
