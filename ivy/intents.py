# ivy/intents.py
"""
Recognises everyday commands, so the router can give them the shortest route.

Ivy still thinks and decides, but with very little to read, so these cost a fraction of a
normal request. If every model is out of quota, the command runs directly. Anything
ambiguous, compound ("… and then …") or conversational returns None.
"""

import datetime
import re

from . import learning
from .tools import resolve_app

_FILLER = re.compile(
    r"^(?:(?:hey|ok|okay|hi)\s+)?(?:ivy[,!.]?\s+)?(?:(?:can|could|would|will) you\s+)?(?:please\s+)?",
    re.I,
)
_SITES = {
    "youtube": "youtube.com", "gmail": "mail.google.com", "google": "google.com",
    "github": "github.com", "netflix": "netflix.com", "twitter": "x.com", "x": "x.com",
    "reddit": "reddit.com", "chatgpt": "chatgpt.com", "claude": "claude.ai",
    "google maps": "maps.google.com", "maps": "maps.google.com", "whatsapp web": "web.whatsapp.com",
    "linkedin": "linkedin.com", "instagram": "instagram.com", "facebook": "facebook.com",
    "amazon": "amazon.com", "wikipedia": "wikipedia.org", "twitch": "twitch.tv",
    "google drive": "drive.google.com", "drive": "drive.google.com", "google docs": "docs.google.com",
}
_UNITS = {"s": 1 / 60, "sec": 1 / 60, "second": 1 / 60, "m": 1, "min": 1, "minute": 1, "h": 60, "hr": 60, "hour": 60}
_BACKGROUND = re.compile(
    r"in the background[,:]?\s+(.+)|(.+?)[,]?\s+in the background[.!]*"
    r"|(?:start|run)\s+a\s+(?:background\s+)?task\s*(?:to|:)\s*(.+)", re.I | re.S)
_TASK_STATUS = {"list tasks", "show tasks", "what are you working on", "how's the task going",
                "how is the task going", "any tasks running", "what tasks are running"}
_TASK_CANCEL = {"cancel the task", "stop the task", "cancel the background task",
                "stop the background task", "cancel task"}
_MUSIC_WORDS = re.compile(r"\b(songs?|music|album|playlist|track|radio|by|some)\b")


def _clean(text: str) -> str:
    t = text.strip().lower()
    t = re.sub(r"[.!?]+$", "", t).strip()
    t = _FILLER.sub("", t)
    return re.sub(r"\s+please$", "", t).strip()


def _strip_video(q: str) -> str:
    q = re.sub(r"^(?:a|an|some|the|me)\s+", "", q)
    return re.sub(r"\s+videos?$", "", q).strip()


_ABOUT_ME = re.compile(
    r"\b(?:your|groq'?s?|tokens?)\b.*\busage\b|\busage\b.*\b(?:groq|models?)\b|\btokens? (?:left|remaining)\b"
    r"|\b(?:which|what) models?\b.*\b(?:you|using|running)\b|\bcan (?:i|we|you) (?:switch|change)\b.*\bmodels?\b"
    r"|\bare you on\b.*\bmodels?\b")
_NOT_ME = re.compile(r"\b(cpu|disk|memory|ram|battery|data|phone|water|gas)\b")
_SWITCH = re.compile(r"\b(?:switch|change|use|go|move|put|set|try)\b.*\b(?:(lightest|lighter|smallest|fastest|light)"
                     r"|(heaviest|heavier|biggest|strongest|heavy)|(automatic|auto|the router))"
                     r"(?: one| models?| mode| version)?(?: (?:please|now|instead|for now|from now on|again))*$")
_LIGHT_ALONE = re.compile(r"\b(?:light|heavy)(?: mode)?(?: please| now)*$")


def about_ivy(text: str) -> str | None:
    """Questions about her own models → 'status', 'fast', 'main' or 'auto'. She can't know
    these from training, so they never reach the model."""
    t = _clean(text).replace("\u2019", "'")
    m = _SWITCH.search(t)
    if m and (not _LIGHT_ALONE.search(t)):
        return "fast" if m.group(1) else "main" if m.group(2) else "auto"
    return "status" if len(t) <= 120 and _ABOUT_ME.search(t) and not _NOT_ME.search(t) else None


