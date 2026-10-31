# ivy/watcher.py
"""
Proactive Ivy: she speaks up without being asked.

A background thread runs small rules on a schedule. A rule returns text to announce,
or None. None of this uses the LLM, so it costs no tokens. Skills can add rules too:

    from ivy.watcher import watch

    @watch(every=300)
    def bitcoin_drop() -> str | None:
        "Warn if Bitcoin falls under 60k."
        ...
        return "Bitcoin just fell under 60 thousand." or None
"""

import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config

_MAX_FAILURES = 3


@dataclass
class Rule:
    fn: Callable
    every: float
    next_run: float = 0.0
    failures: int = 0


_rules: dict[str, Rule] = {}
_started = False


def watch(every: float = 60):
    """Run the decorated function every `every` seconds; what it returns gets announced."""
    def wrap(fn):
        _rules[f"{fn.__module__}.{fn.__name__}"] = Rule(fn, every, time.time() + min(every, 15))
        return fn
    return wrap


def forget_module(module: str) -> list[str]:
    gone = [name for name in _rules if name.startswith(module + ".")]
    for name in gone:
        del _rules[name]
    return gone


def rule_names(module: str) -> list[str]:
    return [name.split(".")[-1] for name in _rules if name.startswith(module + ".")]


def status() -> list[str]:
    return [f"{n.split('.')[-1]}: every {r.every:.0f}s" + (" (stopped after errors)" if r.failures >= _MAX_FAILURES else "")
            for n, r in _rules.items()]


def in_quiet_hours(hour: int | None = None) -> bool:
    quiet = config.ALERTS.get("quiet_hours")
    if not quiet:
        return False
    start, end = quiet
    hour = time.localtime().tm_hour if hour is None else hour
    return start <= hour < end if start < end else hour >= start or hour < end


def run_due(on_alert, now: float | None = None):
    """Run every rule that's due. A rule that keeps failing is stopped, not retried forever."""
    now = time.time() if now is None else now
    for name, rule in list(_rules.items()):
        if rule.failures >= _MAX_FAILURES or now < rule.next_run:
            continue
        rule.next_run = now + rule.every
        try:
            message = rule.fn()
            rule.failures = 0
        except Exception as e:
            rule.failures += 1
            if rule.failures == _MAX_FAILURES:
                print(f"  ⚠️  alert rule {name.split('.')[-1]} stopped: {e}")
            continue
        if message:
            on_alert(message)


def start(on_alert):
    global _started
    if _started:
        return
    _started = True

    def loop():
        while True:
            run_due(on_alert)
            time.sleep(5)

    threading.Thread(target=loop, daemon=True, name="watcher").start()


# =====================================================================
# Built-in rules (settings: config.ALERTS)
# =====================================================================

_battery_warned: set[int] = set()


def battery_alert(pmset_output: str) -> str | None:
    m = re.search(r"(\d+)%;\s*(\w+)", pmset_output)
    if not m:
        return None
    level, state = int(m.group(1)), m.group(2).lower()
    if state != "discharging":
        _battery_warned.clear()
        return None
    crossed = [t for t in config.ALERTS.get("battery") or () if level <= t and t not in _battery_warned]
    if not crossed:
        return None
    _battery_warned.update(crossed)
    return f"Your battery is down to {level} percent."


@watch(every=60)
def _battery() -> str | None:
    return battery_alert(subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout)


_announced_events: dict[str, float] = {}     # title → start time last announced


def calendar_alerts(rows: str, lead_minutes: int) -> list[str]:
    """rows = 'seconds_until_start|title' per line → reminders not yet announced."""
    out = []
    for row in rows.splitlines():
        seconds, _, title = row.partition("|")
        if not seconds.lstrip("-").isdigit() or not title:
            continue
        seconds = int(seconds)
        start = time.time() + seconds
        repeat = abs(start - _announced_events.get(title, 0)) < 120     # same event, read again
        if 0 <= seconds <= lead_minutes * 60 and not repeat:
            _announced_events[title] = start
            minutes = max(1, round(seconds / 60))
            out.append(f"{title} starts in {minutes} minute{'s' if minutes != 1 else ''}.")
    return out


CALENDAR_SCRIPT = '''
set nowDate to current date
set limitDate to nowDate + ({minutes} * minutes)
set out to ""
tell application "Calendar"
    repeat with c in calendars
        repeat with e in (every event of c whose start date ≥ nowDate and start date ≤ limitDate)
            set out to out & (((start date of e) - nowDate) as integer) & "|" & (summary of e) & linefeed
        end repeat
    end repeat
end tell
return out
'''


@watch(every=120)
def _calendar() -> str | None:
    minutes = config.ALERTS.get("calendar_minutes")
    if not minutes:
        return None
    r = subprocess.run(["osascript", "-e", CALENDAR_SCRIPT.format(minutes=minutes)],
                       capture_output=True, text=True, timeout=45)
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[:200])
    return " ".join(calendar_alerts(r.stdout, minutes)) or None


_PARTIAL = (".crdownload", ".download", ".part", ".partial")
_partials: set[str] = set()


def finished_downloads(names: set[str]) -> list[str]:
    """Compare in-progress files with the last check → names of the ones that completed."""
    current = {n for n in names if n.endswith(_PARTIAL)}
    done = []
    for partial in _partials - current:
        final = partial.rsplit(".", 1)[0]
        if final in names:              # gone because it finished, not because it was cancelled
            done.append(final)
    _partials.clear()
    _partials.update(current)
    return done


@watch(every=15)
def _downloads() -> str | None:
    if not config.ALERTS.get("downloads"):
        return None
    done = finished_downloads({p.name for p in (Path.home() / "Downloads").iterdir()})
    if not done:
        return None
    return f"{done[0]} finished downloading." if len(done) == 1 else f"{len(done)} downloads finished."


_disk_warned_day = ""


@watch(every=1800)
def _disk() -> str | None:
    global _disk_warned_day
    limit = config.ALERTS.get("disk_gb")
    free = shutil.disk_usage("/").free / 1e9
    today = time.strftime("%Y-%m-%d")
    if not limit or free >= limit or _disk_warned_day == today:
        return None
    _disk_warned_day = today
    return f"You're running low on disk space, only {free:.0f} gigabytes left."
