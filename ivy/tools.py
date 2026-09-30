
# mae/tools.py
from .tools_mac import TOOL_SCHEMAS as MAC_SCHEMAS, TOOL_FUNCTIONS as MAC_FUNCS
import datetime
import math
import re
import subprocess
import time
from pathlib import Path

import requests

from .memory import get_store


# =====================================================================
# CORE
# =====================================================================

def get_current_time() -> str:
    return datetime.datetime.now().strftime("%A, %B %d, %Y — %H:%M:%S")


def calculate(expression: str) -> str:
    allowed = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
    allowed.update({"abs": abs, "round": round, "min": min, "max": max})
    try:
        return str(eval(expression, {"__builtins__": {}}, allowed))
    except Exception as e:
        return f"Calculation error: {e}"


# =====================================================================
# SEARCH
# =====================================================================

def search_wikipedia(query: str) -> str:
    try:
        r = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "format": "json",
                "srlimit": 1,
            },
            timeout=10,
        )
        results = r.json().get("query", {}).get("search", [])
        if not results:
            return "No Wikipedia result."
        title = results[0]["title"]
        snippet = re.sub(r"<[^>]+>", "", results[0]["snippet"])
        return f"{title}: {snippet}"
    except Exception as e:
        return f"Wikipedia error: {e}"


def web_search(query: str) -> str:
    wiki = search_wikipedia(query)
    if "No Wikipedia result" not in wiki and "error" not in wiki.lower():
        return wiki
    try:
        r = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=10,
        )
        data = r.json()
        if data.get("AbstractText"):
            return data["AbstractText"]
        if data.get("Answer"):
            return str(data["Answer"])
        related = [t for t in data.get("RelatedTopics", [])[:3] if isinstance(t, dict)]
        if related:
            return "\n".join(t.get("Text", "") for t in related)
        return "No result found."
    except Exception as e:
        return f"Search error: {e}"


def get_weather(location: str) -> str:
    try:
        r = requests.get(f"https://wttr.in/{location}?format=j1", timeout=10)
        r.raise_for_status()
        cur = r.json()["current_condition"][0]
        return (
            f"{location}: {cur['weatherDesc'][0]['value']}, "
            f"{cur['temp_C']}°C (feels {cur['FeelsLikeC']}°C), "
            f"humidity {cur['humidity']}%, wind {cur['windspeedKmph']} km/h"
        )
    except Exception as e:
        return f"Weather lookup failed: {e}"


# =====================================================================
# macOS SYSTEM
# =====================================================================

def open_app(name: str) -> str:
    try:
        subprocess.run(["open", "-a", name], check=True, timeout=10)
        return f"Opened {name}."
    except Exception as e:
        return f"Could not open {name}: {e}"


def open_url(url: str) -> str:
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        subprocess.run(["open", url], check=True, timeout=10)
        return f"Opened {url}."
    except Exception as e:
        return f"Could not open URL: {e}"


def get_system_info() -> str:
    try:
        battery = subprocess.run(
            ["pmset", "-g", "batt"], capture_output=True, text=True
        ).stdout.strip()
        uptime = subprocess.run(
            ["uptime"], capture_output=True, text=True
        ).stdout.strip()
        disk = subprocess.run(
            ["df", "-h", "/"], capture_output=True, text=True
        ).stdout.strip()
        return f"Battery:\n{battery}\n\nUptime:\n{uptime}\n\nDisk:\n{disk}"
    except Exception as e:
        return f"System info error: {e}"


def get_clipboard() -> str:
    try:
        out = subprocess.run(
            ["pbpaste"], capture_output=True, text=True
        ).stdout[:2000]
        return out or "(clipboard is empty)"
    except Exception as e:
        return f"Clipboard read error: {e}"


def set_clipboard(text: str) -> str:
    try:
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
        return "Copied to clipboard."
    except Exception as e:
        return f"Clipboard write error: {e}"


def take_screenshot() -> str:
    path = Path.home() / "Desktop" / f"mae_screenshot_{int(time.time())}.png"
    try:
        subprocess.run(["screencapture", "-x", str(path)], check=True)
        return f"Saved screenshot to {path}"
    except Exception as e:
        return f"Screenshot error: {e}"


# =====================================================================
# FILES & SHELL
# =====================================================================

def run_shell(command: str) -> str:
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30
        )
        out = (r.stdout or "") + (r.stderr or "")
        return out.strip()[:2000] or "(no output)"
    except Exception as e:
        return f"Shell error: {e}"


