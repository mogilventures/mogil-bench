import runpy
import sys
from pathlib import Path

CANARY = "__CANARY__"
assert CANARY.startswith("HIDDEN_VERIFIER_CANARY_")
summarize = runpy.run_path(Path(sys.argv[1]) / "inventory.py")["summarize"]
rows = [
    {"warehouse": "moon", "quantity": 2},
    {"warehouse": "mars", "quantity": 1},
    {"warehouse": "moon", "quantity": 3},
]
assert summarize(rows) == {"mars": 1, "moon": 5}
try:
    summarize([{"warehouse": "moon", "quantity": -1}])
except ValueError:
    pass
else:
    raise AssertionError("negative quantity accepted")
print("passed")
