#!/usr/bin/env python3
"""
Ivy's regression tests.

    python -m tests.run            offline: free, about a minute, can't touch your Mac
    python -m tests.run --live     also asks the real model (uses Groq quota)
    python -m tests.run --gui      also drives the real app (a window flashes on screen)
"""

import os
import sys
import unittest

if __name__ == "__main__":
    if "--live" in sys.argv:
        os.environ["IVY_TEST_LIVE"] = "1"
    if "--gui" in sys.argv:
        os.environ["IVY_TEST_GUI"] = "1"
    from tests import support
    support.install()
    suite = unittest.defaultTestLoader.discover("tests", pattern="test_*.py", top_level_dir=str(support.ROOT))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
