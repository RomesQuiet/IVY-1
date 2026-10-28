# ivy/tools.py
"""IVY-1 core tools: web, system, files, packages, memory, timers."""

import ast
import html
import math
import operator
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import requests

from . import config
from .memory import get_store
from .toolkit import TOOLS, tool

_HTTP = requests.Session()
_HTTP.headers["User-Agent"] = "Mozilla/5.0 (Macintosh) IVY-1/1.0"


def _sh(cmd: list[str], timeout: float = 30, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def _osa(script: str, timeout: float = 15) -> str:
    r = _sh(["osascript", "-e", script], timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:400])
    return r.stdout.strip()


# =====================================================================
# CORE
# =====================================================================

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}
_MATH = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
_MATH.update({"abs": abs, "round": round, "min": min, "max": max})


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _MATH:
        return _MATH[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _MATH:
        return _MATH[node.func.id](*[_eval(a) for a in node.args])
    raise ValueError("unsupported expression")


@tool(expression="Math expression, e.g. '2**10', 'sqrt(16)*3', '15% of 80' as '0.15*80'")
def calculate(expression: str) -> str:
    """Evaluate a math expression exactly. Use instead of doing arithmetic in your head."""
    value = _eval(ast.parse(expression.replace("^", "**"), mode="eval").body)
    if isinstance(value, float):
        value = round(value, 10)   # 399.50000000000006 → 399.5
        if value.is_integer():
            value = int(value)
    return f"{expression} = {value}"


# =====================================================================
# WEB
# =====================================================================

def _wikipedia(query: str) -> str:
    r = _HTTP.get(
        "https://en.wikipedia.org/w/api.php",
        params={"action": "query", "list": "search", "srsearch": query,
                "format": "json", "srlimit": 3},
        timeout=8,
    )
    hits = r.json().get("query", {}).get("search", [])
    return "\n".join(
        f"{h['title']}: {html.unescape(re.sub(r'<[^>]+>', '', h['snippet']))}" for h in hits
    ) or "No results."


def _news(query: str) -> str:
    import xml.etree.ElementTree as ET
    r = _HTTP.get("https://news.google.com/rss/search",
                  params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=8)
    items = ET.fromstring(r.content).findall(".//item")[:6]
    return "\n".join(f"{(it.findtext('pubDate') or '')[:16]} | {it.findtext('title')}"
                     for it in items) or "No news results."


@tool(query="Search keywords, e.g. 'F1 race winner', 'latest iPhone model'")
def web_search(query: str) -> str:
    """Fast web lookup (~1 s): latest news headlines with dates plus Wikipedia summaries.
    Use for anything current or factual you aren't sure of."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as pool:
        news, wiki = pool.submit(_news, query), pool.submit(_wikipedia, query)
        parts = []
        for label, fut in (("News", news), ("Wikipedia", wiki)):
            try:
                parts.append(f"{label}:\n{fut.result(timeout=10)}")
            except Exception as e:
                parts.append(f"{label}: unavailable ({e})")
    return "\n\n".join(parts)


@tool(group="web", ack="Let me dig into that.", timeout=60,
      question="The full question to research")
def deep_web_search(question: str) -> str:
    """Slow (~10 s) full web research that reads real pages and returns a sourced answer.
    Uses a lot of quota — only when web_search's headlines weren't enough."""
    from openai import OpenAI
    client = OpenAI(base_url=config.GROQ_BASE_URL, api_key=config.GROQ_API_KEY)
    r = client.chat.completions.create(
        model=config.MODEL_SEARCH,
        messages=[{"role": "user", "content":
                   f"Search the web and answer concisely with key facts and source names: {question}"}],
        tools=[{"type": "browser_search"}],
        tool_choice="required",
        max_tokens=700,
    )
    answer = re.sub(r"【[^】]*】", "", r.choices[0].message.content or "").strip()
    return answer or web_search(question)


@tool(group="web", ack="Reading the page.", url="Full http(s) URL")
def fetch_webpage(url: str) -> str:
    """Download a web page and return its readable text. Use to read an article or docs."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    r = _HTTP.get(url, timeout=12)
    r.raise_for_status()
    text = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", r.text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()


@tool(location="City name, e.g. 'Lisbon'")
def get_weather(location: str) -> str:
    """Current weather and today's forecast for a city."""
    r = _HTTP.get(f"https://wttr.in/{quote(location)}?format=j1", timeout=10)
    r.raise_for_status()
    data = r.json()
    cur = data["current_condition"][0]
    today = data.get("weather", [{}])[0]
    return (
        f"{location}: {cur['weatherDesc'][0]['value'].strip()}, {cur['temp_C']}°C "
        f"(feels {cur['FeelsLikeC']}°C), humidity {cur['humidity']}%, "
        f"wind {cur['windspeedKmph']} km/h. Today: {today.get('mintempC', '?')}–"
        f"{today.get('maxtempC', '?')}°C."
    )


# =====================================================================
# macOS SYSTEM
# =====================================================================

@tool(name="App name, e.g. 'Safari', 'Spotify', 'Visual Studio Code'")
def open_app(name: str) -> str:
    """Open (or bring forward) a macOS application."""
    r = _sh(["open", "-a", name], timeout=10)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"no app named {name}")
    return f"Opened {name}."


