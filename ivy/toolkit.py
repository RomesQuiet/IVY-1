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
    confirm: str | None    # None = safe, "action" = ACTION_MODE, "shell" = CONFIRM_SHELL,
                           # "vision" = uploads a screenshot, needs CLOUD_VISION consent
                           # "task" = free, unless outside content may have prompted it
    ack: str | None        # short phrase spoken in voice mode before a slow tool runs
    timeout: float
    untrusted: bool = False   # returns outside content (web, screen, mail): data, not orders


TOOLS: dict[str, Tool] = {}

# Groq's free tier allows ~8k tokens/minute per model, and every tool schema is
# re-sent on every call. So each request gets the small core set plus the groups
# its wording points at; the model can unlock any other group with use_tools.
GROUPS = {
    "media":    ("volume, pause/next/previous, what's playing, list YouTube results",
                 r"\b(volume|louder|quieter|mute|pause|resume|skip|next|previous|song|track|"
                 r"playing|music|spotify|youtube results|list videos)\b"),
    "web":      ("deep research, reading a web page's text",
                 r"\b(research|in depth|deep dive|read (this|the|that)|article|page|url|link|https?://)"),
    "system":   ("battery/disk, notifications, clipboard, screenshots, timers list/cancel, quit apps, "
                 "dark mode, Wi-Fi, brightness, lock screen, sleep/restart/shut down",
                 r"\b(battery|disk|storage|space|uptime|notif|clipboard|copy|paste|screenshot|screen ?shot|"
                 r"timers?|cancel|running|installed|apps|quit|close|dark mode|light mode|wi-?fi|brightness|"
                 r"lock|sleep|restart|reboot|shut ?down|log ?out)\b"),
    "files":    ("list, read, write, edit, find and search files",
                 r"\b(files?|folders?|director|desktop|downloads|documents|read |write|edit|code|"
                 r"project|repo|find|~/|/users/)"),
    "software": ("install, uninstall, search packages and apps",
                 r"\b(install|uninstall|remove|brew|homebrew|pip|npm|package|app store|update)\b"),
    "memory":   ("delete memories",
                 r"\b(forget|erase|wipe|undo)\b|\bdelete .{0,15}memor"),
    "life":     ("Messages/iMessage, Contacts, Mail (send, unread), Calendar (events, add), "
                 "Reminders, Notes",
                 r"\b(reply|message|imessage|text (him|her|them|my|to)|sms|contact|phone number|e-?mail|mail|inbox|"
                 r"calendar|schedule|agenda|event|meeting|appointment|remind|reminders?|to-?do|notes?)\b"),
    "control":  ("click things by their visible text, wait for text, click by name or position, "
                 "type, press keys, scroll, browser JavaScript",
                 r"\b(click|press|type|scroll|button|window|automate|safari|chrome|tab|javascript|"
                 r"keyboard|shortcut|minimi|maximi|fill|form|screen|menu|dialog|wait for)\b"),
    "browser":  ("Ivy's own browser: open sites, read their controls, click, type, fill forms",
                 r"\b(website|site|form|fill|log ?in|sign ?in|sign ?up|book|booking|reserve|checkout|"
                 r"cart|shop|order|apply|register|on the web|browser_)\b"),
    "tasks":    ("list or cancel background tasks",
                 r"\b(background|tasks?|working on|still running)\b"),
    "skills":   ("learn a new permanent ability (write a skill), list or remove skills",
                 r"\b(skill|learn|teach|new ability|from now on you can|build (a|an) tool|"
                 r"(tell|alert|notify|warn|let) me (know )?(when|if)|keep an eye|watch for|monitor)\b"),
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
         timeout: float = 30.0, untrusted: bool = False, **param_docs):
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
        TOOLS[fn.__name__] = Tool(fn.__name__, fn, schema, group, confirm, ack, timeout, untrusted)
        return fn
    return wrap