def match(text: str) -> tuple[str, dict] | tuple[None, str] | None:
    """→ (tool_name, args) to run, (None, reply) for a direct answer, or None."""
    # --- background tasks (before the length and comma limits: goals can be long) ---
    raw = _FILLER.sub("", text.strip()).strip()
    m = _BACKGROUND.fullmatch(raw)
    if m:
        goal = (m.group(1) or m.group(2) or m.group(3)).strip(" ,:.")
        if len(goal.split()) >= 2:
            return "start_task", {"goal": goal}

    t = _clean(text)
    if t in _TASK_STATUS:
        return "list_tasks", {}
    if t in _TASK_CANCEL:
        return "cancel_task", {}
    if not t or len(t) > 120 or re.search(r"\b(and then|then|and also|after that)\b|,", t):
        return None

    # --- time / date ---
    if re.fullmatch(r"what(?:'s| is) the time(?: now)?|what time is it(?: now)?|time", t):
        return None, datetime.datetime.now().strftime("It's %H:%M.")
    if re.fullmatch(r"what(?:'s| is) (?:the )?date(?: today)?|what day is (?:it|today)|what(?:'s| is) today", t):
        return None, datetime.datetime.now().strftime("It's %A, %d %B %Y.")

    # --- YouTube ---
    m = (re.fullmatch(r"(?:play|watch|open|put on|show me|find|search)\s+(.+?)\s+(?:on|in|from)\s+youtube", t)
         or re.fullmatch(r"youtube\s+(.+)", t))
    if m and _strip_video(m.group(1)):
        return "play_youtube", {"query": _strip_video(m.group(1))}

    # --- music ---
    m = re.fullmatch(r"(?:play|put on|listen to)\s+(.+?)\s+(?:on|in|from|using)\s+(spotify|apple music|music)", t)
    if m:
        return "play_music", {"query": m.group(1), "app": "Music" if "music" in m.group(2) and
                              m.group(2) != "spotify" else "Spotify"}
    m = re.fullmatch(r"(?:play|put on|listen to)\s+(.+)", t)
    if m and _MUSIC_WORDS.search(m.group(1)) and not re.search(r"\b(game|chess|video|movie|film)\b", m.group(1)):
        q = re.sub(r"^(?:some|me some|me)\s+", "", m.group(1)).strip()
        return "play_music", {"query": q, "app": "auto"}

    # --- playback ---
    if re.fullmatch(r"(?:pause|stop)(?: the)?(?: music| song| track| playback| spotify)?|pause", t) and t != "stop":
        return "media_control", {"action": "pause"}
    if re.fullmatch(r"(?:resume|unpause|continue)(?: the)?(?: music| song| playback)?|play(?: the)? music", t):
        return "media_control", {"action": "play"}
    if re.fullmatch(r"(?:next|skip)(?: song| track| this(?: song)?)?|skip it|play next", t):
        return "media_control", {"action": "next"}
    if re.fullmatch(r"(?:previous|last|go back)(?: song| track)|back a song", t):
        return "media_control", {"action": "previous"}
    if re.fullmatch(r"what(?:'s| is) (?:this song|playing)|what song is this|who sings this", t):
        return "media_control", {"action": "now_playing"}

    # --- volume ---
    m = re.fullmatch(r"(?:set |turn )?(?:the )?volume (?:to |at )?(\d{1,3})(?: ?%| percent)?", t)
    if m:
        return "set_volume", {"level": int(m.group(1))}
    if re.fullmatch(r"(?:turn (?:it|the volume|the sound) up|volume up|louder|raise the volume)(?: a bit)?", t):
        return "set_volume", {"change": 15}
    if re.fullmatch(r"(?:turn (?:it|the volume|the sound) down|volume down|quieter|lower the volume)(?: a bit)?", t):
        return "set_volume", {"change": -15}
    if t in ("mute", "mute the sound", "mute volume"):
        return "set_volume", {"level": 0}
    if t in ("unmute", "unmute the sound"):
        return "set_volume", {"level": 40}

    # --- timers ---
    m = re.fullmatch(r"(?:set |start )?(?:a |an )?timer (?:for )?(\d+(?:\.\d+)?)\s*"
                     r"(s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)(?: (?:for|called|named|to) (.+))?", t)
    if not m:
        m = re.fullmatch(r"(?:set |start )?(?:a |an )?(\d+(?:\.\d+)?)[- ]"
                         r"(s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?) timer(?: (?:for|called|named|to) (.+))?", t)
    if m:
        unit = re.sub(r"s$", "", m.group(2)) if m.group(2) not in ("s",) else "s"
        minutes = float(m.group(1)) * _UNITS.get(unit, 1)
        return "set_timer", {"minutes": round(minutes, 3), "label": (m.group(3) or "Timer").strip().capitalize()}

    # --- weather ---
    m = re.fullmatch(r"(?:what(?:'s| is) |how(?:'s| is) )?(?:the )?weather(?: like)?(?: (?:in|for|at) (.+?))?"
                     r"(?: (?:today|now|right now))?", t)
    if m:
        return "get_weather", {"location": (m.group(1) or "").strip().title()}

    # --- open an app or site ---
    m = re.fullmatch(r"(?:open|launch|start|bring up|go to)\s+(?:up\s+)?(?:the\s+)?(.+?)(?:\s+app)?", t)
    if m:
        target = m.group(1).strip()
        if target in _SITES:
            return "open_url", {"url": _SITES[target]}
        if re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", target):
            return "open_url", {"url": target}
        if resolve_app(target):
            return "open_app", {"name": target}

    # --- quit an app ---
    m = re.fullmatch(r"(?:quit|close)\s+(?:the\s+)?(.+?)(?:\s+app)?", t)
    if m and resolve_app(m.group(1)):
        return "quit_app", {"name": m.group(1)}

    # --- open a search page ---
    m = re.fullmatch(r"(?:google|search google for)\s+(.+)", t)
    if m:
        return "browse", {"query": m.group(1), "site": "google"}
    m = re.fullmatch(r"search (youtube|amazon|github|reddit|wikipedia|maps) for (.+)", t)
    if m:
        return "browse", {"query": m.group(2), "site": m.group(1)}
    m = re.fullmatch(r"(?:directions|navigate|take me) to (.+)", t)
    if m:
        return "browse", {"query": f"directions to {m.group(1)}", "site": "maps"}

    return None


FAILED = re.compile(r"^(?:\w+ failed:|unknown tool|missing required|no \w+ (?:results|matches)|not done|"
                    r"could(?:n't| not)|can't|.*\bnot found\b)", re.I)


def hint(text: str) -> tuple[str, dict] | tuple[None, str] | None:
    """What this probably is → (tool_name, args), (None, known answer) or None. Ivy still decides."""
    asked = about_ivy(text)
    if asked == "status":
        return "my_status", {}
    if asked:
        return "switch_model", {"choice": {"fast": "lightest", "main": "heaviest", "auto": "automatic"}[asked]}
    found = match(text)
    if found and found[0] and learning.applies(found[0], text):
        return None     # something Romeu taught her may change this: she needs the full picture
    return found
