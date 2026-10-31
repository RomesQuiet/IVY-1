"""Prints where brew is found, starting from the bare PATH apps get from Finder."""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ivy import config  # noqa: E402,F401

print(shutil.which("brew") or "missing")