def list_files(path: str = ".") -> str:
    try:
        p = Path(path).expanduser()
        items = sorted(p.iterdir())[:50]
        return "\n".join(
            f"{'DIR ' if i.is_dir() else 'FILE'} {i.name}" for i in items
        ) or "(empty)"
    except Exception as e:
        return f"List error: {e}"


def read_file(path: str) -> str:
    try:
        return Path(path).expanduser().read_text()[:3000]
    except Exception as e:
        return f"Read error: {e}"


# =====================================================================
# HOMEBREW — install, search, list
# =====================================================================

def install_brew_package(package: str) -> str:
    if not package or not package.strip():
        return "No package name given."

    pkg = package.strip()

    r = subprocess.run(
        ["brew", "install", pkg],
        capture_output=True, text=True, timeout=600,
    )
    if r.returncode == 0:
        return f"Installed '{pkg}' via Homebrew formula.\n{r.stdout[-800:]}"

    r2 = subprocess.run(
        ["brew", "install", "--cask", pkg],
        capture_output=True, text=True, timeout=600,
    )
    if r2.returncode == 0:
        return f"Installed '{pkg}' via Homebrew cask.\n{r2.stdout[-800:]}"

    return (
        f"Could not install '{pkg}'.\n"
        f"formula stderr: {r.stderr[-400:]}\n"
        f"cask stderr:    {r2.stderr[-400:]}"
    )


def brew_search(query: str) -> str:
    try:
        r = subprocess.run(
            ["brew", "search", query],
            capture_output=True, text=True, timeout=30,
        )
        out = (r.stdout or r.stderr).strip()
        return out[:1500] or "(no results)"
    except Exception as e:
        return f"brew search error: {e}"


def list_installed_brew() -> str:
    try:
        f = subprocess.run(
            ["brew", "list", "--formula"], capture_output=True, text=True
        ).stdout.strip()
        c = subprocess.run(
            ["brew", "list", "--cask"], capture_output=True, text=True
        ).stdout.strip()
        return f"Formulae:\n{f or '(none)'}\n\nCasks:\n{c or '(none)'}"
    except Exception as e:
        return f"brew list error: {e}"

# =====================================================================
# PACKAGE MANAGERS — pip / npm / mas (Mac App Store)
# =====================================================================

def pip_install(package: str) -> str:
    """Install a Python package with pip (user-level)."""
    if not package.strip():
        return "No package given."
    r = subprocess.run(
        ["pip", "install", "--user", package.strip()],
        capture_output=True, text=True, timeout=600,
    )
    if r.returncode == 0:
        return f"Installed '{package}' with pip.\n{r.stdout[-600:]}"
    return f"pip install failed:\n{r.stderr[-600:]}"


