# ivy/tools.py
"""IVY-1 core tools: web, system, files, packages, memory, timers."""

import ast
import html
import math
import operator
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import requests

from . import config, safety, senses
from .memory import get_store
from .toolkit import TOOLS, tool

_HTTP = requests.Session()
_HTTP.headers["User-Agent"] = "Mozilla/5.0 (Macintosh) IVY-1/1.0"


def _sh(cmd: list[str], timeout: float = 30, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def workspace() -> Path:
    config.WORKSPACE.mkdir(parents=True, exist_ok=True)
    return config.WORKSPACE


def resolve_path(path: str) -> Path:
    """Relative paths mean Ivy's working folder, not wherever the app happened to start."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else workspace() / p


def in_workspace(path: str) -> bool:
    return workspace().resolve() in (resolve_path(path).resolve(), *resolve_path(path).resolve().parents)


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
    if not hits:
        return "No results."
    # the top article's opening paragraph has the key facts; snippets usually don't
    intro = _HTTP.get("https://en.wikipedia.org/w/api.php", timeout=8, params={
        "action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
        "exchars": 900, "titles": hits[0]["title"], "format": "json", "redirects": 1,
    }).json().get("query", {}).get("pages", {})
    text = next(iter(intro.values()), {}).get("extract", "")
    others = "; ".join(h["title"] for h in hits[1:])
    return f"{hits[0]['title']}: {text}" + (f"\n(Also: {others})" if others else "")


def _news(query: str) -> str:
    import xml.etree.ElementTree as ET
    r = _HTTP.get("https://news.google.com/rss/search",
                  params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=8)
    items = ET.fromstring(r.content).findall(".//item")[:6]
    return "\n".join(f"{(it.findtext('pubDate') or '')[:16]} | {it.findtext('title')}"
                     for it in items) or "No news results."


@tool(untrusted=True, query="Search keywords, e.g. 'F1 race winner', 'latest iPhone model'")
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


@tool(group="web", ack="Let me dig into that.", timeout=60, untrusted=True,
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


@tool(group="web", ack="Reading the page.", untrusted=True, url="Full http(s) URL")
def fetch_webpage(url: str) -> str:
    """Download a web page and return its readable text. Use to read an article or docs."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    r = _HTTP.get(url, timeout=12)
    r.raise_for_status()
    text = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", r.text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()


@tool(location="City name; leave empty for where Romeu is now")
def get_weather(location: str = "") -> str:
    """Current weather and today's forecast."""
    r = _HTTP.get(f"https://wttr.in/{quote(location)}?format=j1", timeout=10)
    r.raise_for_status()
    data = r.json()
    cur = data["current_condition"][0]
    today = data.get("weather", [{}])[0]
    if not location:
        area = (data.get("nearest_area") or [{}])[0]
        location = (area.get("areaName") or [{}])[0].get("value", "Here")
    return (
        f"{location}: {cur['weatherDesc'][0]['value'].strip()}, {cur['temp_C']}°C "
        f"(feels {cur['FeelsLikeC']}°C), humidity {cur['humidity']}%, "
        f"wind {cur['windspeedKmph']} km/h. Today: {today.get('mintempC', '?')}–"
        f"{today.get('maxtempC', '?')}°C."
    )


# =====================================================================
# macOS SYSTEM
# =====================================================================

_APP_ALIASES = {
    "vscode": "Visual Studio Code", "vs code": "Visual Studio Code", "code": "Visual Studio Code",
    "chrome": "Google Chrome", "brave": "Brave Browser", "settings": "System Settings",
    "system preferences": "System Settings", "app store": "App Store", "files": "Finder",
    "itunes": "Music", "apple music": "Music", "facetime": "FaceTime", "imessage": "Messages",
}


def installed_apps() -> dict[str, str]:
    """lowercase name → real app name, for every .app Ivy can open."""
    apps = {}
    for d in (Path("/Applications"), Path("/System/Applications"),
              Path("/System/Applications/Utilities"), Path.home() / "Applications"):
        if d.exists():
            for item in d.iterdir():
                if item.suffix == ".app" and not item.name.startswith("."):
                    apps[item.stem.lower()] = item.stem
    return apps


def default_browser() -> str:
    if config.BROWSER:
        return config.BROWSER
    r = _sh(["defaults", "read", "com.apple.LaunchServices/com.apple.launchservices.secure", "LSHandlers"])
    m = re.search(r'LSHandlerRoleAll = "([^"]+)";\s*LSHandlerURLScheme = https;', r.stdout)
    bundle = m.group(1) if m else ""
    return {"com.brave.browser": "Brave Browser", "com.google.chrome": "Google Chrome",
            "org.mozilla.firefox": "Firefox", "company.thebrowser.browser": "Arc",
            "com.microsoft.edgemac": "Microsoft Edge"}.get(bundle, "Safari")


def resolve_app(name: str) -> str | None:
    """Best installed app for a spoken name ('vs code', 'browser', 'spotify'…)."""
    key = name.lower().strip().removesuffix(" app").strip()
    if key in ("browser", "web browser", "internet", "default browser", "the browser"):
        return default_browser()
    key = _APP_ALIASES.get(key, key).lower()
    apps = installed_apps()
    if key in apps:
        return apps[key]
    starts = [real for low, real in apps.items() if low.startswith(key)]
    return min(starts, key=len) if starts else None


@tool(name="App name, e.g. 'Safari', 'Spotify', 'vs code', 'browser'")
def open_app(name: str) -> str:
    """Open (or bring forward) a macOS application. 'browser' opens Romeu's default browser."""
    real = resolve_app(name)
    if not real:
        raise RuntimeError(f"no installed app matches '{name}'")
    r = _sh(["open", "-a", real], timeout=10)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"couldn't open {real}")
    if senses.wait_for_app(real):
        return f"Opened {real}."
    return f"Launched {real}, but no window has appeared yet."


