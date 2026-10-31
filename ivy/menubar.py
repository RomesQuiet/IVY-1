# ivy/menubar.py
"""
Ivy in the menu bar, plus a global hotkey that works from any app.

The hotkey uses Carbon's RegisterEventHotKey because, unlike an event monitor, it
needs no Accessibility permission. Everything here must run on the main thread.
"""

import ctypes

import AppKit
import objc
from Foundation import NSAppleEventManager, NSObject
from PyObjCTools import AppHelper

from . import config

_MODIFIERS = {"cmd": 0x100, "command": 0x100, "shift": 0x200, "option": 0x800, "alt": 0x800,
              "ctrl": 0x1000, "control": 0x1000}
_KEYS = {"space": 49, "return": 36, "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
         "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "o": 31,
         "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46}
_SYMBOLS = {"cmd": "⌘", "command": "⌘", "shift": "⇧", "option": "⌥", "alt": "⌥", "ctrl": "⌃",
            "control": "⌃", "space": "Space", "return": "↩"}
_ICONS = {"idle": "sparkles", "listening": "ear", "thinking": "ellipsis.circle", "speaking": "waveform"}

_K_EVENT_CLASS_KEYBOARD = int.from_bytes(b"keyb", "big")
_K_EVENT_HOTKEY_PRESSED = 5
_keep_alive = []     # ctypes callbacks and Cocoa objects that must outlive this module's calls


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


def pretty_hotkey(combo: str) -> str:
    return "".join(_SYMBOLS.get(p, p.upper()) for p in combo.lower().split("+"))


def register_hotkey(combo: str, callback) -> bool:
    """Call `callback` whenever `combo` (e.g. 'option+space') is pressed in any app."""
    *mods, key = combo.lower().split("+")
    if key not in _KEYS or any(m not in _MODIFIERS for m in mods):
        print(f"⚠️  Can't use hotkey '{combo}'. Try something like option+space.")
        return False
    carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
    carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
    target = ctypes.c_void_p(carbon.GetApplicationEventTarget())

    def on_event(_call, _event, _data):
        try:
            callback()
        except Exception as e:
            print(f"[hotkey error] {e}")
        return 0

    handler = _HANDLER(on_event)
    spec = _EventTypeSpec(_K_EVENT_CLASS_KEYBOARD, _K_EVENT_HOTKEY_PRESSED)
    handler_ref, hotkey_ref = ctypes.c_void_p(), ctypes.c_void_p()
    err = carbon.InstallEventHandler(target, handler, 1, ctypes.byref(spec), None,
                                     ctypes.byref(handler_ref))
    if not err:
        err = carbon.RegisterEventHotKey(_KEYS[key], sum({_MODIFIERS[m] for m in mods}),
                                         _EventHotKeyID(int.from_bytes(b"IVY1", "big"), 1),
                                         target, 0, ctypes.byref(hotkey_ref))
    if err:
        print(f"⚠️  Couldn't register the hotkey {pretty_hotkey(combo)} (error {err}).")
        return False
    _keep_alive.append((handler, handler_ref, hotkey_ref))
    return True


class _MenuTarget(NSObject):
    def initWithApp_(self, app):
        self = objc.super(_MenuTarget, self).init()
        if self is not None:
            self.app = app
        return self

    def show_(self, _sender):
        self.app.show_window()

    def settings_(self, _sender):
        self.app.open_settings()

    def toggleWake_(self, _sender):
        self.app.toggle_wake()

    def toggleMute_(self, _sender):
        self.app.toggle_mute()

    def quit_(self, _sender):
        self.app.quit()


class MenuBar:
    def __init__(self, app):
        """`app` provides show_window, open_settings, toggle_wake, toggle_mute and quit."""
        self.target = _MenuTarget.alloc().initWithApp_(app)
        self.item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength)
        menu = AppKit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        add = lambda title, action: self._add(menu, title, action)   # noqa: E731
        add(f"Show Ivy    {pretty_hotkey(config.HOTKEY)}", "show:")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self.wake_item = add("Listen for “OK Ivy”", "toggleWake:")
        self.mute_item = add("Mute voice", "toggleMute:")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        add("Settings…", "settings:")
        add("Quit Ivy", "quit:")
        self.item.setMenu_(menu)
        self.set_state("idle")
        _keep_alive.append(self)

    def _add(self, menu, title, action):
        entry = menu.addItemWithTitle_action_keyEquivalent_(title, action, "")
        entry.setTarget_(self.target)
        return entry

    def set_state(self, state: str):
        def apply():
            icon = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                _ICONS.get(state, _ICONS["idle"]), f"Ivy: {state}")
            if icon is None:
                self.item.button().setTitle_("Ivy")
                return
            icon.setTemplate_(True)
            self.item.button().setImage_(icon)
        AppHelper.callAfter(apply)

    def set_checks(self, wake: bool, muted: bool):
        def apply():
            self.wake_item.setState_(int(wake))
            self.mute_item.setState_(int(muted))
        AppHelper.callAfter(apply)


class _ReopenHandler(NSObject):
    def initWithCallback_(self, callback):
        self = objc.super(_ReopenHandler, self).init()
        if self is not None:
            self.callback = callback
        return self

    @objc.signature(b"v@:@@")
    def handleReopen_withReply_(self, _event, _reply):
        self.callback()


def on_reopen(callback):
    """Run `callback` when the app is opened again while already running (Finder, Spotlight,
    the Dock). Without this a hidden Ivy ignores it and seems not to open at all."""
    handler = _ReopenHandler.alloc().initWithCallback_(callback)
    NSAppleEventManager.sharedAppleEventManager() \
        .setEventHandler_andSelector_forEventClass_andEventID_(
            handler, "handleReopen:withReply:", int.from_bytes(b"aevt", "big"), int.from_bytes(b"rapp", "big"))
    _keep_alive.append(handler)


def activate(foreground: bool):
    """A Dock icon and focus while the window is open; only the menu-bar icon while hidden."""
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular if foreground
                             else AppKit.NSApplicationActivationPolicyAccessory)
    if foreground:
        app.activateIgnoringOtherApps_(True)


def is_active() -> bool:
    return bool(AppKit.NSApplication.sharedApplication().isActive())
