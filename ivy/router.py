# ivy/router.py
"""Task classification + a budget-aware pool of LLM endpoints."""

import os
import re
import time
from dataclasses import dataclass

from . import config

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


def parse_duration(text: str) -> float:
    """'2m52.8s' / '922ms' / '50.4s' / '1h2m' → seconds."""
    if not text:
        return 0.0
    m = re.search(r"([\d.]+)ms\b", text)
    if m and not re.search(r"\d[hms](?!s)", text.replace(m.group(0), "")):
        return float(m.group(1)) / 1000
    total = 0.0
    for value, unit in re.findall(r"([\d.]+)\s*([hms])(?!s)", text):
        total += float(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def parse_retry_seconds(error_message: str) -> float:
    m = re.search(r"try again in ([\dhms.]+)", error_message)
    return parse_duration(m.group(1)) + 1 if m else 30.0


# =====================================================================
# Model pool — every (provider, model) with its own live token budget
# =====================================================================

@dataclass
class Endpoint:
    provider: str
    model: str
    tier: str                 # "fast" | "main" | "backup"
    client: object
    tpm: int                  # tokens/minute cap; also the max size of ONE request
    extra: dict               # provider-specific request params
    remaining: int | None = None
    seen_at: float = 0.0      # when `remaining` was true (in the future right after a 429)
    benched_until: float = 0.0
    bench_reason: str = ""

    @property
    def name(self) -> str:
        short = self.model.split("/")[-1]
        return short if self.provider == "groq" else f"{self.provider}:{short}"

    def budget(self, now: float) -> int:
        """Tokens available now. Groq refills continuously (8000/min ≈ 133 per second),
        so the allowance grows from the last reading instead of resetting all at once."""
        if self.remaining is None:
            return self.tpm
        return min(self.tpm, int(self.remaining + (now - self.seen_at) * self.tpm / 60))

    def wait_for(self, need: int, now: float) -> float:
        """Seconds until `need` tokens have refilled."""
        return max(0.0, (need - self.budget(now)) * 60 / self.tpm)


TIER_ORDER = {
    "simple":  ("fast", "main", "backup"),
    "medium":  ("main", "fast", "backup"),
    "complex": ("main", "fast", "backup"),
}


class ModelPool:
    def __init__(self):
        from openai import OpenAI
        self.endpoints: list[Endpoint] = []
        for p in config.PROVIDERS:
            key = os.getenv(p["key_env"])
            if not key:
                continue
            client = OpenAI(base_url=p["base_url"], api_key=key, timeout=60, max_retries=0)
            for tier, model in p["models"].items():
                if model:
                    self.endpoints.append(Endpoint(p["name"], model, tier, client,
                                                   p.get("tpm", 8000), p.get("extra", {})))
        if not self.endpoints:
            raise RuntimeError("No LLM provider configured — add GROQ_API_KEY to .env")

    # ---- choosing ----

    def chain(self, difficulty: str, force: str | None = None) -> list[Endpoint]:
        order = TIER_ORDER.get(difficulty, TIER_ORDER["medium"])
        eps = sorted(self.endpoints, key=lambda e: order.index(e.tier) if e.tier in order else 9)
        if force:
            eps = sorted(eps, key=lambda e: e.model != force)
        return eps

    def pick(self, difficulty: str, need: int, force: str | None = None,
             skip: set = frozenset()) -> tuple[Endpoint | None, float, str]:
        """Best endpoint for a request of `need` tokens → (endpoint, wait_seconds, why).
        wait > 0 means every usable endpoint is out of budget until then."""
        now = time.time()
        usable = [e for e in self.chain(difficulty, force)
                  if e.benched_until <= now and e.tpm >= need and id(e) not in skip]
        if not usable:
            too_big = all(e.tpm < need for e in self.endpoints)
            return None, 0.0, "too_big" if too_big else "benched"
        for e in usable:
            if e.budget(now) >= need:
                return e, 0.0, ""
        soonest = min(usable, key=lambda e: e.wait_for(need, now))
        return soonest, soonest.wait_for(need, now), "budget"

    # ---- feedback from the API ----

    def update_from_headers(self, e: Endpoint, headers):
        try:
            e.remaining, e.seen_at = int(headers.get("x-ratelimit-remaining-tokens")), time.time()
        except (TypeError, ValueError):
            pass

    def rate_limited(self, e: Endpoint, message: str):
        wait = parse_retry_seconds(message)
        # Groq says this request fits in `wait` seconds, so that's when the budget equals it.
        requested = re.search(r"Requested (\d+)", message)
        e.remaining = int(requested.group(1)) if requested else e.tpm // 2
        e.seen_at = time.time() + wait
        if wait > 120:   # daily quota, not the per-minute one
            self.bench(e, wait, "daily limit reached")
        else:
            print(f"  ⏳ {e.name} rate-limited for {wait:.0f}s")

    def bench(self, e: Endpoint, seconds: float, reason: str):
        e.benched_until, e.bench_reason = time.time() + seconds, reason
        print(f"  💀 {e.name} {reason} — skipping it for ~{max(1, int(seconds // 60))} min")

    def available(self) -> int:
        now = time.time()
        return sum(1 for e in self.endpoints if e.benched_until <= now)

    def status(self) -> str:
        now = time.time()
        parts = []
        for e in self.endpoints:
            if e.benched_until > now:
                parts.append(f"{e.name}: {e.bench_reason} (back {time.strftime('%H:%M', time.localtime(e.benched_until))})")
            else:
                parts.append(f"{e.name}: {max(0, e.budget(now))}/{e.tpm} tokens available")
        return "; ".join(parts)


_pool = None


def get_pool() -> ModelPool:
    global _pool
    if _pool is None:
        _pool = ModelPool()
    return _pool