@tool()
def open_url(url: str) -> str:
    """Open a website in the default browser."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    _sh(["open", url], timeout=10).check_returncode()
    return f"Opened {url}."


@tool(group="system")
def get_system_info() -> str:
    """Battery level, uptime, and free disk space of this Mac."""
    batt = _sh(["pmset", "-g", "batt"]).stdout.strip()
    up = _sh(["uptime"]).stdout.strip()
    disk = _sh(["df", "-h", "/"]).stdout.strip().splitlines()[-1]
    return f"Battery: {batt}\nUptime: {up}\nDisk (/): {disk}"


@tool(level="Volume 0–100. Use 0 to mute.")
def set_volume(level: int) -> str:
    """Set the Mac's output volume."""
    level = max(0, min(100, level))
    _osa(f"set volume output volume {level}" + (" with output muted" if level == 0 else " without output muted"))
    return f"Volume set to {level}%."


@tool(action="What to do", app="Which player to control")
def media_control(action: Literal["play", "pause", "playpause", "next", "previous", "now_playing"],
                  app: Literal["Music", "Spotify"] = "Music") -> str:
    """Control music playback in Apple Music or Spotify, or say what's playing."""
    cmd = {"play": "play", "pause": "pause", "playpause": "playpause",
           "next": "next track", "previous": "previous track"}
    if action == "now_playing":
        return _osa(f'tell application "{app}" to return (name of current track) & " — " & (artist of current track)')
    _osa(f'tell application "{app}" to {cmd[action]}')
    return f"{app}: {action}."


@tool(group="system", title="Short title", message="Notification body")
def notify(title: str, message: str) -> str:
    """Show a macOS notification banner."""
    _osa(f'display notification {_as_str(message)} with title {_as_str(title)}')
    return "Notification shown."


def _as_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


_timers: dict[int, tuple[str, float, threading.Timer]] = {}


@tool(minutes="Duration in minutes (decimals allowed, e.g. 0.5)", label="What the timer is for")
def set_timer(minutes: float, label: str = "Timer") -> str:
    """Start a countdown. When it ends Ivy announces it out loud and shows a notification."""
    from . import voice
    seconds = max(1.0, minutes * 60)
    tid = max(_timers, default=0) + 1

    def fire():
        _timers.pop(tid, None)
        try:
            notify("IVY-1", f"{label} — time's up.")
        except Exception:
            pass
        voice.chime()
        voice.speak_async(f"{label} is done.")

    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()
    _timers[tid] = (label, time.time() + seconds, t)
    return f"Timer #{tid} '{label}' set for {minutes:g} min."


