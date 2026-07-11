import runpy
import sys
from pathlib import Path

CANARY = "__CANARY__"
assert CANARY.startswith("HIDDEN_VERIFIER_CANARY_")
workspace = Path(sys.argv[1])
slugify = runpy.run_path(workspace / "slugify.py")["slugify"]
assert slugify("  Blue  Widget__XL ") == "blue-widget-xl"
assert slugify("already-clean") == "already-clean"
print("passed")
