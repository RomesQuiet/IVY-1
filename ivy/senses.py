# ivy/senses.py
"""
Ivy's senses: what is really happening on this Mac, so she checks instead of assuming.

  snapshot()  cheap live state: front app, window, browser tab, music, volume
  diff()      what changed between two snapshots (used to verify every action)
  read_text() on-device OCR of a window or the screen, with click coordinates
  see()       a screenshot for the vision model

macOS gates all of this behind two permissions, which belong to whatever launches
Ivy (Terminal, Python or Ivy.app). Without them she must say so, not pretend:
  Screen Recording  window titles, screenshots, OCR
  Accessibility     clicking, typing, key presses
"""

import ctypes
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_CHROMIUM = {"Brave Browser", "Google Chrome", "Microsoft Edge", "Arc", "Chromium", "Vivaldi", "Opera"}
_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_"
_SCREEN_HELP = ("Ivy can't see the screen: macOS Screen Recording permission is off for the app "
                "that launched her. Turn it on in System Settings → Privacy & Security → Screen "
                "Recording, then restart Ivy.")
_AX_HELP = ("Ivy can't click or type: macOS Accessibility permission is off for the app that "
            "launched her. Turn it on in System Settings → Privacy & Security → Accessibility, "
            "then restart Ivy.")


# =====================================================================
# Permissions
# =====================================================================

def _ax_lib():
    lib = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    lib.AXIsProcessTrusted.restype = ctypes.c_bool
    return lib


def permissions() -> dict:
    import Quartz
    return {"screen": bool(Quartz.CGPreflightScreenCaptureAccess()),
            "accessibility": bool(_ax_lib().AXIsProcessTrusted())}


def request_permissions() -> dict:
    """Show macOS's own prompts and open the settings panes for whatever is missing."""
    import Quartz
    have = permissions()
    if not have["screen"]:
        Quartz.CGRequestScreenCaptureAccess()
        subprocess.Popen(["open", _SETTINGS + "ScreenCapture"])
    if not have["accessibility"]:
        try:
            import objc
            from Foundation import NSDictionary
            lib = _ax_lib()
            lib.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
            opts = NSDictionary.dictionaryWithObject_forKey_(True, "AXTrustedCheckOptionPrompt")
            lib.AXIsProcessTrustedWithOptions(objc.pyobjc_id(opts))
        except Exception:
            pass
        subprocess.Popen(["open", _SETTINGS + "Accessibility"])
    return have


def doctor() -> list[str]:
    """Plain-English report of what Ivy can and can't perceive right now."""
    p = permissions()
    lines = [
        "Screen Recording: " + ("on — she can read and see the screen" if p["screen"] else
                                "OFF — she can't read the screen or see window titles"),
        "Accessibility: " + ("on — she can click and type" if p["accessibility"] else
                             "OFF — she can't click, type or press keys"),
    ]
    try:
        import Vision  # noqa: F401
        lines.append("On-device OCR: available")
    except ImportError:
        lines.append("On-device OCR: MISSING — pip install -r requirements.txt")
    return lines


def live_check() -> list[str]:
    """Actually try each sense, for /doctor. Reports counts, never screen contents."""
    lines = []
    if permissions()["screen"]:
        t0 = time.time()
        try:
            blocks, region = read_text()
            lines.append(f"Live test: read {len(blocks)} pieces of text from {region['app']} "
                         f"in {time.time() - t0:.1f}s")
        except Exception as e:
            lines.append(f"Live test: screen reading FAILED — {e}")
        titled = sum(1 for w in windows() if w["title"])
        lines.append(f"Live test: {titled} of {len(windows())} window titles visible")
    else:
        lines.append("Live test: skipped screen reading (Screen Recording is off)")
    from .tools import default_browser
    browser = default_browser()
    if any(w["app"] == browser for w in windows()):
        readable = bool(browser_tab(browser)[1])
        lines.append(f"Live test: {browser} tab " + ("readable" if readable else
                     "NOT readable — allow Automation for it in System Settings → Privacy & Security"))
    return lines


def require_screen():
    if not permissions()["screen"]:
        raise PermissionError(_SCREEN_HELP)


