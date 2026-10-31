# ivy/browser.py
"""
Ivy's own browser: websites driven through their real structure, not pixels.

Chromium refuses remote debugging on a person's main profile, and that's the right way
round anyway: Ivy gets a separate Brave profile and a separate window, with only the
logins Romeu chooses to give it. Everyday browsing stays out of reach.

browser_read lists a page's buttons, links and fields with numbers; the action tools use
those numbers. Passwords and card numbers are never typed, and clicks that buy, delete
or send always ask (see click_risk), whatever the approval settings.
"""

import json
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import config
from .toolkit import tool

_BINARIES = ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
             "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
_lock = threading.RLock()
_session = None

_SENSITIVE_FIELD = re.compile(r"pass(word|code|wd)|card|cvv|cvc|ccnum|cc-|iban|ssn|nif|security.?code|one-time|otp", re.I)
_RISKY_CLICK = re.compile(
    r"\b(buy|pay|purchase|checkout|check out|place (your )?order|order now|confirm|subscribe|"
    r"delete|remove|erase|send|transfer|publish|post|submit|sign out|log ?out|close account)\b", re.I)

_SNAPSHOT = r"""(() => {
  const seen = [];
  const pick = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],'
             + '[role=tab],[role=menuitem],[role=checkbox],[contenteditable=true],[onclick]';
  let n = 0;
  for (const e of document.querySelectorAll(pick)) {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    if (!r.width || !r.height || s.visibility === 'hidden' || s.display === 'none' || e.disabled) continue;
    e.setAttribute('data-ivy-ref', ++n);
    const tag = e.tagName.toLowerCase();
    const kind = e.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'input' ? (e.type || 'text') : tag);
    const field = tag === 'input' || tag === 'textarea';
    const label = (e.getAttribute('aria-label') || (field ? '' : e.innerText) || e.placeholder || e.name
                   || e.title || e.value || '').trim().replace(/\s+/g, ' ').slice(0, 70);
    const value = field && e.value && e.type !== 'password' && !['submit', 'button', 'checkbox', 'radio'].includes(e.type)
      ? ` = "${e.value.slice(0, 60)}"` : '';
    const opts = tag === 'select' ? ' options: ' + [...e.options].slice(0, 12).map(o => o.text.trim()).join(' | ') : '';
    seen.push(`[${n}] ${kind} "${label}"${value}${opts}`);
    if (n >= 90) break;
  }
  const text = (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 2800);
  return JSON.stringify({title: document.title, url: location.href, items: seen, text});
})()"""