@tool(group="system")
def list_timers() -> str:
    """List running timers and how long is left on each."""
    if not _timers:
        return "No timers running."
    now = time.time()
    return "\n".join(f"#{i} {lbl}: {max(0, int(end - now)) // 60}m {max(0, int(end - now)) % 60}s left"
                     for i, (lbl, end, _) in _timers.items())


@tool(group="system")
def cancel_timer(timer_id: int) -> str:
    """Cancel a running timer by its number."""
    entry = _timers.pop(timer_id, None)
    if not entry:
        return f"No timer #{timer_id}."
    entry[2].cancel()
    return f"Cancelled timer #{timer_id} ({entry[0]})."


@tool(group="system")
def get_clipboard() -> str:
    """Read the text currently on the clipboard."""
    return _sh(["pbpaste"]).stdout[:3000] or "(clipboard is empty)"


@tool(group="system")
def set_clipboard(text: str) -> str:
    """Copy text to the clipboard."""
    subprocess.run(["pbcopy"], input=text, text=True, check=True)
    return "Copied to clipboard."


@tool(group="system")
def take_screenshot() -> str:
    """Capture the whole screen and save it to the Desktop."""
    path = Path.home() / "Desktop" / f"ivy_screenshot_{int(time.time())}.png"
    _sh(["screencapture", "-x", str(path)]).check_returncode()
    return f"Saved screenshot to {path}"


@tool(group="system")
def list_installed_apps() -> str:
    """List applications installed in /Applications and ~/Applications."""
    found = sorted(
        item.stem
        for d in (Path("/Applications"), Path.home() / "Applications") if d.exists()
        for item in d.iterdir() if item.suffix == ".app" and not item.name.startswith(".")
    )
    return ", ".join(found) or "(no apps found)"


# =====================================================================
# FILES & SHELL
# =====================================================================

@tool(group="files", confirm="shell", timeout=60, command="zsh command line")
def run_shell(command: str) -> str:
    """Run a shell command on this Mac and return its output. Use for anything the
    other tools don't cover (git, file ops, system queries, scripts)."""
    r = _sh(["/bin/zsh", "-lc", command], timeout=55)
    out = ((r.stdout or "") + (r.stderr or "")).strip() or "(no output)"
    return out if r.returncode == 0 else f"exit {r.returncode}\n{out}"


@tool(group="files", path="Directory, '~' allowed")
def list_files(path: str = "~") -> str:
    """List files in a directory."""
    p = Path(path).expanduser()
    items = sorted(p.iterdir(), key=lambda i: (not i.is_dir(), i.name.lower()))
    shown = [f"{'📁' if i.is_dir() else '📄'} {i.name}" for i in items if not i.name.startswith(".")]
    return "\n".join(shown[:100]) or "(empty)"


@tool(group="files", path="File path, '~' allowed")
def read_file(path: str) -> str:
    """Read a text file."""
    return Path(path).expanduser().read_text(errors="replace")


# =====================================================================
# SOFTWARE — one tool per verb, the manager is a parameter
# =====================================================================

Manager = Literal["brew", "cask", "pip", "npm", "appstore", "app"]


@tool(group="software", confirm="action", ack="Installing that now.", timeout=600,
      name="Package or app name", manager="brew = CLI tools and most apps (tries cask too), "
      "pip = Python, npm = Node, appstore = opens the App Store page")
def install_package(name: str, manager: Manager = "brew") -> str:
    """Install software on this Mac. Use for 'install X', 'get me X', 'I need X'."""
    name = name.strip()
    if manager == "appstore":
        _sh(["open", f"macappstore://search.itunes.apple.com/WebObjects/MZSearch.woa/wa/search?q={quote(name)}"])
        return f"Opened the App Store search for '{name}' — click Get there."
    cmds = {
        "brew": [["brew", "install", name], ["brew", "install", "--cask", name]],
        "cask": [["brew", "install", "--cask", name]],
        "pip": [["python3", "-m", "pip", "install", "--user", name]],
        "npm": [["npm", "install", "-g", name]],
    }.get(manager)
    if not cmds:
        return f"Can't install with '{manager}'."
    errors = []
    for cmd in cmds:
        r = _sh(cmd, timeout=590)
        if r.returncode == 0:
            return f"Installed {name} ({' '.join(cmd[:3])})."
        errors.append(r.stderr.strip()[-300:])
    return f"Install of {name} failed:\n" + "\n".join(errors)


