# ivy/toolkit.py
"""
Tool registry for IVY-1.

Write a normal typed function with a docstring and decorate it with @tool.
The JSON schema the model sees is generated from the signature, so the
function and its schema can never drift apart.

    @tool(ack="Checking the weather.", location="City name, e.g. 'Lisbon'")
    def get_weather(location: str) -> str:
        "Current weather for a city."
"""

import inspect
import re
import typing
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _Timeout
from dataclasses import dataclass

MAX_RESULT_CHARS = 4000

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}
_CASTS = {"integer": int, "number": float, "string": str}


@dataclass
class Tool:
    name: str
    fn: typing.Callable
    schema: dict
    group: str             # only "core" tools are always sent; others load on demand
    confirm: str | None    # None = safe, "action" = follows ACTION_MODE, "shell" = CONFIRM_SHELL
    ack: str | None        # short phrase spoken in voice mode before a slow tool runs
    timeout: float


TOOLS: dict[str, Tool] = {}

# Groq's free tier allows ~8k tokens/minute per model, and every tool schema is
# re-sent on every call. So each request gets the small core set plus the groups
# its wording points at; the model can unlock any other group with use_tools.
GROUPS = {
    "web":      ("deep research, reading web pages",
                 r"\b(research|in depth|deep|read (this|the|that)|article|page|url|link|website|https?://)"),
    "system":   ("battery/disk, notifications, clipboard, screenshots, timers list/cancel, running/installed apps",
                 r"\b(battery|disk|storage|uptime|notif|clipboard|copy|paste|screenshot|screen ?shot|"
                 r"timers?|cancel|running|installed|apps|switch to|bring up|focus)\b"),
    "files":    ("shell commands, list and read files",
                 r"\b(shell|terminal|command|run |script|files?|folders?|director|desktop|downloads|"
                 r"documents|read |git|python|process|kill|ls\b|cat\b|~/|/users/)"),
    "software": ("install, uninstall, search packages and apps",
                 r"\b(install|uninstall|remove|brew|homebrew|pip|npm|package|app store|update)\b"),
    "memory":   ("delete memories",
                 r"\b(forget|erase|wipe)\b|\bdelete .{0,15}memor"),
    "control":  ("AppleScript, click UI buttons, type, press keys, scroll, browser JavaScript, "
                 "Messages/Mail/Calendar/Reminders automation",
                 r"\b(click|press|type|scroll|button|window|applescript|automate|message|imessage|text |"
                 r"mail|email|calendar|reminder|event|meeting|safari|chrome|tab|javascript|setting|"
                 r"dark mode|wifi|bluetooth|do not disturb|minimi|maximi|close)\b"),
}


def _param_spec(annotation, doc: str | None) -> dict:
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        choices = list(typing.get_args(annotation))
        spec = {"type": _JSON_TYPES.get(type(choices[0]), "string"), "enum": choices}
    else:
        spec = {"type": _JSON_TYPES.get(annotation, "string")}
    if doc:
        spec["description"] = doc
    return spec


def tool(group: str = "core", confirm: str | None = None, ack: str | None = None,
         timeout: float = 30.0, **param_docs):
    """Register a function as a tool. Keyword args document its parameters."""
    def wrap(fn):
        sig = inspect.signature(fn)
        hints = typing.get_type_hints(fn)
        props, required = {}, []
        for name, p in sig.parameters.items():
            props[name] = _param_spec(hints.get(name, str), param_docs.get(name))
            if p.default is inspect.Parameter.empty:
                required.append(name)
            else:
                props[name]["default"] = p.default

        description = " ".join((inspect.getdoc(fn) or fn.__name__).split())
        schema = {
            "type": "function",
            "function": {
                "name": fn.__name__,
                "description": description,
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        }
        TOOLS[fn.__name__] = Tool(fn.__name__, fn, schema, group, confirm, ack, timeout)
        return fn
    return wrap


def groups_for(text: str) -> set[str]:
    """Which optional tool groups a user message is likely to need."""
    t = (text or "").lower()
    return {g for g, (_, pattern) in GROUPS.items() if re.search(pattern, t)}


def schemas(groups: set[str] | None = None) -> list[dict]:
    """Schemas for core tools + the given groups (all tools if groups is None)."""
    if groups is None:
        return [t.schema for t in TOOLS.values()]
    out = [t.schema for t in TOOLS.values() if t.group == "core" or t.group in groups]
    missing = [g for g in GROUPS if g not in groups]
    if missing:
        out.append(_use_tools_schema(missing))
    return out


def _use_tools_schema(missing: list[str]) -> dict:
    listing = "; ".join(f"{g} = {GROUPS[g][0]}" for g in missing)
    return {
        "type": "function",
        "function": {
            "name": "use_tools",
            "description": "Unlock more tools when none of the current ones fit. "
                           f"Groups: {listing}. Call it, then use the new tools.",
            "parameters": {
                "type": "object",
                "properties": {"groups": {"type": "string",
                                          "description": "Comma-separated group names"}},
                "required": ["groups"],
            },
        },
    }


def _clean_args(t: Tool, args: dict) -> tuple[dict, str | None]:
    """Drop unknown args, coerce simple types, report missing required ones."""
    params = t.schema["function"]["parameters"]
    props, required = params["properties"], params["required"]
    clean = {}
    for k, v in (args or {}).items():
        if k not in props:
            continue
        want = props[k]["type"]
        try:
            if want == "boolean" and isinstance(v, str):
                v = v.strip().lower() in ("true", "1", "yes")
            elif want in _CASTS and not isinstance(v, (bool, dict, list)):
                v = _CASTS[want](v)
        except (TypeError, ValueError):
            return {}, f"Argument '{k}' must be a {want}, got {v!r}."
        clean[k] = v
    missing = [r for r in required if r not in clean]
    if missing:
        return {}, f"Missing required argument(s): {', '.join(missing)}. Call {t.name} again with them."
    return clean, None


_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="tool")


def run(name: str, args: dict) -> str:
    """Execute a tool by name. Always returns a string; never raises."""
    t = TOOLS.get(name)
    if not t:
        return f"Unknown tool '{name}'. Available: {', '.join(sorted(TOOLS))}."
    clean, err = _clean_args(t, args)
    if err:
        return err
    future = _pool.submit(t.fn, **clean)
    try:
        result = future.result(timeout=t.timeout)
    except _Timeout:
        return f"{name} is still running after {int(t.timeout)}s — gave up waiting."
    except Exception as e:
        return f"{name} failed: {type(e).__name__}: {e}"
    text = "(done)" if result is None else str(result)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n…(truncated, {len(text)} chars total)"
    return text