@tool(group="system")
def quit_app(name: str) -> str:
    """Quit a running app (it will still ask to save unsaved work)."""
    real = resolve_app(name) or name
    if not _running(real):
        return f"{real} isn't running."
    _osa(f"tell application {_as_str(real)} to quit")
    for _ in range(12):
        if not _running(real):
            return f"Quit {real}."
        time.sleep(0.25)
    return f"Asked {real} to quit, but it's still open (it may be waiting on an unsaved-changes dialog)."


def open_in_browser(url: str, browser: str = "") -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    app = browser or config.BROWSER
    cmd = ["open", "-a", resolve_app(app) or app, url] if app else ["open", url]
    _sh(cmd, timeout=10).check_returncode()
    return url


@tool(browser="Only if Romeu names one, e.g. 'Safari'; otherwise the default browser")
def open_url(url: str, browser: str = "") -> str:
    """SHOW a web page to Romeu in the browser. Only when asked to open or see something —
    Ivy can't read what's opened; to read a page use fetch_webpage."""
    return f"Opened {open_in_browser(url, browser)}."


_SEARCH_SITES = {
    "google": "https://www.google.com/search?q={}",
    "youtube": "https://www.youtube.com/results?search_query={}",
    "maps": "https://www.google.com/maps/search/{}",
    "images": "https://www.google.com/search?tbm=isch&q={}",
    "news": "https://news.google.com/search?q={}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={}",
    "amazon": "https://www.amazon.com/s?k={}",
    "github": "https://github.com/search?q={}",
    "reddit": "https://www.reddit.com/search/?q={}",
    "translate": "https://translate.google.com/?sl=auto&tl=en&text={}",
}