@tool(group="software", confirm="action", timeout=300,
      manager="'app' moves a .app from /Applications to the Trash; others use that package manager")
def uninstall_package(name: str, manager: Manager = "brew") -> str:
    """Uninstall software from this Mac."""
    name = name.strip()
    if manager in ("app", "appstore"):
        app = name if name.endswith(".app") else name + ".app"
        for d in (Path("/Applications"), Path.home() / "Applications"):
            if (d / app).exists():
                _osa(f'tell application "Finder" to delete POSIX file {_as_str(str(d / app))}', timeout=30)
                return f"Moved {app} to the Trash."
        return f"{app} not found."
    cmds = {
        "brew": [["brew", "uninstall", name], ["brew", "uninstall", "--cask", name]],
        "cask": [["brew", "uninstall", "--cask", name]],
        "pip": [["python3", "-m", "pip", "uninstall", "-y", name]],
        "npm": [["npm", "uninstall", "-g", name]],
    }[manager]
    for cmd in cmds:
        if _sh(cmd, timeout=290).returncode == 0:
            return f"Uninstalled {name}."
    return f"Could not uninstall {name} with {manager}."


@tool(group="software", timeout=40)
def brew_search(query: str) -> str:
    """Search Homebrew for installable packages and apps."""
    r = _sh(["brew", "search", query], timeout=35)
    return (r.stdout or r.stderr).strip()[:1500] or "(no results)"


# =====================================================================
# MEMORY
# =====================================================================

@tool(text="The fact, short and in third person, e.g. 'Romeu prefers metric units'",
      tags="Optional comma-separated keywords")
def remember(text: str, tags: str = "") -> str:
    """Save a lasting fact about the user (name, preference, project, date). Recall first
    if it might contradict an existing memory."""
    entry = get_store().add(text, [t.strip() for t in tags.split(",") if t.strip()])
    if "error" in entry:
        return entry["error"]
    return f"Remembered (id={entry['id']})."


@tool(query="Keywords; leave empty to list every memory")
def recall(query: str = "") -> str:
    """Search long-term memory. With an empty query, lists all memories."""
    store = get_store()
    results = store.search(query, limit=10) if query.strip() else store.all()
    return "\n".join(f"[{m['id']}] {m['text']}" for m in results) or "No matching memories."


@tool(group="memory", memory_id_or_text="A memory id like 'm_a1b2c3d4', or words matching the memories to delete")
def forget(memory_id_or_text: str) -> str:
    """Delete memories by id or by matching text. Handles every match in one call."""
    store = get_store()
    arg = memory_id_or_text.strip()
    if arg.startswith("m_") and store.forget(arg):
        return f"Forgotten {arg}."
    matches = store.search(arg, limit=20)
    deleted = [f"[{m['id']}] {m['text']}" for m in matches if store.forget(m["id"])]
    return ("Forgotten:\n" + "\n".join(deleted)) if deleted else f"No memory matches '{arg}'."


@tool(group="memory", confirm="action")
def forget_all() -> str:
    """Erase EVERY memory. Only when the user explicitly asks to wipe all memories."""
    return f"Cleared all {get_store().clear()} memories."


# Register the macOS control tools (AppleScript, UI, keyboard, browser JS).
from . import tools_mac  # noqa: E402,F401


if __name__ == "__main__":
    print(f"{len(TOOLS)} tools registered:")
    for t in TOOLS.values():
        print(f"  • {t.name:20} {t.confirm or ''}")
