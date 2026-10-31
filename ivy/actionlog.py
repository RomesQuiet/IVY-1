# ivy/actionlog.py
"""
A permanent record of what Ivy did, one JSON object per line, so an incident can be read
back instead of reconstructed. It also holds timings:  python -m ivy.actionlog
"""

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from . import config

_SECRET = re.compile(r"(?i)\b(gsk_|sk-|csk-)[\w-]{8,}|((api[_-]?key|token|secret|password)\s*[=:]\s*)\S+")
_lock = threading.Lock()


def _redact(text: str) -> str:
    return _SECRET.sub(lambda m: (m.group(2) or "") + "[hidden]", text)


def record(kind: str, name: str, **fields):
    """Never raises: a full disk mustn't stop Ivy, or be mistaken for a tool failing."""
    entry = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "name": name, **fields}
    line = _redact(json.dumps(entry, ensure_ascii=False, default=str))
    try:
        path = Path(config.ACTION_LOG)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(line + "\n")
    except OSError:
        pass


def summary(lines=None) -> list[str]:
    """Where the time went, slowest total first."""
    if lines is None:
        lines = Path(config.ACTION_LOG).read_text().splitlines()
    totals: dict[str, list[float]] = {}
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if "ms" in e:
            totals.setdefault(f"{e['kind']} {e['name']}", []).append(e["ms"])
    rows = sorted(totals.items(), key=lambda kv: -sum(kv[1]))
    return [f"{name}: {len(ms)} calls, {sum(ms) / len(ms) / 1000:.1f}s average, {sum(ms) / 1000:.0f}s total"
            for name, ms in rows]


if __name__ == "__main__":
    print("\n".join(summary()) or "Nothing logged yet.", file=sys.stdout)