@tool(group="web", query="What to search for", site="Where to search")
def browse(query: str, site: Literal["google", "youtube", "maps", "images", "news", "wikipedia",
                                     "amazon", "github", "reddit", "translate"] = "google") -> str:
    """SHOW search results in Romeu's browser (Google, Maps directions, Amazon, GitHub…).
    Only when Romeu wants to see them — Ivy can't read the page. To find an answer, use web_search."""
    url = _SEARCH_SITES[site].format(quote(query))
    open_in_browser(url)
    return f"Opened {site} results for '{query}'."


@tool(group="system")
def get_system_info() -> str:
    """Battery level, uptime, and free disk space of this Mac."""
    batt = _sh(["pmset", "-g", "batt"]).stdout.strip()
    up = _sh(["uptime"]).stdout.strip()
    disk = _sh(["df", "-h", "/"]).stdout.strip().splitlines()[-1]
    return f"Battery: {batt}\nUptime: {up}\nDisk (/): {disk}"


@tool(group="media", level="Volume 0–100. Use 0 to mute.",
      change="Instead of level: relative change, e.g. 15 (louder) or -15 (quieter)")
def set_volume(level: int = -1, change: int = 0) -> str:
    """Set or change the Mac's output volume."""
    if change:
        level = senses.get_volume()[0] + change
    actual, muted = senses.set_volume(level)
    return "Muted." if muted else f"Volume {actual}%."


def _running(app: str) -> bool:
    return _osa(f"application {_as_str(app)} is running") == "true"


def active_player() -> str:
    """Spotify if it's running, else Apple Music."""
    return "Spotify" if _running("Spotify") or not _running("Music") and config.MUSIC_APP == "Spotify" else "Music"


@tool(group="media", action="What to do", app="Leave as auto to use whichever player is running")
def media_control(action: Literal["play", "pause", "next", "previous", "now_playing"],
                  app: Literal["auto", "Music", "Spotify"] = "auto") -> str:
    """Control music playback (Spotify or Apple Music), or say what's playing."""
    app = active_player() if app == "auto" else app
    if action == "now_playing":
        if not _running(app):
            return "Nothing is playing."
        return "Now playing: " + _osa(f'tell application "{app}" to return (name of current track) & " by " & (artist of current track)')
    if not _running(app):
        return f"{app} isn't running, so there's nothing to {action}."
    track = f'tell application "{app}" to return (name of current track) & " by " & (artist of current track)'
    before = _osa(track)
    cmd = {"play": "play", "pause": "pause", "next": "next track", "previous": "previous track"}
    _osa(f'tell application "{app}" to {cmd[action]}')
    time.sleep(0.5)
    state = _osa(f'tell application "{app}" to return player state as string')
    now = _osa(track)
    if action == "pause":
        return "Paused." if state != "playing" else f"Tried to pause, but {app} is still playing."
    if action == "play":
        return f"Playing {now}." if state == "playing" else f"Tried to play, but {app} is {state}."
    return f"Now playing {now}." if now != before else f"Sent {action}, but {app} is still on {now}."


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

@tool(confirm="shell", timeout=60, command="zsh command line")
def run_shell(command: str) -> str:
    """Run a shell command on this Mac and return its output; it's stopped after 55 s.
    Starts in Ivy's working folder. Use for anything other tools don't cover."""
    refused = safety.forbidden(command)
    if refused:
        return refused
    if safety.deletes(command):
        return "NOT DONE: run_shell doesn't delete. Use remove_file, which can be undone."
    r = _sh(["/bin/zsh", "-lc", command], timeout=55, cwd=str(workspace()))
    out = ((r.stdout or "") + (r.stderr or "")).strip() or "(no output)"
    return out if r.returncode == 0 else f"exit {r.returncode}\n{out}"


