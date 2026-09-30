# mae/tools_mac.py
"""macOS control layer — AX, JS injection, coordinates. The 'hands' of MAE-2."""

import subprocess
import tempfile
import time
from pathlib import Path

try:
    import pyautogui
    _PYAUTOGUI_OK = True
except Exception:
    _PYAUTOGUI_OK = False


# =====================================================================
# APPLESCRIPT CORE
# =====================================================================

def _osascript(script: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode == 0:
            out = r.stdout.strip()
            return out if out else "(done)"
        return f"AppleScript error: {r.stderr.strip()[:500]}"
    except subprocess.TimeoutExpired:
        return "AppleScript timed out."
    except Exception as e:
        return f"AppleScript failed: {e}"


def applescript(code: str) -> str:
    """
    Run arbitrary AppleScript. This is the master key.
    Use for: opening apps, clicking UI elements by name, sending iMessage,
    controlling Music/Spotify, reading Calendar, toggling System Settings,
    and thousands more macOS automations.
    """
    if not code or not code.strip():
        return "Empty AppleScript."
    return _osascript(code.strip())


# =====================================================================
# UI INTROSPECTION (background, no focus steal)
# =====================================================================

def list_running_apps() -> str:
    return _osascript(
        'tell application "System Events" to get name of '
        '(processes where background only is false)'
    )


def list_ui_elements(app_name: str, window_index: int = 1) -> str:
    """List clickable elements in a background app's window."""
    script = f'''
    tell application "System Events"
        tell process "{app_name}"
            set out to ""
            try
                repeat with b in (every button of window {window_index})
                    set out to out & "button: " & (name of b) & "\\n"
                end repeat
            end try
            try
                repeat with t in (every text field of window {window_index})
                    set out to out & "textfield: " & (name of t) & "\\n"
                end repeat
            end try
            try
                repeat with c in (every checkbox of window {window_index})
                    set out to out & "checkbox: " & (name of c) & "\\n"
                end repeat
            end try
            try
                repeat with l in (every menu button of window {window_index})
                    set out to out & "menubutton: " & (name of l) & "\\n"
                end repeat
            end try
            return out
        end tell
    end tell
    '''
    return _osascript(script)


def click_element(app_name: str, element_name: str, window_index: int = 1) -> str:
    """Click a named UI element in a background window. No focus change."""
    script = f'''
    tell application "System Events"
        tell process "{app_name}"
            set win to window {window_index}
            try
                click button "{element_name}" of win
                return "clicked button"
            end try
            try
                click checkbox "{element_name}" of win
                return "clicked checkbox"
            end try
            try
                click menu button "{element_name}" of win
                return "clicked menu button"
            end try
            try
                click text field "{element_name}" of win
                return "clicked text field"
            end try
            return "element not found"
        end tell
    end tell
    '''
    return _osascript(script)


def activate_app(name: str) -> str:
    return _osascript(f'tell application "{name}" to activate')


# =====================================================================
# BROWSER JAVASCRIPT (background, precise)
# =====================================================================

def run_js_safari(code: str) -> str:
    """
    Run JavaScript in the frontmost Safari tab.
    Requires: Safari → Develop → Allow JavaScript from Apple Events.
    """
    if not code:
        return "Empty JS."
    escaped = code.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Safari" to do JavaScript "{escaped}" in document 1'
    return _osascript(script, timeout=20)


def run_js_chrome(code: str) -> str:
    """Run JavaScript in the active Chrome tab."""
    if not code:
        return "Empty JS."
    escaped = code.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'tell application "Google Chrome" to execute active tab of '
        f'front window javascript "{escaped}"'
    )
    return _osascript(script, timeout=20)


# =====================================================================
# COORDINATE LAYER (last resort — needs focus)
# =====================================================================

def click_at(x: int, y: int) -> str:
    if not _PYAUTOGUI_OK:
        return "pyautogui not installed."
    try:
        pyautogui.click(int(x), int(y))
        return f"Clicked at ({x}, {y})."
    except Exception as e:
        return f"Click failed: {e}"


def type_text(text: str) -> str:
    if not _PYAUTOGUI_OK:
        return "pyautogui not installed."
    try:
        pyautogui.write(text, interval=0.02)
        return f"Typed {len(text)} chars."
    except Exception as e:
        return f"Typing failed: {e}"


