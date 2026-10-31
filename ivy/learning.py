# ivy/learning.py
"""
Learning from corrections: "no, I meant Safari", "from now on use metric".

A cheap filter spots messages that might teach something, so normal requests cost
nothing. Those get one small model call that decides whether it's a lasting lesson or
a one-off, and lessons are saved as memories tagged "learned".

The model sees only Romeu's own words and the names of the tools used, never web pages
or screen text, so outside content can't plant a permanent memory.
"""

import json
import re

from .memory import get_store
from .router import get_pool

TAG = "learned"
MAX_LESSON = 160
_NEED = 900            # tokens to budget for the call, reasoning included

_SIGNAL = re.compile(
    r"^\s*(no\b|nope\b|not (that|this|what)\b|wrong\b|that'?s (wrong|not)\b|i (said|meant|mean)\b|"
    r"i didn'?t (mean|say|ask)\b|actually\b|it'?s not\b)"
    r"|\b(from now on|next time|in (the )?future|going forward|by default|every time|whenever i)\b"
    r"|\b(always|never|please don'?t|don'?t ever|stop (using|doing|opening|calling|saying))\b"
    r"|\b(i prefer|i'?d rather|i like my|i hate when|call me|instead of|i meant)\b", re.I)
_NOT_A_LESSON = re.compile(
    r"^\s*(no (thanks|thank you|problem|worries|need|way)\b|never mind|nevermind|not (now|yet|really)\b|"
    r"actually,? (what|who|when|where|why|how|can|could|is|are|do|does)\b)", re.I)

_PROMPT = """Romeu just said something to their AI assistant. Decide if it teaches a lasting fact
for FUTURE requests. Default to null: most corrections only fix the current request.

Save a lesson only for:
- a stated habit or rule: "from now on use metric" → "Romeu wants metric units."
- how Romeu wants to be addressed: "call me Romy" → "Romeu wants to be called Romy."
- what a general word means to Romeu: request "open the browser", now "no, I meant Safari"
  → "When Romeu says the browser, they mean Safari."

Never save (lesson is null):
- a change to this one task: "no, make it 7 minutes", "no, the second video", "use Porto instead"
- a complaint or retry: "that's wrong, try again", "no, that's not it"
- anything specific to one number, video, file or result

Previous request: {previous}
What the assistant did: {did}
Romeu now says: {said}

Notes it already has:
{notes}

Reply with JSON only: {{"lesson": "<one short third-person fact starting with Romeu or When Romeu, or null>",
"replaces": "<id of a note this contradicts, or null>"}}"""

# choices a lesson can change; pausing or a timer doesn't depend on preferences
_CHOICE_TOOLS = {"open_app", "open_url", "browse", "play_music", "play_youtube"}
_IGNORED = {"romeu", "prefers", "prefer", "when", "says", "said", "mean", "means", "they", "them", "their",
            "uses", "wants", "want", "always", "never", "that", "this", "with", "instead", "should",
            "open", "play", "from", "over", "than", "likes", "like"}


def worth_checking(text: str) -> bool:
    return bool(_SIGNAL.search(text or "")) and not _NOT_A_LESSON.search(text or "")


def lessons() -> list[dict]:
    return [m for m in get_store().all() if TAG in m.get("tags", [])]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 4 and w not in _IGNORED}


def applies(tool: str, text: str) -> bool:
    """Should this instant command go to the agent, because a lesson may change the choice?"""
    if tool not in _CHOICE_TOOLS:
        return False
    wanted = _words(text)
    return any(wanted & _words(m["text"]) for m in lessons())


def _ask(prompt: str) -> str:
    endpoint, wait, _ = get_pool().pick("simple", _NEED)
    if endpoint is None or wait > 0:
        return ""        # never make Romeu wait for this; the next correction gets another go
    r = endpoint.client.chat.completions.create(
        model=endpoint.model, max_tokens=500, temperature=0,
        messages=[{"role": "user", "content": prompt}])
    return r.choices[0].message.content or ""


def study(said: str, previous: str = "", did: str = "") -> dict | None:
    """→ the saved memory, or None. Failures are silent: this must never break a request."""
    if not worth_checking(said):
        return None
    known = lessons()
    notes = "\n".join(f"[{m['id']}] {m['text']}" for m in known) or "(none)"
    try:
        raw = _ask(_PROMPT.format(previous=previous[:300] or "(none)", did=did[:200] or "(nothing)",
                                  said=said[:300], notes=notes))
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except Exception:
        return None
    lesson = data.get("lesson")
    if not isinstance(lesson, str) or not lesson.strip() or lesson.strip().lower() == "null":
        return None
    lesson = " ".join(lesson.split())[:MAX_LESSON]
    store = get_store()
    old = data.get("replaces")
    if old in {m["id"] for m in known}:
        store.forget(old)
    entry = store.add(lesson, [TAG])
    return None if "error" in entry else entry