@tool(group="files", confirm="shell", timeout=130, code="Python 3 source; print() what you want back")
def run_python(code: str) -> str:
    """Run a Python script on this Mac (requests, numpy and the standard library are
    available). Use for calculations, data work, file processing, APIs — anything."""
    import sys
    refused = safety.forbidden_python(code)
    if refused:
        return refused
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
    try:
        r = _sh([sys.executable, f.name], timeout=120, cwd=str(workspace()))
    finally:
        Path(f.name).unlink(missing_ok=True)
    out = ((r.stdout or "") + (r.stderr or "")).strip() or "(no output)"
    return out if r.returncode == 0 else f"exit {r.returncode}\n{out}"


def _hold(path: Path) -> Path:
    """Move something to the holding folder instead of deleting it."""
    folder = Path(config.REMOVED_DIR) / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    folder.mkdir(parents=True)
    (folder / "from.txt").write_text(str(path))
    held = folder / path.name
    shutil.move(str(path), str(held))
    return held


@tool()
def my_status() -> str:
    """Ivy's real models, router mode, token use and how to switch. Use for any question about herself."""
    from .router import get_pool
    return get_pool().report()


@tool(group="files", confirm="always", timeout=120, path="File or folder to remove")
def remove_file(path: str) -> str:
    """Remove a file or folder. It's kept so restore_removed can bring it back."""
    target = resolve_path(path)
    refused = safety.forbidden(f"rm {target}")
    if refused or target == Path.home() or target.parent == Path.home():
        return refused or f"NOT DONE: {target} is too broad to remove."
    if not target.exists():
        return f"{target} not found."
    _hold(target)
    return f"Removed {target.name}. It can be restored."


@tool(group="files", timeout=120, name="Name of the removed item; empty lists them")
def restore_removed(name: str = "") -> str:
    """List what Ivy removed, or put one item back where it was."""
    held = sorted(p for p in Path(config.REMOVED_DIR).glob("*/*") if p.name != "from.txt") if Path(config.REMOVED_DIR).exists() else []
    if not name:
        return "\n".join(f"{p.name} (removed {p.parent.name[:8]})" for p in held) or "Nothing to restore."
    match = [p for p in held if p.name.lower().removesuffix(".app") == name.lower().removesuffix(".app")]
    if not match:
        return f"{name} isn't in the removed items."
    item = match[-1]
    home = Path((item.parent / "from.txt").read_text())
    if home.exists():
        return f"NOT DONE: something already exists at {home}."
    home.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(item), str(home))
    shutil.rmtree(item.parent)
    return f"Restored {home}."


