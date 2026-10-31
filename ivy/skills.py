# ivy/skills.py
"""
Skills: tools that live as plain Python files in skills/ and load at startup.

Ivy can write new ones herself with create_skill, so anything she can't do yet
she can learn — check a website, call an API, automate an app — and keep.
A skill file is ordinary Python using the same @tool decorator as built-ins:

    from ivy.toolkit import tool

    @tool(city="City name")
    def sunrise(city: str) -> str:
        "Today's sunrise and sunset times for a city."
        ...
        return "Sunrise 07:42, sunset 18:21"
"""

import importlib.util
import re
import sys
import traceback

from . import config, watcher
from .toolkit import TOOLS, tool

_loaded: dict[str, list[str]] = {}    # skill name → tool names it registered


def _load(path) -> list[str]:
    before = set(TOOLS)
    name = f"ivy_skill_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    added = sorted(set(TOOLS) - before) + watcher.rule_names(name)
    _loaded[path.stem] = added
    return added


def load_all():
    config.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(config.SKILLS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            _load(path)
        except Exception as e:
            print(f"  ⚠️  skill {path.name} failed to load: {e}")


def _unload(name: str):
    for t in _loaded.pop(name, []):
        TOOLS.pop(t, None)
    watcher.forget_module(f"ivy_skill_{name}")
    sys.modules.pop(f"ivy_skill_{name}", None)


@tool(group="skills", confirm="action", timeout=60,
      name="snake_case file name, e.g. 'crypto_prices'",
      code="Full Python source defining one or more @tool functions (see description)")
def create_skill(name: str, code: str) -> str:
    """Teach Ivy a new permanent ability by writing a Python skill file. Use when no
    existing tool can do something and it's worth keeping. The code must start with
    `from ivy.toolkit import tool` and define functions decorated with @tool(...) that have
    type hints, a docstring, and return a short string. Keyword args to @tool describe
    parameters. Network: use `requests`. Risky actions: @tool(confirm="action").
    For "tell me when X" requests, instead use `from ivy.watcher import watch` and a function
    decorated @watch(every=seconds) that returns the text to announce, or None: it runs in the
    background from now on. Replaces an existing skill with the same name."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name):
        return "Skill name must be snake_case letters/digits, e.g. 'crypto_prices'."
    if "@tool" not in code and "@watch" not in code:
        return "The code has no @tool or @watch function — nothing for Ivy to use."
    path = config.SKILLS_DIR / f"{name}.py"
    config.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    previous = path.read_text() if path.exists() else None
    _unload(name)
    path.write_text(code)
    try:
        added = _load(path)
    except Exception:
        err = traceback.format_exc(limit=3)
        _unload(name)
        if previous is None:
            path.unlink()
        else:
            path.write_text(previous)
            _load(path)
        return f"The skill failed to load, nothing was saved. Fix and retry:\n{err[-1200:]}"
    if not added:
        return "Loaded, but it registered nothing — did you decorate the functions?"
    return f"Learned skill '{name}'. New tools available right now: {', '.join(added)}."


@tool(group="skills")
def list_skills() -> str:
    """List the skills Ivy has learned and the tools each provides."""
    if not _loaded:
        return f"No skills yet. They live in {config.SKILLS_DIR}."
    return "\n".join(f"{n}: {', '.join(tools) or '(no tools)'}" for n, tools in sorted(_loaded.items()))


@tool(group="skills", confirm="always")
def remove_skill(name: str) -> str:
    """Forget a learned skill. The file moves to the Trash, so it can be recovered."""
    path = config.SKILLS_DIR / f"{name}.py"
    if not path.exists():
        return f"No skill named '{name}'."
    from .tools import _as_str, _osa
    _osa(f'tell application "Finder" to delete POSIX file {_as_str(str(path))}')
    _unload(name)
    return f"Removed skill '{name}' (it's in the Trash)."