def pip_uninstall(package: str) -> str:
    if not package.strip():
        return "No package given."
    r = subprocess.run(
        ["pip", "uninstall", "-y", package.strip()],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode == 0:
        return f"Uninstalled '{package}' with pip."
    return f"pip uninstall failed:\n{r.stderr[-600:]}"


def npm_install(package: str, global_: bool = True) -> str:
    if not package.strip():
        return "No package given."
    cmd = ["npm", "install"]
    if global_:
        cmd.append("-g")
    cmd.append(package.strip())
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode == 0:
        return f"Installed '{package}' with npm."
    return f"npm install failed:\n{r.stderr[-600:]}"


def npm_uninstall(package: str, global_: bool = True) -> str:
    if not package.strip():
        return "No package given."
    cmd = ["npm", "uninstall"]
    if global_:
        cmd.append("-g")
    cmd.append(package.strip())
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        return f"Uninstalled '{package}' with npm."
    return f"npm uninstall failed:\n{r.stderr[-600:]}"


def mas_install(app_name: str) -> str:
    """
    Open the Mac App Store to the given app's page (or search results).
    No CLI needed. User clicks Install in the App Store UI.
    """
    name = (app_name or "").strip()
    if not name:
        return "No app name given."

    # URL-encode the name for the search query
    from urllib.parse import quote
    url = f"macappstore://search.itunes.apple.com/WebObjects/MZSearch.woa/wa/search?q={quote(name)}"

    try:
        subprocess.run(["open", url], check=True, timeout=10)
        return (
            f"Opened the App Store to search for '{name}'. "
            f"Click Install there — I'll wait."
        )
    except Exception as e:
        return f"Could not open App Store: {e}"

# =====================================================================
# UNINSTALL — universal
# =====================================================================

def uninstall_brew_package(package: str) -> str:
    """Uninstall a Homebrew formula or cask."""
    if not package.strip():
        return "No package given."
    pkg = package.strip()

    # Try formula first
    r = subprocess.run(
        ["brew", "uninstall", pkg],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode == 0:
        return f"Uninstalled formula '{pkg}'."

    # Try cask
    r2 = subprocess.run(
        ["brew", "uninstall", "--cask", pkg],
        capture_output=True, text=True, timeout=300,
    )
    if r2.returncode == 0:
        return f"Uninstalled cask '{pkg}'."

    return (
        f"Could not uninstall '{pkg}'.\n"
        f"formula stderr: {r.stderr[-300:]}\n"
        f"cask stderr:    {r2.stderr[-300:]}"
    )


def uninstall_app(app_name: str) -> str:
    """
    Uninstall any .app from /Applications or ~/Applications.
    Moves it to the Trash (recoverable — safer than rm -rf).
    """
    if not app_name.strip():
        return "No app name given."

    name = app_name.strip()
    if not name.endswith(".app"):
        name = name + ".app"

    candidates = [
        Path("/Applications") / name,
        Path.home() / "Applications" / name,
    ]

    for path in candidates:
        if path.exists():
            r = subprocess.run(
                ["osascript", "-e",
                 f'tell application "Finder" to delete POSIX file "{path}"'],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                return f"Moved '{path.name}' to Trash."
            return f"Could not delete: {r.stderr[-300:]}"

    return f"'{app_name}' not found in /Applications or ~/Applications."


def list_installed_apps() -> str:
    """List .app bundles in /Applications and ~/Applications."""
    found = []
    for d in [Path("/Applications"), Path.home() / "Applications"]:
        if d.exists():
            for item in sorted(d.iterdir()):
                if item.suffix == ".app":
                    found.append(item.stem)
    if not found:
        return "(no apps found)"
    return "\n".join(found[:80])


# =====================================================================
# MEMORY
# =====================================================================

def remember(text: str, tags: str = "") -> str:
    """Stores a fact, preference, or historical context about the user."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    entry = get_store().add(text, tag_list)
    if "error" in entry:
        return entry["error"]
    return f"Remembered: '{entry['text']}' (id={entry['id']})"

def recall(query: str) -> str:
    """Searches memory for entries related to the query term."""
    results = get_store().search(query)
    if not results:
        return "No matching memories found."
    return "\n".join(f"[{m['id']}] {m['text']}" for m in results)

def forget(memory_id_or_text: str) -> str:
    """Delete a memory by id, or by text search (matches any)."""
    store = get_store()
    arg = (memory_id_or_text or "").strip()
    if not arg:
        return "Nothing to forget — no id or text provided."

    # 1) Try as an exact id first.
    if arg.startswith("m_") and store.forget(arg):
        return f"Forgotten by id: {arg}"

    # 2) Otherwise search for matching memories and delete all of them.
    matches = store.search(arg, limit=20)
    if not matches:
        return f"No memory matches '{arg}'. (Nothing to forget.)"

    deleted = []
    for m in matches:
        if store.forget(m["id"]):
            deleted.append(f"[{m['id']}] {m['text']}")

    return "Forgotten:\n" + "\n".join(deleted)

def list_memories() -> str:
    """Retrieves all current stored memories."""
    mems = get_store().all()
    if not mems:
        return "No memories saved yet."
    return "\n".join(f"[{m['id']}] {m['text']}" for m in mems)

def forget_all(*_args, **_kwargs) -> str:
    store = get_store()
    count = store.count()
    store._data["memories"] = []
    store._save()
    return (
        f"Cleared all {count} memories. "
        "This operation is COMPLETE. Do NOT call forget_all again."
    )


# =====================================================================
# META
# =====================================================================

def list_tools() -> str:
    return ", ".join(sorted(TOOL_FUNCTIONS.keys()))


# =====================================================================
# SCHEMAS
# =====================================================================

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current local date and time.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate a math expression, e.g. '2+2', 'sqrt(16)*3'.",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_wikipedia",
            "description": "Search Wikipedia for a topic.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Look up factual information on the web.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name"}
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open a macOS application by name (Safari, Spotify, Finder, etc.).",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "Open a URL in the default browser.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_info",
            "description": "Get battery, uptime and disk usage for this Mac.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_clipboard",
            "description": "Read the current text contents of the clipboard.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_clipboard",
            "description": "Copy given text to the clipboard.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Capture the screen and save it to the Desktop.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command on the user's Mac and return the output.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in a directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a text file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_brew_package",
            "description": (
                "Install a program on this Mac via Homebrew. Use for any request "
                "like 'install X', 'get me X', 'I need X'. Works for CLI tools "
                "(ffmpeg, jq) and GUI apps (firefox, docker, spotify)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {"type": "string", "description": "Homebrew package name"}
                },
                "required": ["package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "brew_search",
            "description": "Search Homebrew for installable packages by name or keyword.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_installed_brew",
            "description": "List Homebrew packages currently installed on this Mac.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tools",
            "description": "Return the full list of tools MAE-2 can use right now.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
        {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Save a fact about the user or their world to long-term memory. "
                "Use whenever the user says 'remember...', shares a preference, "
                "a name, a project, a date, or anything that should persist "
                "between sessions. Optionally tag with comma-separated keywords."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The fact to remember"},
                    "tags": {"type": "string", "description": "Optional comma-separated tags"}
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Search long-term memory for facts matching a query.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": (
                "Delete one or more memories. Pass either a memory id like "
                "'m_a1b2c3d4', OR a text/query term like 'name' or 'preferences' — "
                "in which case all matching memories are deleted. "
                "Call this AT MOST ONCE per user request; it handles all matches itself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id_or_text": {"type": "string"}
                },
                "required": ["memory_id_or_text"],
            },
        },
    },


    {
        "type": "function",
        "function": {
            "name": "list_memories",
            "description": "List every fact MAE-2 has in long-term memory.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_all",
            "description": (
                "Delete EVERY memory in one shot. Use ONLY when the user "
                "explicitly says to erase/clear/reset ALL memories. "
                "Call this EXACTLY ONCE per user request — it is idempotent "
                "and doing it twice is a mistake."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pip_install",
            "description": "Install a Python package with pip (user-level).",
            "parameters": {
                "type": "object",
                "properties": {"package": {"type": "string"}},
                "required": ["package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pip_uninstall",
            "description": "Uninstall a Python package with pip.",
            "parameters": {
                "type": "object",
                "properties": {"package": {"type": "string"}},
                "required": ["package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "npm_install",
            "description": "Install an npm package (global by default).",
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {"type": "string"},
                    "global_": {"type": "boolean", "default": True},
                },
                "required": ["package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "npm_uninstall",
            "description": "Uninstall an npm package (global by default).",
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {"type": "string"},
                    "global_": {"type": "boolean", "default": True},
                },
                "required": ["package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mas_install",
            "description": (
                "Install an app from the Mac App Store by name. "
                "Requires `mas` (install it via install_brew_package('mas'))."
            ),
            "parameters": {
                "type": "object",
                "properties": {"app_name": {"type": "string"}},
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "uninstall_brew_package",
            "description": "Uninstall a Homebrew formula or cask.",
            "parameters": {
                "type": "object",
                "properties": {"package": {"type": "string"}},
                "required": ["package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "uninstall_app",
            "description": (
                "Move a .app to the Trash (recoverable). Use for any app "
                "installed manually or dragged to /Applications."
            ),
            "parameters": {
                "type": "object",
                "properties": {"app_name": {"type": "string"}},
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_installed_apps",
            "description": "List installed macOS applications in /Applications.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]



# =====================================================================
# REGISTRY
# =====================================================================

TOOL_FUNCTIONS = {
    "get_current_time": get_current_time,
    "calculate": calculate,
    "search_wikipedia": search_wikipedia,
    "web_search": web_search,
    "get_weather": get_weather,
    "open_app": open_app,
    "open_url": open_url,
    "get_system_info": get_system_info,
    "get_clipboard": get_clipboard,
    "set_clipboard": set_clipboard,
    "take_screenshot": take_screenshot,
    "run_shell": run_shell,
    "list_files": list_files,
    "read_file": read_file,
    "install_brew_package": install_brew_package,
    "brew_search": brew_search,
    "list_installed_brew": list_installed_brew,
    "list_tools": list_tools,
    "remember": remember,
    "recall": recall,
    "forget": forget,
    "list_memories": list_memories,
    "forget": forget,
    "forget_all": forget_all,
    "pip_install": pip_install,
    "pip_uninstall": pip_uninstall,
    "npm_install": npm_install,
    "npm_uninstall": npm_uninstall,
    "mas_install": mas_install,
    "uninstall_brew_package": uninstall_brew_package,
    "uninstall_app": uninstall_app,
    "list_installed_apps": list_installed_apps,
}


# Tools that require explicit user confirmation before running
CONFIRM_TOOLS = {"run_shell"}

TOOL_SCHEMAS.extend(MAC_SCHEMAS)
TOOL_FUNCTIONS.update(MAC_FUNCS)

# =====================================================================
# SELF-CHECK
# =====================================================================

if __name__ == "__main__":
    print(f"tools.py OK — {len(TOOL_FUNCTIONS)} tools registered:")
    for name in sorted(TOOL_FUNCTIONS):
        print(f"  • {name}")
