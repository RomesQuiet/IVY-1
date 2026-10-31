#!/usr/bin/env python3
"""IVY-1 — GUI entry point. Single source of truth."""

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import webview

from ivy import config, audio

# Loaded in the background by IvyApp._boot so the window appears immediately
# (openai + sounddevice + the tool stack take several seconds to import).
voice = None

UI_PATH = Path(__file__).parent / "ivy" / "ui" / "index.html"
SETTINGS_PATH = config.ROOT / "settings.json"


def code_stamp() -> float:
    """When the code Ivy runs last changed. Skills aren't included: they reload themselves."""
    files = [Path(__file__), *config.ROOT.glob("ivy/*.py"), *config.ROOT.glob("ivy/ui/*.html")]
    return max(f.stat().st_mtime for f in files)


LOADED_CODE = code_stamp()


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def window_colour() -> str:
    """pywebview refuses to open a window unless this is exactly #RRGGBB."""
    colour = load_settings().get("window_bg")
    return colour if isinstance(colour, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", colour) else "#1c1c1e"

# =====================================================================
# LogTee — mirrors print() into the GUI console
# =====================================================================

class LogTee:
    def __init__(self, original, on_line):
        self.original = original
        self.on_line = on_line
        self.buffer = ""
        self._lock = threading.Lock()
        self._depth = 0

    def write(self, text):
        if self._depth > 0:
            # We're inside on_line — write to original only, don't re-emit.
            if self.original:
                try:
                    self.original.write(text)
                except Exception:
                    pass
            return

        self._depth += 1
        try:
            lines = []
            with self._lock:
                if self.original:
                    try:
                        self.original.write(text)
                    except Exception:
                        pass
                self.buffer += text
                for sep in ("\n", "\r"):
                    while sep in self.buffer:
                        line, self.buffer = self.buffer.split(sep, 1)
                        if line.strip():
                            lines.append(line)
            for line in lines:
                try:
                    self.on_line(line)
                except Exception:
                    pass
        finally:
            self._depth -= 1

    def flush(self):
        if self.original:
            try:
                self.original.flush()
            except Exception:
                pass

    def isatty(self):
        return False


# =====================================================================
# App
# =====================================================================

class IvyApp:
    def __init__(self):
        self.brain = None
        self.booted = threading.Event()
        self.window = None
        self.menu = None
        self.visible = True
        self.quitting = False
        self._ready = False
        self._pending = []
        self._js_lock = threading.Lock()

        # chat serialization
        self._chat_lock = threading.Lock()

        # audio state
        self.audio_running = False
        self.audio_thread = None
        self.sleeping = True
        self._audio_lock = threading.Lock()

    # -----------------------------------------------------------------
    # Logging bridge
    # -----------------------------------------------------------------

    def on_log_line(self, line: str):
        if not self._ready:
            self._pending.append(line)
            return
        self._js(f"window.appendConsole({json.dumps(line)})")

    def _flush_pending(self):
        self._ready = True
        while self._pending:
            line = self._pending.pop(0)
            self._js(f"window.appendConsole({json.dumps(line)})")

    def _js(self, code: str):
        if not self.window:
            return
        # evaluate_js waits for the main thread, so calling it from there (a print in the
        # menu-bar code, say, comes through LogTee) would freeze the app for good.
        if threading.current_thread() is threading.main_thread():
            threading.Thread(target=self._js, args=(code,), daemon=True).start()
            return
        with self._js_lock:
            try:
                self.window.evaluate_js(code)
            except Exception:
                pass  # never print — would loop through LogTee

    def _state(self, s):
        self._js(f"setState({json.dumps(s)})")
        if self.menu:
            self.menu.set_state(s)

    # -----------------------------------------------------------------
    # Menu bar, hotkey, window
    # -----------------------------------------------------------------

    def _install_menu(self):
        """Runs on the main thread once the window exists."""
        from ivy import menubar
        self.menu = menubar.MenuBar(self)
        menubar.on_reopen(self.open_latest)
        if menubar.register_hotkey(config.HOTKEY, self.toggle_window):
            print(f"⌨️  {menubar.pretty_hotkey(config.HOTKEY)} shows or hides Ivy from any app.")

    def show_window(self):
        from PyObjCTools import AppHelper
        from ivy import menubar

        def run():
            self.window.show()
            AppHelper.callAfter(menubar.activate, True)
            self._js("document.getElementById('input').focus()")

        self.visible = True
        threading.Thread(target=run, daemon=True).start()

    def open_latest(self):
        """Show Ivy, restarting first if her code has changed. A running Ivy keeps the code
        it started with, and opening the app only wakes it, so nothing else ever updates her."""
        if code_stamp() > LOADED_CODE and not self._chat_lock.locked():
            print("🔄 New code found — restarting")
            self.restart()
        else:
            self.show_window()

    def hide_window(self):
        from PyObjCTools import AppHelper
        from ivy import menubar
        self.visible = False
        threading.Thread(target=self.window.hide, daemon=True).start()
        AppHelper.callAfter(menubar.activate, False)

    def toggle_window(self):
        from ivy import menubar
        if self.visible and menubar.is_active():
            self.hide_window()
        else:
            self.open_latest()

    def toggle_wake(self):
        threading.Thread(target=self.set_wake, args=(not self.audio_running,), daemon=True).start()

    def toggle_mute(self):
        self.set_mute(not config.MUTED)
        self._js(f"window.setMuteUI({json.dumps(config.MUTED)})")

    def _sync_menu(self):
        if self.menu:
            self.menu.set_checks(self.audio_running, config.MUTED)

    def _on_closing(self):
        """The red button hides Ivy to the menu bar; only Quit really exits."""
        if self.quitting:
            return True
        self.hide_window()
        return False

    def quit(self):
        self.quitting = True
        threading.Thread(target=self.window.destroy, daemon=True).start()
        # A slow tool thread must not leave a windowless Ivy behind: a leftover copy
        # would catch the next launch and keep running old code.
        exit_timer = threading.Timer(1.5, os._exit, args=(0,))
        exit_timer.daemon = True
        exit_timer.start()

    def restart(self):
        """Quit, then start a fresh copy once this process is really gone."""
        import AppKit
        bundle = str(AppKit.NSBundle.mainBundle().bundlePath())
        # macOS can still list the old copy for a moment after it exits, and `open` then
        # fails with procNotFound, so keep trying until it takes.
        start = f"for i in 1 2 3 4 5 6 7 8 9 10; do sleep 0.3; open {bundle!r} && break; done" \
            if bundle.endswith("Ivy.app") else f"exec {sys.executable!r} {__file__!r}"
        subprocess.Popen(["/bin/sh", "-c", f"while kill -0 {os.getpid()} 2>/dev/null; "
                                           f"do sleep 0.1; done; {start}"],
                         start_new_session=True)
        self.quit()

    def _watch_code(self):
        """While the window is open, offer the update instead of interrupting."""
        while code_stamp() <= LOADED_CODE:
            time.sleep(5)
        self._js("window.showUpdate()")

    # -----------------------------------------------------------------
    # Proactive alerts
    # -----------------------------------------------------------------

    def _on_alert(self, text: str):
        from ivy import tools, watcher
        print(f"🔔 {text}")
        self._js(f"window.showAlert({json.dumps(text)})")
        try:
            tools.notify("Ivy", text)
        except Exception:
            pass
        talking = self._chat_lock.locked() or (self.audio_running and not self.sleeping)
        if not talking:
            # keep it in the conversation so "what was that?" works
            self.brain.messages.append({"role": "assistant", "content": f"(I alerted Romeu: {text})"})
            if not watcher.in_quiet_hours():
                voice.chime()
                voice.speak_async(text)

    # -----------------------------------------------------------------
    # Startup (background)
    # -----------------------------------------------------------------

    def _boot(self):
        global voice
        t0 = time.time()
        try:
            from ivy import voice as _voice
            from ivy.brain import Brain
            voice = _voice
            self.brain = Brain(confirm=self._confirm)
        except Exception as e:
            print(f"[startup error] {e}")
            self._js(f"window.showError({json.dumps('Startup failed: ' + str(e))})")
            return
        self.booted.set()
        print(f"✅ Ready ({time.time() - t0:.1f}s)")
        self._js("window.onReady && window.onReady()")
        self._check_senses()
        voice.warm_up()   # load the voice + open the Groq connection
        from ivy import mcp_client, senses, watcher
        senses.warm_up()
        watcher.start(self._on_alert)
        threading.Thread(target=self._watch_code, daemon=True, name="code-watch").start()
        mcp_client.load_all()

    def _check_senses(self):
        """Tell Romeu if macOS is keeping Ivy blind, with a one-click fix."""
        from ivy import senses
        missing = [k for k, ok in senses.permissions().items() if not ok]
        if missing:
            self._js(f"window.showPermissions({json.dumps(missing)})")

    def fix_permissions(self):
        from ivy import senses
        senses.request_permissions()
        return {"ok": True}

    def _confirm(self, title: str, detail: str) -> bool:
        """Ask Romeu before a risky tool runs (native macOS dialog)."""
        if not self.window:
            return False
        try:
            return bool(self.window.create_confirmation_dialog(title, detail))
        except Exception as e:
            print(f"[confirm error] {e}")
            return False

    # =================================================================
    # JS → Python
    # =================================================================

    def is_ready(self):
        return self.booted.is_set()

    def get_settings(self):
        return load_settings()

    def save_settings(self, changes: dict):
        """Saved through Python because pywebview's web storage is wiped on every launch."""
        data = {**load_settings(), **changes}
        temp = SETTINGS_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2))
        temp.replace(SETTINGS_PATH)
        return {"ok": True}

    def open_settings(self):
        self.show_window()
        self._js("window.openSettings()")

    def stop(self):
        """Stop button / Esc: cancel the running request and stop talking."""
        if self.brain:
            self.brain.cancel()
        if voice:
            voice.stop_speaking()
        return {"ok": True}

    def send(self, text: str):
        if not text or not text.strip():
            return {"ok": False}
        threading.Thread(
            target=self._do_chat, args=(text.strip(), False), daemon=True
        ).start()
        return {"ok": True}

    def console_cmd(self, text: str):
        text = (text or "").strip()
        if not text:
            return {"ok": True}
        self._js(f"window.appendConsole({json.dumps('> ' + text)}, 'cmd')")
        if text.startswith("/"):
            return self._slash(text)
        threading.Thread(
            target=self._do_chat, args=(text, False), daemon=True
        ).start()
        return {"ok": True}

    def _slash(self, text: str):
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
        if cmd not in ("/help", "/quit", "/settings") and not self.booted.wait(timeout=30):
            print("Still starting up — try again in a moment.")
            return {"ok": False}

        if cmd == "/wake":
            self.set_wake(True)
        elif cmd in ("/sleep", "/text"):
            self.set_wake(False)
        elif cmd == "/reset":
            self.brain.reset()
            print("Conversation cleared.")
        elif cmd == "/memories":
            from ivy.memory import get_store
            mems = get_store().all()
            if not mems:
                print("(no memories yet)")
            for m in mems:
                print(f"[{m['id']}] {m['text']}")
        elif cmd == "/model":
            if not arg or arg == "auto":
                self.brain.force_model = None
                print("Model: auto")
            else:
                self.brain.force_model = arg
                print(f"Model: {arg}")
        elif cmd == "/mute":
            config.MUTED = not config.MUTED
            if config.MUTED:
                voice.stop_speaking()
            print(f"Voice {'muted' if config.MUTED else 'unmuted'}")
        elif cmd == "/settings":
            self._js("window.openSettings()")
        elif cmd == "/doctor":
            from ivy import mcp_client, senses, watcher
            for line in senses.doctor():
                print(line)
            threading.Thread(target=lambda: [print(l) for l in senses.live_check()], daemon=True).start()
            for line in mcp_client.status():
                print(f"MCP — {line}")
            print("Alerts — " + "; ".join(watcher.status()))
            print(self.brain and __import__("ivy.router", fromlist=["get_pool"]).get_pool().status())
        elif cmd == "/help":
            for line in [
                "/wake         — wake-word mode ON",
                "/sleep        — wake-word mode OFF",
                "/reset        — clear conversation history",
                "/memories     — list long-term memories",
                "/model <name> — force a model, or 'auto'",
                "/mute         — toggle voice output",
                "/settings     — colours and themes",
                "/doctor       — what Ivy can see and do right now",
                "/quit         — close IVY-1",
            ]:
                print(line)
        elif cmd == "/quit":
            if self.window:
                self.quit()
        else:
            print(f"Unknown command: {cmd}")
        return {"ok": True}

    def set_wake(self, on: bool):
        with self._audio_lock:
            if on:
                if self.audio_thread and self.audio_thread.is_alive():
                    self.audio_thread.join(timeout=2.0)
                    if self.audio_thread.is_alive():
                        print("⚠️  previous audio loop still running")
                        return {"ok": False}
                self.audio_running = True
                self.sleeping = True
                audio.clear_abort_wake()
                self.audio_thread = threading.Thread(
                    target=self._audio_loop, daemon=True
                )
                self.audio_thread.start()
                self._js("window.setWakeUI(true)")
                self._sync_menu()
                print("🔮 Wake-word mode ON.")
            else:
                if not self.audio_running:
                    return {"ok": True}
                self.audio_running = False
                if voice:
                    voice.stop_speaking()
                audio.abort_wake()
                self._js("window.setWakeUI(false)")
                self._sync_menu()
                print("💤 Wake mode OFF.")
        return {"ok": True}

    def set_model(self, name):
        self.booted.wait()
        self.brain.force_model = name
        print(f"Model: {name or 'auto'}")
        return {"ok": True}

    def set_mute(self, muted: bool):
        config.MUTED = bool(muted)
        if config.MUTED and voice:
            voice.stop_speaking()
        self._sync_menu()
        return {"ok": True}

    # =================================================================
    # Audio loop
    # =================================================================

    def _audio_loop(self):
        self.booted.wait()
        try:
            from ivy.wake import WakeListener
            listener = WakeListener(
                wakeword=getattr(config, "WAKE_WORD", "ok_ivy"),
                threshold=getattr(config, "WAKE_THRESHOLD", 0.5),
            )
        except Exception as e:
            print(f"[wake error] {e}")
            self.audio_running = False
            self._js("window.setWakeUI(false)")
            return

        while self.audio_running:
            try:
                if self.sleeping:
                    woke = listener.wait_for_wake()
                    if not self.audio_running:
                        break
                    if not woke:
                        continue
                    self.sleeping = False
                    voice.chime()
                    self._state("listening")
                    continue

                # AWAKE
                wav = voice.record_until_silence()
                if not self.audio_running:
                    break
                if not wav:
                    continue

                text = voice.transcribe(wav)
                if not text:
                    continue

                print(f"You (voice): {text}")
                self._js(f"window._simulate_user({json.dumps(text)})")

                if voice.should_exit_voice(text):
                    self.sleeping = True
                    self._state("idle")
                    voice.speak_async("Going to sleep.")
                    voice.wait_until_silent()
                    continue

                self._state("thinking")
                self._do_chat(text, from_wake=True)
                if self.audio_running and not self.sleeping:
                    self._state("listening")

            except Exception as e:
                print(f"[audio loop error] {e}")
                time.sleep(1)

    # =================================================================
    # Chat (serialized — one at a time, from any caller)
    # =================================================================

    def _do_chat(self, text: str, from_wake: bool = False):
        if not self.booted.is_set():
            self._js("window.setStatus('⏳ Starting up — your message will run in a moment…')")
            self.booted.wait()
        with self._chat_lock:
            try:
                # Stop any ongoing speech so replies don't overlap
                voice.stop_speaking()
                if self.menu:
                    self.menu.set_state("thinking")

                def on_token(t):
                    self._js(f"window.appendToken({json.dumps(t)})")

                from ivy.toolkit import describe_call

                def on_tool(name, args):
                    detail = args.get("command") or args.get("code") or ""
                    self._js(f"window.showTool({json.dumps(name)}, "
                             f"{json.dumps(describe_call(name, args))}, {json.dumps(detail[:2000])})")

                def on_tool_result(name, args, result):
                    ok = not re.match(r"(\w+ failed:|NOT DONE|Unknown tool|Missing required|Stop:|"
                                      r"AppleScript error|exit \d+)", result)
                    self._js(f"window.toolDone({json.dumps(name)}, {json.dumps(ok)}, "
                             f"{json.dumps(result[:600])})")

                def on_status(msg):
                    self._js(f"window.setStatus({json.dumps(msg)})")

                self._js("window.beginMessage()")
                reply = self.brain.chat(
                    text,
                    on_token=on_token,
                    on_tool=on_tool,
                    on_tool_result=on_tool_result,
                    on_status=on_status,
                    on_sentence=voice.speak_async,  # always speak
                )
                self._js("window.endMessage()")
                if self.menu:
                    self.menu.set_state("idle")

                if reply:
                    print(f"IVY-1: {reply}")

                # In wake mode, listen for "stop"/barge-in while she talks. When typing,
                # don't block: the next message simply cuts the speech off.
                if reply and from_wake and voice.is_speaking():
                    if voice.monitor_for_bargein():
                        print("  🔇 (interrupted — go ahead)")
            except Exception as e:
                print(f"[chat error] {e}")
                self._js(f"window.showError({json.dumps(str(e))})")
                self._js("window.endMessage()")
                if self.menu:
                    self.menu.set_state("idle")


# =====================================================================
# main
# =====================================================================

def main():
    app = IvyApp()

    # Install the log tee BEFORE the window exists so nothing is lost
    sys.stdout = LogTee(sys.__stdout__, app.on_log_line)
    sys.stderr = LogTee(sys.__stderr__, app.on_log_line)

    window = webview.create_window(
        "IVY-1",
        str(UI_PATH),
        js_api=app,
        width=900,
        height=680,
        min_size=(640, 500),
        background_color=window_colour(),
        text_select=True,      # pywebview blocks selecting and copying text by default
        confirm_close=False,
    )
    app.window = window

    def on_loaded():
        from PyObjCTools import AppHelper
        app._flush_pending()
        if not app.menu:
            AppHelper.callAfter(app._install_menu)

    window.events.loaded += on_loaded
    window.events.closing += app._on_closing

    threading.Thread(target=app._boot, daemon=True, name="boot").start()
    webview.start(debug=False)


if __name__ == "__main__":
    main()