def press_key(keys: str) -> str:
    """e.g. 'enter', 'escape', 'cmd+c', 'cmd+shift+4'."""
    if not _PYAUTOGUI_OK:
        return "pyautogui not installed."
    try:
        parts = [p.strip().lower() for p in keys.split("+")]
        if len(parts) == 1:
            pyautogui.press(parts[0])
        else:
            pyautogui.hotkey(*parts)
        return f"Pressed {keys}."
    except Exception as e:
        return f"Key press failed: {e}"


def scroll(direction: str = "down", amount: int = 5) -> str:
    if not _PYAUTOGUI_OK:
        return "pyautogui not installed."
    try:
        clicks = int(amount) if direction == "up" else -int(amount)
        pyautogui.scroll(clicks)
        return f"Scrolled {direction} by {amount}."
    except Exception as e:
        return f"Scroll failed: {e}"


def screenshot(path: str = None) -> str:
    if path is None:
        path = str(Path(tempfile.gettempdir()) / "mae_screen.png")
    subprocess.run(["screencapture", "-x", path], check=True)
    return f"Screenshot saved: {path}"


def get_screen_size() -> str:
    if not _PYAUTOGUI_OK:
        return "pyautogui not installed."
    w, h = pyautogui.size()
    return f"{w}x{h}"


# =====================================================================
# SCHEMAS
# =====================================================================

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "applescript",
            "description": (
                "Run arbitrary AppleScript on macOS. This is the master tool for "
                "controlling macOS: opening apps, clicking UI elements by name in "
                "background windows (no focus steal), sending iMessage, controlling "
                "Music/Spotify, reading Calendar, toggling System Settings, moving "
                "windows, and much more. When you need something macOS-related that "
                "no other tool covers, write AppleScript and call this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "AppleScript source"}
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_running_apps",
            "description": "List GUI apps currently running on the Mac.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_ui_elements",
            "description": (
                "List named UI elements (buttons, text fields, checkboxes) inside "
                "a specific app's window. Use BEFORE click_element so you know what "
                "elements exist. Works on background apps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {"type": "string"},
                    "window_index": {"type": "integer", "default": 1},
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_element",
            "description": (
                "Click a named UI element inside a background app window. "
                "Example: click_element('Spotify', 'Next') clicks the Next button "
                "in Spotify without stealing focus from your active window."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {"type": "string"},
                    "element_name": {"type": "string"},
                    "window_index": {"type": "integer", "default": 1},
                },
                "required": ["app_name", "element_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "activate_app",
            "description": "Bring a running app to the foreground.",
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
            "name": "run_js_safari",
            "description": (
                "Run JavaScript in the frontmost Safari tab. Use this for precise "
                "web automation: clicking buttons by CSS selector, filling forms, "
                "reading page content, navigating. Works without stealing focus. "
                "Example for YouTube play: "
                "document.querySelector('.ytp-play-button')?.click()"
            ),
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_js_chrome",
            "description": "Same as run_js_safari but for Google Chrome.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_at",
            "description": (
                "Click at screen coordinates. LAST RESORT — use only when "
                "click_element and run_js_* have failed. Steals focus. "
                "Get coordinates from a screenshot first if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text at the current focus (keyboard simulation).",
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
            "name": "press_key",
            "description": "Press a key or key combo. Examples: 'enter', 'escape', 'cmd+c', 'cmd+shift+4'.",
            "parameters": {
                "type": "object",
                "properties": {"keys": {"type": "string"}},
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll the current view.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down"], "default": "down"},
                    "amount": {"type": "integer", "default": 5},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Capture the full screen to a PNG file. Returns the path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_screen_size",
            "description": "Return the screen resolution, e.g. '1440x900'.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


TOOL_FUNCTIONS = {
    "applescript": applescript,
    "list_running_apps": list_running_apps,
    "list_ui_elements": list_ui_elements,
    "click_element": click_element,
    "activate_app": activate_app,
    "run_js_safari": run_js_safari,
    "run_js_chrome": run_js_chrome,
    "click_at": click_at,
    "type_text": type_text,
    "press_key": press_key,
    "scroll": scroll,
    "screenshot": screenshot,
    "get_screen_size": get_screen_size,
}

# Tools that alter the system — require session approval (mode "b")
ACTION_TOOLS = {
    "click_element", "run_js_safari", "run_js_chrome",
    "click_at", "type_text", "press_key", "scroll",
    "applescript", "activate_app",
}