def require_accessibility():
    if not permissions()["accessibility"]:
        raise PermissionError(_AX_HELP)


# =====================================================================
# Live state
# =====================================================================

def _osa(script: str, timeout: float = 2.0) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _running(process: str) -> bool:
    return subprocess.run(["pgrep", "-x", process], capture_output=True).returncode == 0


def windows() -> list[dict]:
    """Visible app windows, front to back. Titles need Screen Recording."""
    import Quartz
    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    out = []
    for w in infos:
        bounds = w.get("kCGWindowBounds") or {}
        if w.get("kCGWindowLayer", 1) != 0 or bounds.get("Height", 0) < 60 or bounds.get("Width", 0) < 100:
            continue
        pid = w.get("kCGWindowOwnerPID")
        out.append({
            "id": w.get("kCGWindowNumber"),
            "app": "Ivy" if pid == os.getpid() else w.get("kCGWindowOwnerName", "?"),
            "title": w.get("kCGWindowName") or "",
            "x": bounds.get("X", 0), "y": bounds.get("Y", 0),
            "w": bounds.get("Width", 0), "h": bounds.get("Height", 0),
        })
    return out


def browser_tab(app: str) -> tuple[str, str]:
    """(title, url) of the browser's active tab, or ('', '') if unreadable."""
    if app == "Safari":
        script = 'tell application "Safari" to return (name of current tab of front window) & linefeed & (URL of current tab of front window)'
    elif app in _CHROMIUM:
        script = (f'tell application "{app}" to return (title of active tab of front window) & linefeed '
                  f'& (URL of active tab of front window)')
    else:
        return "", ""
    title, _, url = _osa(script).partition("\n")
    return title, url


_last_media = ""       # Spotify can take ~1 s to answer; the last reading stays useful


def _media() -> str:
    global _last_media
    _last_media = ""
    for app, process in (("Spotify", "Spotify"), ("Music", "Music")):
        if _running(process):
            info = _osa(f'tell application "{app}" to return (player state as string) & "|" & '
                        f'(name of current track) & "|" & (artist of current track)')
            if info.count("|") >= 2:
                state, name, artist = info.split("|", 2)
                _last_media = f"{app} {state}: {name} by {artist}"
                break
    return _last_media


class _Addr(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode(), "big")


_coreaudio = None


def _audio():
    """Default output device + the CoreAudio library. `osascript`'s 'get volume settings'
    takes ~4 s on this Mac; CoreAudio answers in about a millisecond once loaded."""
    global _coreaudio
    if _coreaudio is None:
        _coreaudio = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    device, size = ctypes.c_uint32(), ctypes.c_uint32(4)
    addr = _Addr(_fourcc("dOut"), _fourcc("glob"), 0)
    if _coreaudio.AudioObjectGetPropertyData(1, ctypes.byref(addr), 0, None, ctypes.byref(size),
                                             ctypes.byref(device)):
        raise OSError("no default output device")
    return _coreaudio, device


def get_volume() -> tuple[int, bool]:
    """(level 0–100, muted) of the current output device."""
    ca, device = _audio()
    level, muted, size = ctypes.c_float(), ctypes.c_uint32(), ctypes.c_uint32(4)
    addr = _Addr(_fourcc("vmvc"), _fourcc("outp"), 0)
    if ca.AudioObjectGetPropertyData(device, ctypes.byref(addr), 0, None, ctypes.byref(size),
                                     ctypes.byref(level)):
        raise OSError("this output device has no volume control")
    addr = _Addr(_fourcc("mute"), _fourcc("outp"), 0)
    ca.AudioObjectGetPropertyData(device, ctypes.byref(addr), 0, None, ctypes.byref(size),
                                  ctypes.byref(muted))
    return round(level.value * 100), bool(muted.value)