@tool(group="files", timeout=20, command="zsh command, e.g. 'python3 main.py'", folder="Folder to run it in")
def launch_program(command: str, folder: str = "") -> str:
    """Start something that keeps running (a game, app or server) and check it survived the first
    seconds. If it crashes, returns the error output."""
    refused = safety.forbidden(command)
    if refused:
        return refused
    cwd = resolve_path(folder) if folder else workspace()
    if not cwd.is_dir():
        return f"NOT DONE: there's no folder {cwd}."
    log = Path(tempfile.gettempdir()) / f"ivy_program_{int(time.time())}.log"
    with open(log, "w") as out:
        proc = subprocess.Popen(["/bin/zsh", "-lc", command], cwd=str(cwd), stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(12):
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    if proc.poll() is None:
        return f"Running (process {proc.pid}), from {cwd}."
    return f"It stopped straight away (exit {proc.returncode}):\n{log.read_text()[-1500:]}"


@tool(group="files", path="Directory, '~' allowed; relative means the working folder")
def list_files(path: str = "") -> str:
    """List files in a directory."""
    p = resolve_path(path)
    items = sorted(p.iterdir(), key=lambda i: (not i.is_dir(), i.name.lower()))
    shown = [f"{'📁' if i.is_dir() else '📄'} {i.name}" for i in items if not i.name.startswith(".")]
    return "\n".join(shown[:100]) or "(empty)"


@tool(group="files", path="File path, '~' allowed", start_line="First line to show, for long files")
def read_file(path: str, start_line: int = 1) -> str:
    """Read a text file with line numbers (about 150 lines at a time)."""
    lines = resolve_path(path).read_text(errors="replace").splitlines()
    start = max(1, start_line)
    shown = lines[start - 1:start + 149]
    out = "\n".join(f"{i:5} {line[:200]}" for i, line in enumerate(shown, start))
    if start + 150 <= len(lines):
        out += f"\n…({len(lines)} lines total; continue with start_line={start + 150})"
    return out or "(empty file)"


@tool(group="files", confirm="action", path="File to create or replace")
def write_file(path: str, content: str) -> str:
    """Create a file, or replace its whole contents. Relative paths go in Ivy's working folder."""
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Wrote {len(content)} chars to {p} (read back {p.stat().st_size} bytes)."


@tool(group="files", confirm="action", old="Exact text to replace; must appear exactly once",
      new="Replacement text")
def edit_file(path: str, old: str, new: str) -> str:
    """Change part of a file by exact text replacement. Read the file first."""
    p = resolve_path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        return f"Not changed: that text appears {count} times. Read the file and give an exact, unique match."
    p.write_text(text.replace(old, new))
    return f"Edited {p}."


@tool(group="files", timeout=25, query="File name or words inside files (Spotlight)",
      folder="Limit the search to this folder")
def find_files(query: str, folder: str = "") -> str:
    """Find files anywhere on this Mac by name or content using Spotlight."""
    cmd = ["mdfind", query] + (["-onlyin", str(Path(folder).expanduser())] if folder else [])
    hits = _sh(cmd, timeout=20).stdout.strip().splitlines()
    return "\n".join(hits[:40]) + (f"\n…({len(hits)} total)" if len(hits) > 40 else "") if hits else "No files found."


@tool(group="files", timeout=25, pattern="Text or regular expression", folder="Folder to search")
def search_in_files(pattern: str, folder: str = "~") -> str:
    """Search inside text files in a folder (like grep), with file names and line numbers."""
    r = _sh(["grep", "-rnIE", "--exclude-dir=.git", "--exclude-dir=node_modules", "--exclude-dir=.venv",
             "--exclude-dir=__pycache__", pattern, str(Path(folder).expanduser())], timeout=20)
    lines = r.stdout.splitlines()
    return "\n".join(line[:200] for line in lines[:40]) + \
        (f"\n…({len(lines)} matches total)" if len(lines) > 40 else "") if lines else "No matches."


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


_NEVER_UNINSTALL = {"ivy", "claude", "finder", "safari", "terminal", "python", "system settings"}


@tool(group="software", confirm="always", timeout=300,
      manager="'app' removes a .app from /Applications, restorably; others use that package manager")
def uninstall_package(name: str, manager: Manager = "brew") -> str:
    """Uninstall ONE app or package; Romeu confirms each one. Removed apps can be restored."""
    name = name.strip()
    if name.lower().removesuffix(".app") in _NEVER_UNINSTALL:
        return f"NOT DONE: {name} is protected. Romeu can remove it in Finder."
    if manager in ("app", "appstore"):
        app = name if name.endswith(".app") else name + ".app"
        for d in (Path("/Applications"), Path.home() / "Applications"):
            if (d / app).exists():
                _hold(d / app)
                return f"Removed {app}. restore_removed can bring it back."
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


@tool(group="memory", confirm="always")
def forget_all() -> str:
    """Erase EVERY memory. Only when the user explicitly asks to wipe all memories."""
    return f"Cleared all {get_store().clear()} memories."


# Register the rest: macOS control, media, everyday apps, and learned skills.
from . import browser, context, skills, tasks, tools_life, tools_mac, tools_media  # noqa: E402,F401
skills.load_all()


if __name__ == "__main__":
    print(f"{len(TOOLS)} tools registered:")
    for t in TOOLS.values():
        print(f"  • {t.name:20} {t.confirm or ''}")
