"""Runs ivy.py behind the safety net."""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import support

support.install()
sys.argv = ["ivy.py"]
runpy.run_path(str(support.ROOT / "ivy.py"), run_name="__main__")