def set_volume(level: int) -> tuple[int, bool]:
    """Set the volume (0 mutes) and read it back."""
    ca, device = _audio()
    scalar = ctypes.c_float(max(0, min(100, level)) / 100)
    addr = _Addr(_fourcc("vmvc"), _fourcc("outp"), 0)
    if level > 0 and ca.AudioObjectSetPropertyData(device, ctypes.byref(addr), 0, None, 4,
                                                   ctypes.byref(scalar)):
        raise OSError("couldn't change the volume on this output device")
    mute = ctypes.c_uint32(1 if level <= 0 else 0)
    addr = _Addr(_fourcc("mute"), _fourcc("outp"), 0)
    ca.AudioObjectSetPropertyData(device, ctypes.byref(addr), 0, None, 4, ctypes.byref(mute))
    return get_volume()


def _volume() -> str:
    level, muted = get_volume()
    return "muted" if muted else f"{level}%"


def _glance() -> dict:
    """The instant part of the state (no AppleScript, a few ms)."""
    others = [w for w in windows() if w["app"] != "Ivy"]
    front = others[0] if others else {"app": "", "title": ""}
    return {"front": front["app"], "title": front["title"], "windows": len(others),
            "apps": list(dict.fromkeys(w["app"] for w in others))}


def snapshot(budget: float = 1.2) -> dict:
    """What's happening right now. A source too slow to answer becomes None — unknown,
    which is different from "" (nothing there) and is never reported as a change."""
    state = _glance() | {"tab": None, "url": None, "media": None, "volume": None}
    pool = ThreadPoolExecutor(3)
    jobs = {"tab": pool.submit(browser_tab, state["front"]), "media": pool.submit(_media),
            "volume": pool.submit(_volume)}
    deadline = time.time() + budget
    for key, job in jobs.items():
        try:
            state[key] = job.result(timeout=max(0.05, deadline - time.time()))
        except Exception:
            pass
    pool.shutdown(wait=False)
    if isinstance(state["tab"], tuple):
        state["tab"], state["url"] = state["tab"]
    return state


def summary(s: dict) -> str:
    """A few lines for the system prompt."""
    lines = []
    if s["front"]:
        head = f"Front app: {s['front']}"
        name = s["tab"] or s["title"]
        if name:
            head += f" — \"{name[:90]}\""
        if s["url"]:
            head += f" ({s['url'][:90]})"
        lines.append(head)
    rest = [a for a in s["apps"] if a != s["front"]]
    if rest:
        lines.append("Also on screen: " + ", ".join(rest[:6]))
    media = s["media"] if s["media"] is not None else _last_media
    if media:
        lines.append(media[:100])
    if s["volume"]:
        lines.append(f"Volume: {s['volume']}")
    if not s["title"] and not s["tab"]:
        lines.append("(Window titles are hidden: Screen Recording permission is off.)")
    return "\n".join(lines)


def diff(before: dict, after: dict) -> list[str]:
    """Human-readable list of what changed. Unknown (None) values prove nothing."""
    def moved(key):
        return before.get(key) is not None and after.get(key) is not None and before[key] != after[key]

    out = []
    if moved("front"):
        out.append(f"front app {before['front'] or 'nothing'} → {after['front'] or 'nothing'}")
    if moved("url") and after["url"]:
        out.append(f"browser now at {after['url'][:100]}")
    if moved("tab") and after["tab"]:
        out.append(f"tab title \"{after['tab'][:80]}\"")
    elif moved("title") and after["title"]:
        out.append(f"window title \"{after['title'][:80]}\"")
    if moved("windows"):
        out.append(f"visible windows {before['windows']} → {after['windows']}")
    if moved("media"):
        out.append(after["media"][:100] or "music stopped")
    if moved("volume") and after["volume"]:
        out.append(f"volume {after['volume']}")
    return out


def wait_for_change(before: dict, timeout: float = 1.5) -> tuple[dict, list[str]]:
    """Watch the window list until something moves (apps and pages take a moment),
    then take one full reading and report what changed."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if diff(before, _glance() | {"tab": None, "url": None, "media": None, "volume": None}):
            time.sleep(0.35)                # let the new window's title settle
            break
        time.sleep(0.15)
    after = snapshot()
    return after, diff(before, after)


def wait_for_app(name: str, timeout: float = 3.0) -> bool:
    """True once the app has a visible window."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if any(w["app"] == name for w in windows()):
            return True
        time.sleep(0.2)
    return False


