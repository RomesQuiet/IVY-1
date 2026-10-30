# ivy/tools_mac.py
"""macOS control layer — the eyes and hands of IVY-1: reading the screen, AppleScript,
UI elements, browser JS, keyboard/mouse. Everything that changes what's on screen asks
first (see config.ACTION_MODE)."""

import base64
import subprocess
import time
from typing import Literal

from . import config, senses
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
    # Without Accessibility macOS silently drops synthetic input, so a click would
    # "succeed" while doing nothing. Refuse instead of pretending.
    senses.require_accessibility()
    # pyautogui takes ~1–2 s to import, so only load it when a tool needs it.
    import pyautogui
    return pyautogui


def _focus(app: str = ""):
    """Bring the target forward. Ivy's own window is often in front, and keys sent
    now would land in her chat box instead of the app."""
    from .tools import resolve_app
    wins = senses.windows()
    target = (resolve_app(app) or app) if app else next((w["app"] for w in wins if w["app"] != "Ivy"), "")
    if target and (not wins or wins[0]["app"] != target):
        _osascript(f"tell application {_q(target)} to activate")
        time.sleep(0.4)


# =====================================================================
# APPLESCRIPT
# =====================================================================

@tool(confirm="action", timeout=35, code="AppleScript source")
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
# EYES
# =====================================================================

@tool(untrusted=True, ack="Taking a look.", timeout=45,
      app="An app's name to read its window (even behind others), 'screen' for the whole "
          "display, or empty for the front window")
def read_screen(app: str = "") -> str:
    """Read the text on screen with its (x, y) position, using on-device OCR. Nothing leaves
    this Mac. Use it to see what's really there, before and after acting."""
    blocks, region = senses.read_text(app)
    if not blocks:
        return f"No readable text in {region['app']}."
    lines = [f"({b['x']},{b['y']}) {b['text']}" for b in blocks[:140]]
    extra = f"\n…({len(blocks)} text blocks in total)" if len(blocks) > 140 else ""
    return f"{region['app']} ({region['w']:.0f}×{region['h']:.0f} at {region['x']:.0f},{region['y']:.0f}):\n" \
        + "\n".join(lines) + extra


@tool(confirm="vision", untrusted=True, ack="Let me look.", timeout=45,
      question="What you want to know about what's on screen",
      app="An app's name, 'screen' for the whole display, or empty for the front window")
def see_screen(question: str, app: str = "") -> str:
    """Actually look at the screen with a vision model: pictures, layout, colours, video, charts,
    "what am I looking at". Uploads a screenshot to Groq, so use read_screen when text is enough."""
    if config.CLOUD_VISION == "off":
        return "Cloud vision is switched off (CLOUD_VISION in config.py). Use read_screen."
    from openai import OpenAI
    jpeg, region = senses.screenshot_jpeg(app)
    client = OpenAI(base_url=config.GROQ_BASE_URL, api_key=config.GROQ_API_KEY, timeout=40, max_retries=0)
    r = client.chat.completions.create(
        model=config.MODEL_VISION,
        max_tokens=350,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": f"This is a screenshot of {region['app']} on a Mac. {question} "
                                     "Answer briefly and only from what is visible."},
            {"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
        ]}],
    )
    return r.choices[0].message.content or "The vision model returned nothing."


@tool(group="control", confirm="action", timeout=45, text="The visible words to click, e.g. 'Sign in'",
      app="App whose window to look in; empty for the front window",
      nth="Which match, if the words appear more than once")
def click_text(text: str, app: str = "", nth: int = 1) -> str:
    """Find words on screen and click them. Works in any app, including ones that aren't
    scriptable. The window is brought forward first."""
    gui = _gui()
    _focus(app)
    blocks, region = senses.read_text(app)
    hit = senses.find_text(blocks, text, nth)
    if not hit:
        return f"NOT DONE: can't see '{text}' in {region['app']}. Use read_screen to see what's there."
    gui.click(hit["x"], hit["y"])
    return f"Clicked '{hit['text'][:60]}' at ({hit['x']}, {hit['y']}) in {region['app']}."


@tool(group="control", untrusted=True, timeout=35, seconds="How long to wait, at most 30")
def wait_for_text(text: str, seconds: int = 10, app: str = "") -> str:
    """Wait until words appear on screen, e.g. while a page loads or a download finishes."""
    t0 = time.time()
    while time.time() - t0 < min(seconds, 30):
        blocks, region = senses.read_text(app)
        if senses.find_text(blocks, text):
            return f"'{text}' is on screen in {region['app']} after {time.time() - t0:.0f}s."
        time.sleep(0.7)
    return f"NOT FOUND: '{text}' did not appear within {seconds}s."


# =====================================================================
# KEYBOARD & MOUSE (acts on whatever has focus)
# =====================================================================

@tool(group="control", confirm="action", app="App to type into; empty = the front window")
def type_text(text: str, app: str = "") -> str:
    """Type text (any language, accents included) into the app's focused field."""
    import Quartz
    _gui()
    _focus(app)
    for i in range(0, len(text), 16):
        chunk = text[i:i + 16]
        units = len(chunk.encode("utf-16-le")) // 2
        for pressed in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, pressed)
            Quartz.CGEventKeyboardSetUnicodeString(event, units, chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.012)
    return f"Typed {len(text)} chars. Use read_screen to confirm they landed."


@tool(group="control", confirm="action", keys="e.g. 'enter', 'escape', 'cmd+c', 'cmd+shift+4'",
      app="App to send them to; empty = the front window")
def press_key(keys: str, app: str = "") -> str:
    """Press a key or key combination."""
    parts = [p.strip().lower() for p in keys.split("+")]
    gui = _gui()
    _focus(app)
    gui.press(parts[0]) if len(parts) == 1 else gui.hotkey(*parts)
    return f"Pressed {keys}."


@tool(group="control", confirm="action")
def click_at(x: int, y: int) -> str:
    """Click at screen coordinates from read_screen. Prefer click_text."""
    _gui().click(x, y)
    return f"Clicked at ({x}, {y})."


@tool(group="control", confirm="action")
def scroll(direction: Literal["up", "down"] = "down", amount: int = 5) -> str:
    """Scroll the view under the mouse."""
    _gui().scroll(amount if direction == "up" else -amount)
    return f"Scrolled {direction} by {amount}."