class Session:
    """One DevTools connection to one tab."""

    def __init__(self, ws_url: str):
        import websocket
        self.ws = websocket.create_connection(ws_url, timeout=12, suppress_origin=True)
        self.url, self.next_id = ws_url, 0

    def send(self, method: str, **params) -> dict:
        self.next_id += 1
        self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:
            reply = json.loads(self.ws.recv())
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise RuntimeError(reply["error"].get("message", "browser error"))
                return reply.get("result", {})

    def run(self, expression: str):
        result = self.send("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("text", "script failed"))
        return result["result"].get("value")


def _api(path: str, method: str = "GET"):
    request = urllib.request.Request(f"http://127.0.0.1:{config.BROWSER_PORT}/json{path}", method=method)
    with urllib.request.urlopen(request, timeout=2) as r:
        return json.load(r)


def _pages() -> list[dict]:
    return [t for t in _api("/list") if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]


def _launch():
    binary = next((b for b in _BINARIES if Path(b).exists()), None)
    if not binary:
        raise RuntimeError("Ivy's browser needs Brave or Chrome installed.")
    config.BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    args = [binary, f"--remote-debugging-port={config.BROWSER_PORT}", "--remote-allow-origins=http://127.0.0.1",
            f"--user-data-dir={config.BROWSER_PROFILE}", "--no-first-run", "--no-default-browser-check", "about:blank"]
    if config.BROWSER_HEADLESS:
        args.insert(1, "--headless=new")
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    end = time.time() + 30
    while time.time() < end:
        try:
            if _pages():
                return
        except OSError:
            pass
        time.sleep(0.3)
    raise RuntimeError("Ivy's browser didn't start.")


def _tab() -> Session:
    """The current tab, starting the browser or following a newly opened tab as needed."""
    global _session
    try:
        pages = _pages()
    except OSError:
        _launch()
        pages, _session = _pages(), None
    newest = pages[0]["webSocketDebuggerUrl"]       # DevTools lists the most recent tab first
    if _session is None or _session.url != newest:
        _session = Session(newest)
        _session.send("Page.bringToFront")
    return _session


def _settle(tab: Session, was_url: str = ""):
    """Wait for navigation or a page load to finish."""
    end = time.time() + 10
    time.sleep(0.35)
    while time.time() < end:
        try:
            if tab.run("document.readyState") == "complete":
                break
        except Exception:
            pass
        time.sleep(0.2)
    time.sleep(0.25)        # scripts often fill the page just after load


def _where(tab: Session) -> str:
    return f"Now at {tab.run('location.href')} — \"{tab.run('document.title')}\". Use browser_read to see it."


def _element(tab: Session, ref: int, expression: str):
    found = tab.run(f"""(() => {{ const e = document.querySelector('[data-ivy-ref="{int(ref)}"]');
        if (!e) return null; return ({expression}); }})()""")
    if found is None:
        raise LookupError(f"Element {ref} isn't on the page any more. Call browser_read for fresh numbers.")
    return found


_LABEL = "(e.getAttribute('aria-label') || e.innerText || e.value || e.title || '').trim().slice(0, 80)"


def click_risk(args: dict) -> str | None:
    """Why this click needs Romeu's OK every time, or None. Unknown means ask."""
    with _lock:
        try:
            tab = _tab()
            label = _element(tab, args.get("ref", 0), _LABEL + " + ' ' + (e.form && e.form.action || e.href || '')")
        except Exception:
            return None     # the click itself will fail and report why
    m = _RISKY_CLICK.search(label)
    return f'clicking "{label[:60].strip()}" could {m.group(1).lower()} something for real' if m else None


@tool(group="browser", timeout=45, untrusted=True, url="Full http(s) address")
def browser_open(url: str) -> str:
    """Open a site in Ivy's own browser window, to read it or act on it. Use for logging-in sites,
    forms and multi-step web tasks. Then call browser_read."""
    if not re.match(r"https?://", url):
        if "://" in url or url.lower().startswith(("file:", "javascript:", "data:")):
            return "NOT DONE: only http and https addresses can be opened."
        url = "https://" + url
    with _lock:
        tab = _tab()
        tab.send("Page.navigate", url=url)
        _settle(tab)
        return _where(tab)


@tool(group="browser", timeout=30, untrusted=True)
def browser_read() -> str:
    """See the page in Ivy's browser: its text and a numbered list of every button, link and field."""
    with _lock:
        tab = _tab()
        page = json.loads(tab.run(_SNAPSHOT))
    items = "\n".join(page["items"]) or "(nothing clickable)"
    return f"{page['title']} — {page['url']}\n\nControls:\n{items}\n\nPage text:\n{page['text']}"


@tool(group="browser", confirm="action", timeout=30, ref="The element's number from browser_read")
def browser_click(ref: int) -> str:
    """Click a button or link in Ivy's browser, by its number."""
    with _lock:
        tab = _tab()
        before = tab.run("location.href")
        spot = _element(tab, ref, "(e.scrollIntoView({block: 'center'}), (r => ({x: r.x + r.width / 2, "
                                  "y: r.y + r.height / 2}))(e.getBoundingClientRect()))")
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            tab.send("Input.dispatchMouseEvent", type=kind, x=spot["x"], y=spot["y"], button="left", clickCount=1)
        _settle(tab, before)
        tab = _tab()                # the click may have opened a new tab
        moved = tab.run("location.href") != before
        return _where(tab) if moved else "Clicked; the address didn't change. Use browser_read to see the result."


@tool(group="browser", confirm="action", timeout=30, ref="The field's number from browser_read",
      submit="True = press Enter afterwards")
def browser_type(ref: int, text: str, submit: bool = False) -> str:
    """Type into a field in Ivy's browser, replacing what's there. Never used for passwords or card
    numbers: those are refused, and Romeu types them."""
    with _lock:
        tab = _tab()
        about = _element(tab, ref, "[e.type, e.name, e.id, e.autocomplete, e.getAttribute('aria-label'), "
                                   "e.placeholder].join(' ')")
        if _SENSITIVE_FIELD.search(about):
            return ("NOT DONE: this looks like a password or payment field. Ivy never types those. "
                    "Ask Romeu to fill it in directly in the browser window.")
        _element(tab, ref, "(e.focus(), e.select && e.select(), true)")
        tab.send("Input.insertText", text=text)
        typed = _element(tab, ref, "e.value !== undefined ? e.value : e.innerText")
        if text not in str(typed):
            return f"Typed, but the field shows {str(typed)[:80]!r} instead. Check with browser_read."
        if submit:
            before = tab.run("location.href")
            for kind in ("rawKeyDown", "char", "keyUp"):
                tab.send("Input.dispatchKeyEvent", type=kind, key="Enter", code="Enter",
                         windowsVirtualKeyCode=13, text="\r" if kind == "char" else "")
            _settle(tab, before)
            return f"Typed and pressed Enter. {_where(_tab())}"
        return "Typed; the field now holds that text."


@tool(group="browser", confirm="action", timeout=20, ref="The dropdown's number", option="The option's visible text")
def browser_select(ref: int, option: str) -> str:
    """Choose an option in a dropdown in Ivy's browser."""
    want = json.dumps(option.lower())
    with _lock:
        tab = _tab()
        picked = _element(tab, ref, f"""(() => {{ const o = [...e.options].find(o => o.text.trim().toLowerCase().includes({want}));
            if (!o) return ''; e.value = o.value; e.dispatchEvent(new Event('input', {{bubbles: true}}));
            e.dispatchEvent(new Event('change', {{bubbles: true}})); return o.text.trim(); }})()""")
    return f"Selected {picked}." if picked else f"NOT DONE: no option matches '{option}'."


def close():
    """Quit Ivy's browser. Closing its tabs would leave the process running."""
    global _session
    with _lock:
        try:
            Session(_api("/version")["webSocketDebuggerUrl"]).ws.send(
                json.dumps({"id": 1, "method": "Browser.close"}))
        except (OSError, ValueError, KeyError):
            pass
        _session = None
