# ivy/tools_mac.py
"""macOS control layer — AppleScript, UI elements, browser JS, keyboard/mouse.
The 'hands' of IVY-1. Everything that changes what's on screen asks first
(see config.ACTION_MODE)."""

import subprocess
from typing import Literal

from .toolkit import tool


def _q(s: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _osascript(script: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "AppleScript timed out."
    if r.returncode == 0:
        return r.stdout.strip() or "(done)"
    return f"AppleScript error: {r.stderr.strip()[:500]}"


def _gui():
    # pyautogui takes ~1–2 s to import, so only load it when a tool needs it.
    import pyautogui
    return pyautogui


# =====================================================================
# APPLESCRIPT
# =====================================================================

@tool(group="control", confirm="action", timeout=35, code="AppleScript source")
def applescript(code: str) -> str:
    """Run AppleScript — the master key for macOS automation: Messages, Mail, Calendar,
    Reminders, Finder, window management, System Settings, any scriptable app. Use when
    no dedicated tool fits."""
    return _osascript(code.strip())


# =====================================================================
# UI INTROSPECTION (background, no focus steal)
# =====================================================================

@tool(group="system")
def list_running_apps() -> str:
    """List GUI apps currently running."""
    return _osascript('tell application "System Events" to get name of '
                      '(processes where background only is false)')


@tool(group="control", app_name="Process name as shown in the menu bar")
def list_ui_elements(app_name: str, window_index: int = 1) -> str:
    """List the buttons, text fields, checkboxes and menu buttons in an app's window.
    Call before click_element so you know the exact names."""
    kinds = ["button", "text field", "checkbox", "menu button"]
    body = "\n".join(
        f'try\nrepeat with e in (every {k} of window {window_index})\n'
        f'set out to out & "{k}: " & (name of e) & linefeed\nend repeat\nend try'
        for k in kinds
    )
    return _osascript(
        f'tell application "System Events" to tell process {_q(app_name)}\n'
        f'set out to ""\n{body}\nreturn out\nend tell'
    ) or "(no named elements)"


@tool(group="control", confirm="action")
def click_element(app_name: str, element_name: str, window_index: int = 1) -> str:
    """Click a named UI element in an app window without stealing focus,
    e.g. click_element('Spotify', 'Next')."""
    tries = "\n".join(
        f'try\nclick {k} {_q(element_name)} of window {window_index}\nreturn "clicked {k}"\nend try'
        for k in ("button", "checkbox", "menu button", "text field")
    )
    return _osascript(
        f'tell application "System Events" to tell process {_q(app_name)}\n'
        f'{tries}\nreturn "element not found"\nend tell'
    )


@tool(group="system")
def activate_app(name: str) -> str:
    """Bring a running app to the foreground."""
    return _osascript(f"tell application {_q(name)} to activate")


# =====================================================================
# BROWSER JAVASCRIPT
# =====================================================================

@tool(group="control", confirm="action", timeout=25, browser="Which browser's front tab")
def run_js_browser(code: str, browser: Literal["Safari", "Chrome"] = "Safari") -> str:
    """Run JavaScript in the browser's active tab and return the result — read page
    content, fill forms, click links precisely. Safari needs Develop → Allow JavaScript
    from Apple Events."""
    if browser == "Chrome":
        script = f'tell application "Google Chrome" to execute active tab of front window javascript {_q(code)}'
    else:
        script = f'tell application "Safari" to do JavaScript {_q(code)} in document 1'
    return _osascript(script, timeout=20)


# =====================================================================
# KEYBOARD & MOUSE (last resort — acts on whatever has focus)
# =====================================================================

@tool(group="control", confirm="action")
def type_text(text: str) -> str:
    """Type text into whatever currently has keyboard focus."""
    _gui().write(text, interval=0.02)
    return f"Typed {len(text)} chars."


@tool(group="control", confirm="action", keys="e.g. 'enter', 'escape', 'cmd+c', 'cmd+shift+4'")
def press_key(keys: str) -> str:
    """Press a key or key combination."""
    parts = [p.strip().lower() for p in keys.split("+")]
    gui = _gui()
    gui.press(parts[0]) if len(parts) == 1 else gui.hotkey(*parts)
    return f"Pressed {keys}."


@tool(group="control", confirm="action")
def click_at(x: int, y: int) -> str:
    """Click at screen coordinates. Prefer click_element when the control has a name."""
    _gui().click(x, y)
    return f"Clicked at ({x}, {y})."


@tool(group="control", confirm="action")
def scroll(direction: Literal["up", "down"] = "down", amount: int = 5) -> str:
    """Scroll the view under the mouse."""
    _gui().scroll(amount if direction == "up" else -amount)
    return f"Scrolled {direction} by {amount}."