def warm_up():
    """Load the slow frameworks once, at startup, so later reads take milliseconds."""
    try:
        windows()
        get_volume()
        import Vision  # noqa: F401
        snapshot()
    except Exception as e:
        print(f"  (senses warm-up: {e})")


# =====================================================================
# Eyes
# =====================================================================

def _find_window(app: str) -> dict | None:
    wins = [w for w in windows() if w["app"] != "Ivy"]
    if app:
        wins = [w for w in wins if app.lower() in w["app"].lower()]
    return wins[0] if wins else None


def capture(app: str = "") -> tuple[Path, dict]:
    """Screenshot → (png path, region in screen points).
    app='' = the front window that isn't Ivy's; app='screen' = the whole main display."""
    require_screen()
    path = Path(tempfile.gettempdir()) / f"ivy_eyes_{os.getpid()}.png"
    win = None if app == "screen" else _find_window(app)
    if app and app != "screen" and not win:
        raise RuntimeError(f"no visible window for '{app}'")
    if win:
        cmd = ["screencapture", "-x", "-o", "-l", str(win["id"]), str(path)]
        region = {"x": win["x"], "y": win["y"], "w": win["w"], "h": win["h"], "app": win["app"]}
    else:
        import AppKit
        frame = AppKit.NSScreen.mainScreen().frame()
        cmd = ["screencapture", "-x", "-m", str(path)]
        region = {"x": 0, "y": 0, "w": frame.size.width, "h": frame.size.height, "app": "screen"}
    subprocess.run(cmd, check=True, timeout=10)
    return path, region


def ocr(path: Path, region: dict) -> list[dict]:
    """On-device text recognition → [{text, x, y}] in screen points, top to bottom."""
    import Vision
    from Foundation import NSURL
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
        NSURL.fileURLWithPath_(str(path)), None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    # Accurate takes ~5 s here but read 9/9 phrases of real dark-theme UI; "fast" takes
    # 0.2 s and got 0–2/9. A misread screen is worse than a slow one.
    request.setRecognitionLevel_(0)
    request.setUsesLanguageCorrection_(True)
    request.setRecognitionLanguages_(["en-US", "pt-BR"])
    handler.performRequests_error_([request], None)
    found = []
    for obs in request.results() or []:
        box = obs.boundingBox()
        found.append({
            "text": obs.topCandidates_(1)[0].string(),
            "x": int(region["x"] + (box.origin.x + box.size.width / 2) * region["w"]),
            "y": int(region["y"] + (1 - box.origin.y - box.size.height / 2) * region["h"]),
            "w": box.size.width * region["w"],
        })
    return sorted(found, key=lambda b: (b["y"] // 12, b["x"]))


def read_text(app: str = "") -> tuple[list[dict], dict]:
    path, region = capture(app)
    try:
        return ocr(path, region), region
    finally:
        path.unlink(missing_ok=True)


def find_text(blocks: list[dict], text: str, nth: int = 1) -> dict | None:
    """Locate words on screen → a block with the point to click, exact matches first."""
    want = " ".join(text.lower().split())
    exact = [b for b in blocks if b["text"].lower().strip() == want]
    partial = [b for b in blocks if want in b["text"].lower() and b not in exact]
    hits = exact + partial
    if len(hits) < nth:
        return None
    b = dict(hits[nth - 1])
    low = b["text"].lower()
    if low.strip() != want and low:     # aim at the words inside a longer line
        start = low.index(want)
        mid = (start + len(want) / 2) / len(low)
        b["x"] = int(b["x"] - b["w"] / 2 + mid * b["w"])
    return b


def screenshot_jpeg(app: str = "", width: int = 1280) -> tuple[bytes, dict]:
    """A downscaled JPEG for the vision model (~1.8k tokens at this size)."""
    import io
    from PIL import Image
    path, region = capture(app)
    try:
        img = Image.open(path).convert("RGB")
        if img.width > width:
            img = img.resize((width, int(img.height * width / img.width)))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=72)
        return out.getvalue(), region
    finally:
        path.unlink(missing_ok=True)
