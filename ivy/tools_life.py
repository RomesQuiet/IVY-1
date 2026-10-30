# ivy/tools_life.py
"""Everyday macOS apps: Messages, Contacts, Mail, Calendar, Reminders, Notes,
plus system switches. Anything that sends, creates or powers off asks first.
macOS asks once per app for automation permission the first time."""

import datetime
import re
import subprocess
from typing import Literal

from .toolkit import tool
from .tools import _as_str, _osa, _sh


def _as_date(var: str, when: datetime.datetime) -> str:
    """AppleScript that builds a date without depending on the system locale."""
    return (f"set {var} to current date\n"
            f"set day of {var} to 1\n"
            f"set year of {var} to {when.year}\n"
            f"set month of {var} to {when.month}\n"
            f"set day of {var} to {when.day}\n"
            f"set time of {var} to {when.hour * 3600 + when.minute * 60}\n")


def _parse_when(text: str) -> datetime.datetime:
    text = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ValueError(f"use 'YYYY-MM-DD HH:MM', got {text!r}")


# =====================================================================
# CONTACTS & MESSAGES
# =====================================================================

@tool(group="life", name="Full or partial name, e.g. 'Mom', 'Ana Silva'")
def find_contact(name: str) -> str:
    """Look up phone numbers and emails in Contacts."""
    script = (f'tell application "Contacts"\n'
              f'set out to ""\n'
              f'repeat with p in (every person whose name contains {_as_str(name)})\n'
              f'set out to out & name of p & ": "\n'
              f'repeat with ph in phones of p\nset out to out & (value of ph) & "  "\nend repeat\n'
              f'repeat with em in emails of p\nset out to out & (value of em) & "  "\nend repeat\n'
              f'set out to out & linefeed\nend repeat\nreturn out\nend tell')
    return _osa(script, timeout=30) or f"No contact matching '{name}'."


def _handle_for(to: str) -> str:
    if re.fullmatch(r"[+\d][\d\s()-]{5,}|[^@\s]+@[^@\s]+", to.strip()):
        return to.strip()
    found = find_contact(to)
    m = re.search(r":\s*(\S[^\n]*?)\s{2}", found)
    if not m:
        raise RuntimeError(f"no phone or email for '{to}' in Contacts")
    return m.group(1).strip()


@tool(group="life", confirm="action", to="Contact name, phone number or email", text="Message text")
def send_imessage(to: str, text: str) -> str:
    """Send an iMessage/SMS through the Messages app."""
    handle = _handle_for(to)
    _osa(f'tell application "Messages"\n'
         f'set svc to 1st account whose service type = iMessage\n'
         f'send {_as_str(text)} to participant {_as_str(handle)} of svc\nend tell')
    return f"Sent to {to} ({handle})."


# =====================================================================
# MAIL
# =====================================================================

@tool(group="life", confirm="action",
      send="False = open the draft for Romeu to review (safer); True = send immediately")
def send_email(to: str, subject: str, body: str, send: bool = False) -> str:
    """Write an email in Apple Mail."""
    address = _handle_for(to) if "@" not in to else to
    _osa(f'tell application "Mail"\n'
         f'set m to make new outgoing message with properties '
         f'{{subject:{_as_str(subject)}, content:{_as_str(body)}, visible:true}}\n'
         f'tell m to make new to recipient at end of to recipients with properties {{address:{_as_str(address)}}}\n'
         + ("send m\n" if send else "activate\n") + 'end tell', timeout=30)
    return f"Email to {address} {'sent' if send else 'drafted and open in Mail'}."


@tool(group="life", untrusted=True)
def unread_email(limit: int = 8) -> str:
    """Senders and subjects of the newest unread emails in Apple Mail."""
    script = (f'tell application "Mail"\nset out to ""\n'
              f'set msgs to (messages of inbox whose read status is false)\n'
              f'repeat with i from 1 to (count of msgs)\nif i > {limit} then exit repeat\n'
              f'set m to item i of msgs\n'
              f'set out to out & (sender of m) & " — " & (subject of m) & linefeed\n'
              f'end repeat\nreturn out\nend tell')
    return _osa(script, timeout=45) or "No unread email."


# =====================================================================
# CALENDAR & REMINDERS & NOTES
# =====================================================================

@tool(group="life", days="How many days ahead, starting today")
def calendar_events(days: int = 1) -> str:
    """Upcoming events from the Calendar app."""
    start = datetime.datetime.now().replace(hour=0, minute=0)
    end = start + datetime.timedelta(days=max(1, days))
    script = (_as_date("d0", start) + _as_date("d1", end) +
              'set out to ""\ntell application "Calendar"\n'
              'repeat with c in calendars\n'
              'repeat with e in (every event of c whose start date ≥ d0 and start date < d1)\n'
              'set out to out & (start date of e as string) & " — " & (summary of e) & linefeed\n'
              'end repeat\nend repeat\nend tell\nreturn out')
    return _osa(script, timeout=60) or "Nothing on the calendar."


