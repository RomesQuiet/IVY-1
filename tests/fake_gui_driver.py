"""Drives the real app behind the safety net and reports what happened as JSON."""
import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import support

support.install()
report_path, settings_path, phase = sys.argv[1:4]
report = {}


def finish():
    Path(report_path).write_text(json.dumps(report))
    os._exit(0)


threading.Timer(110, lambda: (report.update(error="timed out"), finish())).start()
from ivy import config

config.ALERTS.update(calendar_minutes=None, downloads=None, battery=None, disk_gb=None)
import ivy_gui

ivy_gui.SETTINGS_PATH = Path(settings_path)
holder = {}


class App(ivy_gui.IvyApp):
    def __init__(self):
        super().__init__()
        holder["app"] = self


ivy_gui.IvyApp = App


def on_main(fn):
    from PyObjCTools import AppHelper
    box, done = {}, threading.Event()
    AppHelper.callAfter(lambda: (box.setdefault("r", fn()), done.set()))
    done.wait(8)
    return box.get("r")


def press_hotkey():
    carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
    carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
    event = ctypes.c_void_p()
    carbon.CreateEvent(None, int.from_bytes(b"keyb", "big"), 5, ctypes.c_double(0), 0, ctypes.byref(event))
    return carbon.SendEventToEventTarget(event, ctypes.c_void_p(carbon.GetApplicationEventTarget()))


def drive():
    while "app" not in holder:
        time.sleep(0.1)
    a = holder["app"]
    while not (a.booted.is_set() and a.menu):
        time.sleep(0.2)
    time.sleep(1.2)
    w = a.window
    visible = lambda: on_main(lambda: bool(w.native.isVisible()))
    report["bg"] = w.evaluate_js("JSON.stringify(theme)")
    report["window_colour"] = ivy_gui.window_colour()
    if phase == "first":
        from ivy import menubar
        report["icon"] = on_main(lambda: a.menu.item.button().image() is not None)
        on_main(lambda: menubar.activate(True))
        for _ in range(40):                # the hotkey hides Ivy only while she's the active app
            if on_main(menubar.is_active):
                break
            time.sleep(0.25)
        report["became_active"] = bool(on_main(menubar.is_active))
        on_main(press_hotkey)
        time.sleep(1.2)
        report["hotkey_hides"] = not visible()
        on_main(press_hotkey)
        time.sleep(1.2)
        report["hotkey_shows"] = visible()
        on_main(lambda: w.native.performClose_(None))
        time.sleep(1.2)
        report["red_button_hides"] = not visible()
        on_main(lambda: a.menu.target.show_(None))
        time.sleep(1.2)
        report["menu_shows"] = visible()
        a._on_alert("Test alert.")
        time.sleep(0.4)
        report["alert_in_chat"] = bool(w.evaluate_js("[...document.querySelectorAll('.msg.note')].some(m => m.textContent.includes('Test alert'))"))
        w.evaluate_js("addMsg('assistant','copy this reply'); 1")
        report["selection"] = w.evaluate_js(
            "(() => { const m = document.querySelector('.msg.assistant'); const r = document.createRange();"
            " r.selectNodeContents(m); const s = getSelection(); s.removeAllRanges(); s.addRange(r);"
            " return [getComputedStyle(m).webkitUserSelect, s.toString()].join('|') })()")
        a.open_settings()
        time.sleep(0.8)
        report["settings_open"] = bool(w.evaluate_js("settingsEl.open"))
        w.evaluate_js("(() => { const s = document.getElementById('slider-bl'); s.value = 90;"
                      " s.dispatchEvent(new Event('input')); settingsEl.close(); return 1 })()")
        time.sleep(1)
    finish()


threading.Thread(target=drive, daemon=True).start()
ivy_gui.main()
