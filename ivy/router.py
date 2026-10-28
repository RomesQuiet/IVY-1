# ivy/router.py
"""Task classification + model routing with rate-limit awareness."""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import config

STATE_PATH = Path(__file__).resolve().parent.parent / "state.json"

def _chain(*models):
    return list(dict.fromkeys(models))   # dedupe, keep order


DIFFICULTY_CHAINS = {
    "simple":  _chain(config.MODEL_FAST,  config.MODEL_MAIN,  config.MODEL_HEAVY),
    "medium":  _chain(config.MODEL_MAIN,  config.MODEL_HEAVY, config.MODEL_FAST),
    "complex": _chain(config.MODEL_HEAVY, config.MODEL_MAIN,  config.MODEL_FAST),
}
ALL_MODELS = _chain(config.MODEL_FAST, config.MODEL_MAIN, config.MODEL_HEAVY)


# Weighted signals: positive pushes toward complex, negative toward simple.
_SIGNALS = [
    # --- strong complex signals ---
    (r"\bwhy (does|do|is|are|would|should|did|has|have)\b", 4),
    (r"\bexplain (how|why|in detail|the reasoning)\b", 4),
    (r"\bstep[- ]by[- ]step\b", 4),
    (r"\bwalk me through\b", 4),
    (r"\b(design|architect) (a|an|the)\b", 3),
    (r"\bdebug\b", 3),
    (r"\brefactor\b", 3),
    (r"\bwrite (a|an|the)? ?(function|class|script|program|code|algorithm|essay|poem|story)\b", 4),
    (r"\b(compare|contrast) \w+ (and|to|with|vs)\b", 3),
    (r"\b(pros and cons|tradeoffs|advantages and disadvantages)\b", 3),
    (r"\b(analy|analys|critique|evaluate)\w+\b", 3),
    (r"\b(in detail|thoroughly|comprehensive(ly)?|deep dive)\b", 3),
    (r"\bhow (do|does|would|can) .{1,40} work\b", 3),
    (r"\bwhat (are|were) the (implications|consequences|repercussions)\b", 3),
    (r"\b(reason|reasoning) (about|through)\b", 3),
    (r"\bmulti[- ]?(step|stage|phase)\b", 3),
    (r"\bstrategy\b", 2),
    (r"\boptimize|optimise\b", 2),
    (r"\bphilosoph(y|ical)\b", 2),
    (r"\bimplications?\b", 2),

    # --- strong simple signals ---
    (r"^\s*(hi|hey|hello|yo|sup|howdy)\b", -5),
    (r"^\s*(thanks|thank you|thx|ty|cheers)\b", -5),
    (r"^\s*good (morning|afternoon|evening|night)\b", -5),
    (r"\bwhat time is it\b", -5),
    (r"\b(what|what's|whats) (is )?(the )?(time|date|day)\b", -5),
    (r"\bwhat day is (it|today)\b", -5),
    (r"\bweather\b", -4),
    (r"^\s*(open|close|launch|quit|kill|start|stop|pause|play)\b", -4),
    (r"^\s*(remember|forget|recall)\b", -4),
    (r"^\s*(list|show) (my |the )?(files|memories|tools|apps)\b", -4),
    (r"^\s*calc\b", -4),
    (r"^\s*screenshot\b", -4),
    (r"^\s*install\b", -3),
    (r"^\s*uninstall\b", -3),
    (r"\bvolume (up|down)\b", -3),
    (r"\b(skip|next|previous) (track|song)\b", -3),
    (r"^\s*(shut|sleep|standby)\b", -4),
]


def _score(text: str) -> int:
    t = (text or "").lower().strip()
    if not t:
        return -5

    s = 0
    for pat, w in _SIGNALS:
        if re.search(pat, t):
            s += w

    words = t.split()

    if len(words) > 45:
        s += 5
    elif len(words) > 30:
        s += 3
    elif len(words) > 18:
        s += 1
    elif len(words) <= 4:
        s -= 3

    if t.count("?") >= 2:
        s += 2

    if "```" in t or "def " in t or "class " in t:
        s += 3

    return s


def classify(text: str) -> str:
    s = _score(text)
    if s >= 3:
        return "complex"
    if s <= -3:
        return "simple"
    return "medium"


def parse_retry_seconds(error_message: str) -> int:
    m = re.search(r"try again in (\d+)m([\d.]+)s", error_message)
    if m:
        return int(m.group(1)) * 60 + int(float(m.group(2))) + 5
    m = re.search(r"try again in ([\d.]+)s", error_message)
    if m:
        return int(float(m.group(1))) + 5
    return 60


class ModelRouter:
    def __init__(self):
        self.state = {"dead_until": {}}
        self._load()

    def _load(self):
        if STATE_PATH.exists():
            try:
                data = json.loads(STATE_PATH.read_text())
                self.state = data if isinstance(data, dict) else self.state
                self.state.setdefault("dead_until", {})
            except Exception:
                pass

    def _save(self):
        STATE_PATH.write_text(json.dumps(self.state, indent=2))

    def is_dead(self, model: str) -> bool:
        iso = self.state["dead_until"].get(model)
        if not iso:
            return False
        try:
            until = datetime.fromisoformat(iso)
        except Exception:
            return False
        if datetime.now() >= until:
            self.state["dead_until"].pop(model, None)
            self._save()
            return False
        return True

    def mark_dead(self, model: str, seconds: int, reason: str = "rate-limited"):
        until = (datetime.now() + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        self.state["dead_until"][model] = until
        self._save()
        mins = max(1, seconds // 60)
        print(f"  💀 {model.split('/')[-1]} {reason} — skipping it for ~{mins} min")

    def dead_summary(self) -> str:
        dead = {m: t for m, t in self.state["dead_until"].items() if self.is_dead(m)}
        if not dead:
            return ""
        parts = [f"{m.split('/')[-1]} until {t[11:16]}" for m, t in dead.items()]
        return "rate-limited: " + ", ".join(parts)

    def chain_for(self, difficulty: str):
        return list(DIFFICULTY_CHAINS.get(difficulty, DIFFICULTY_CHAINS["medium"]))

    def pick(self, difficulty: str) -> str:
        for m in self.chain_for(difficulty):
            if not self.is_dead(m):
                return m
        return self.chain_for(difficulty)[0]

    def alive_count(self) -> int:
        return sum(1 for m in ALL_MODELS if not self.is_dead(m))


_router = None


def get_router() -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router