@tool(group="life", confirm="action", start="'YYYY-MM-DD HH:MM' local time",
      calendar="Calendar name; empty = the first writable one")
def add_calendar_event(title: str, start: str, minutes: int = 60, calendar: str = "") -> str:
    """Create a Calendar event."""
    when = _parse_when(start)
    target = f"calendar {_as_str(calendar)}" if calendar else \
        "first calendar whose writable is true"
    _osa(_as_date("d0", when) +
         f"set d1 to d0 + ({minutes} * minutes)\n"
         f'tell application "Calendar"\ntell {target}\n'
         f"make new event with properties {{summary:{_as_str(title)}, start date:d0, end date:d1}}\n"
         f"end tell\nend tell", timeout=30)
    return f"Added '{title}' on {when:%a %d %b at %H:%M}."


@tool(group="life", confirm="action", due="Optional 'YYYY-MM-DD HH:MM'")
def add_reminder(text: str, due: str = "") -> str:
    """Add a reminder to the Reminders app (it notifies at the due time)."""
    if due:
        when = _parse_when(due)
        _osa(_as_date("d0", when) + 'tell application "Reminders" to make new reminder with '
             f"properties {{name:{_as_str(text)}, due date:d0, remind me date:d0}}", timeout=30)
        return f"Reminder set: '{text}' at {when:%a %d %b %H:%M}."
    _osa(f'tell application "Reminders" to make new reminder with properties {{name:{_as_str(text)}}}',
         timeout=30)
    return f"Added reminder: '{text}'."


@tool(group="life")
def list_reminders() -> str:
    """Open (not completed) reminders."""
    return _osa('tell application "Reminders"\nset out to ""\n'
                'repeat with r in (every reminder whose completed is false)\n'
                'set out to out & name of r & linefeed\nend repeat\nreturn out\nend tell',
                timeout=45) or "No open reminders."


@tool(group="life", confirm="action")
def create_note(title: str, body: str) -> str:
    """Create a note in the Notes app."""
    html = f"<h1>{title}</h1><p>{body}</p>".replace("\n", "<br>")
    _osa(f'tell application "Notes" to make new note with properties {{body:{_as_str(html)}}}', timeout=30)
    return f"Note '{title}' created."


# =====================================================================
# SYSTEM SWITCHES
# =====================================================================

def _wifi_device() -> str:
    out = _sh(["networksetup", "-listallhardwareports"]).stdout
    m = re.search(r"Hardware Port: Wi-Fi\nDevice: (\w+)", out)
    return m.group(1) if m else "en0"


@tool(group="system")
def system_toggle(action: Literal["dark_mode_on", "dark_mode_off", "dark_mode_toggle", "wifi_on",
                                  "wifi_off", "brightness_up", "brightness_down", "lock_screen",
                                  "sleep_display", "show_desktop"]) -> str:
    """Flip a system setting: dark mode, Wi-Fi, brightness, lock the screen, sleep the display."""
    dark = 'tell application "System Events" to tell appearance preferences to set dark mode to '
    if action.startswith("dark_mode"):
        _osa(dark + {"dark_mode_on": "true", "dark_mode_off": "false",
                     "dark_mode_toggle": "not dark mode"}[action])
    elif action.startswith("wifi"):
        _sh(["networksetup", "-setairportpower", _wifi_device(), action[5:]]).check_returncode()
    elif action.startswith("brightness"):
        code = 144 if action.endswith("up") else 145
        _osa(f'tell application "System Events"\nrepeat 2 times\nkey code {code}\nend repeat\nend tell')
    elif action == "lock_screen":
        _osa('tell application "System Events" to keystroke "q" using {control down, command down}')
    elif action == "sleep_display":
        _sh(["pmset", "displaysleepnow"])
    elif action == "show_desktop":
        _osa('tell application "System Events" to key code 103')   # F11
    return f"Done: {action.replace('_', ' ')}."


@tool(group="system", confirm="action")
def power(action: Literal["sleep", "restart", "shut_down", "log_out"]) -> str:
    """Put the Mac to sleep, restart, shut down or log out."""
    verb = {"sleep": "sleep", "restart": "restart", "shut_down": "shut down", "log_out": "log out"}[action]
    subprocess.Popen(["osascript", "-e", f'tell application "System Events" to {verb}'])
    return f"{verb.capitalize()} requested."
