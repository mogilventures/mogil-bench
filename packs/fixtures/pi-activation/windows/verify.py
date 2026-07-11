import runpy
import sys
from pathlib import Path

CANARY = "__CANARY__"
assert CANARY.startswith("HIDDEN_VERIFIER_CANARY_")
merge_windows = runpy.run_path(Path(sys.argv[1]) / "windows.py")["merge_windows"]
source = [(8, 9), (1, 3), (4, 6), (12, 12), (11, 11)]
original = list(source)
assert merge_windows(source) == [(1, 6), (8, 9), (11, 12)]
assert source == original
assert merge_windows([]) == []
print("passed")