def register(name: str, fn, description: str, parameters: dict, group: str,
             confirm: str | None = "action", untrusted: bool = True, timeout: float = 60.0):
    """Add a tool whose schema already exists (used for MCP servers)."""
    schema = {"type": "function", "function": {
        "name": name, "description": description[:300], "parameters": parameters}}
    TOOLS[name] = Tool(name, fn, schema, group, confirm, None, timeout, untrusted)


_VERBS = {
    "web_search": "🔎 Searching the web", "deep_web_search": "🔬 Researching", "fetch_webpage": "📄 Reading",
    "get_weather": "🌤 Weather", "calculate": "🧮 Calculating", "open_app": "🚀 Opening",
    "open_url": "🌐 Opening", "browse": "🌐 Searching", "play_youtube": "▶️ YouTube",
    "search_youtube": "▶️ YouTube search", "play_music": "🎵 Playing", "media_control": "🎵",
    "set_volume": "🔊 Volume", "set_timer": "⏱ Timer", "remember": "🧠 Remembering",
    "recall": "🧠 Recalling", "forget": "🧠 Forgetting", "run_shell": "⌨️ Shell", "run_python": "🐍 Python",
    "applescript": "🍎 AppleScript", "send_imessage": "💬 Message", "send_email": "✉️ Email",
    "calendar_events": "📅 Calendar", "add_calendar_event": "📅 New event", "add_reminder": "⏰ Reminder",
    "create_note": "📝 Note", "create_skill": "🧩 Learning skill", "system_toggle": "⚙️",
    "read_screen": "👁 Reading the screen", "see_screen": "👁 Looking at the screen",
    "click_text": "🖱 Clicking", "wait_for_text": "⏳ Waiting for", "type_text": "⌨️ Typing",
    "press_key": "⌨️ Key", "read_file": "📄 Reading", "write_file": "📝 Writing", "edit_file": "📝 Editing",
    "find_files": "🔎 Finding files", "search_in_files": "🔎 Searching files",
    "browser_open": "🌐 Ivy's browser", "browser_read": "🌐 Reading the page",
    "browser_click": "🌐 Clicking", "browser_type": "🌐 Typing", "browser_select": "🌐 Choosing",
    "launch_program": "🚀 Starting", "start_task": "⏳ Background task", "list_tasks": "⏳ Tasks", "cancel_task": "⏳ Cancelling task",
    "read_context": "👁 Reading what's in front", "reply_email": "✉️ Reply",
    "install_package": "📦 Installing", "uninstall_package": "📦 Uninstalling", "use_tools": "🧰 Unlocking",
}


def describe_call(name: str, args: dict) -> str:
    """One readable line for a tool call, e.g. '▶️ YouTube · gotham chess'."""
    verb = _VERBS.get(name, "🛠 " + name.replace("_", " "))
    args = args or {}
    detail = str(args.get("path") or next((v for v in args.values() if isinstance(v, (str, int, float)) and str(v)), ""))
    if name in ("run_python", "applescript", "create_skill"):
        detail = (args.get("name") or "") if name == "create_skill" else ""
    detail = " ".join(detail.split())
    return f"{verb} · {detail[:80]}" if detail else verb


def groups_for(text: str) -> set[str]:
    """Which optional tool groups a user message is likely to need."""
    t = (text or "").lower()
    return {g for g, (_, pattern) in list(GROUPS.items()) if re.search(pattern, t)}


def schemas(groups: set[str] | None = None) -> list[dict]:
    """Schemas for core tools + the given groups (all tools if groups is None)."""
    if groups is None:
        return [t.schema for t in list(TOOLS.values())]
    out = [t.schema for t in list(TOOLS.values()) if t.group == "core" or t.group in groups]
    missing = [g for g in list(GROUPS) if g not in groups]
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
    props, required = params.get("properties", {}), params.get("required", [])
    clean = {}
    for k, v in (args or {}).items():
        if k not in props:
            continue
        want = props[k].get("type")
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
        return f"Unknown tool '{name}'. Use one listed in this request, or use_tools to unlock more."
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
