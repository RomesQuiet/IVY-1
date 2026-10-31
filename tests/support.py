# tests/support.py
"""
Safety net and fake model for Ivy's tests.

install() runs before anything imports Ivy. It makes it impossible for a test to touch
the real Mac: no opening apps, no AppleScript, no volume changes, no network (offline),
and memory and skills live in a temp folder. Anything blocked raises Blocked.
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
LIVE = bool(os.environ.get("IVY_TEST_LIVE"))
_ALLOWED_COMMANDS = {"osacompile", "pgrep", "pmset"}      # compile only / read only
blocked: list = []
_real_popen = subprocess.Popen


class Blocked(RuntimeError):
    pass


class _GuardedPopen(_real_popen):
    def __init__(self, args, *a, **kw):
        name = Path(args[0] if isinstance(args, (list, tuple)) else str(args).split()[0]).name
        script = Path(str(args[1])).name if isinstance(args, (list, tuple)) and len(args) > 1 else ""
        headless = isinstance(args, (list, tuple)) and "--headless=new" in args
        if name not in _ALLOWED_COMMANDS and not headless and not (name.startswith("python") and script.startswith("fake_")):
            blocked.append(args)
            raise Blocked(f"test tried to run: {args}")
        super().__init__(args, *a, **kw)


def install():
    sys.path.insert(0, str(ROOT))
    if not LIVE:
        os.environ["GROQ_API_KEY"] = "test-key-not-real"
        for key in ("CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "SPOTIFY_CLIENT_ID"):
            os.environ[key] = ""
    temp = Path(tempfile.mkdtemp(prefix="ivy-tests-"))
    os.environ["IVY_SKILLS_DIR"] = str(temp / "skills")
    subprocess.Popen = _GuardedPopen

    from ivy import config, memory, senses
    config.MUTED = True
    config.ROOT = temp                      # caches and settings land here too
    config.REPORTS_DIR = temp / "reports"
    config.WORKSPACE = temp / "projects"
    config.BROWSER_PROFILE = temp / "browser"
    config.BROWSER_HEADLESS = True          # never a window, never the real profile
    config.BROWSER_PORT = 9347
    memory._store = memory.MemoryStore(temp / "memory.json")

    def no_volume(_level):
        raise Blocked("test tried to change the volume")
    senses.set_volume = no_volume
    senses.get_volume = lambda: (30, False)

    if not LIVE:
        import requests

        def no_network(*_a, **_k):
            raise Blocked("test tried to use the network")
        requests.sessions.Session.send = no_network

    from ivy import tools, tools_mac, tools_media       # registers every tool

    def no_browser(url, browser=""):
        raise Blocked(f"test tried to open {url}")
    tools.open_in_browser = tools_media.open_in_browser = no_browser

    def no_hands():
        raise Blocked("test tried to use the keyboard or mouse")
    tools_mac._gui = no_hands
    return temp


# =====================================================================
# Scripted fake model
# =====================================================================

def chunk(content=None, calls=None, reasoning=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=calls, reasoning=reasoning)
    return SimpleNamespace(usage=usage, choices=[SimpleNamespace(delta=delta)])


def say(text: str) -> list:
    return [chunk(content=text)]


def call(tool_name: str, /, reasoning: str | None = None, **args) -> list:
    """A step where the model asks for one tool."""
    fn = SimpleNamespace(name=tool_name, arguments=json.dumps(args))
    return [chunk(reasoning=reasoning, calls=[SimpleNamespace(index=0, id=f"id-{tool_name}", function=fn)])]


def calls(*pairs) -> list:
    """A step where the model asks for several tools at once: calls(('tool', {...}), ...)."""
    out = []
    for i, (name, args) in enumerate(pairs):
        fn = SimpleNamespace(name=name, arguments=json.dumps(args))
        out.append(chunk(calls=[SimpleNamespace(index=i, id=f"id{i}", function=fn)]))
    return out


def api_error(status: int, message: str):
    """The exception the OpenAI client raises for an HTTP error."""
    import openai
    response = SimpleNamespace(status_code=status, headers={}, request=None)
    kind = openai.RateLimitError if status == 429 else openai.APIStatusError
    return kind(message, response=response, body=None)


def stream_error(message: str):
    """Errors inside a stream arrive as a plain APIError."""
    import openai
    return openai.APIError(message, request=None, body=None)


class _Stream:
    def __init__(self, items):
        self.items, self.closed = items, False

    def __iter__(self):
        for item in self.items:
            if isinstance(item, Exception):
                raise item
            yield item

    def close(self):
        self.closed = True


class FakeModel:
    """Plays back `steps`: each is a list of chunks (a chunk list may contain an exception,
    raised mid-stream) or an exception raised when the request is opened."""

    def __init__(self, steps, remaining=7000):
        self.steps, self.requests, self.remaining = list(steps), [], remaining
        self.lessons = []          # replies for learning's one-off (non-streaming) call
        self.chat = SimpleNamespace(completions=SimpleNamespace(
            create=self._plain, with_raw_response=SimpleNamespace(create=self._create)))

    def _plain(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        message = SimpleNamespace(content=self.lessons.pop(0) if self.lessons else "{}")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def _create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        if not self.steps:
            raise AssertionError("the agent asked the model more times than the test scripted")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        headers = {"x-ratelimit-remaining-tokens": str(self.remaining), "x-ratelimit-reset-tokens": "1s"}
        return SimpleNamespace(headers=headers, parse=lambda: _Stream(step))


def fake_pool(fast=None, main=None, backup=None, tpm=8000):
    """Install a pool of fake models and return it."""
    from ivy import router
    pool = router.ModelPool.__new__(router.ModelPool)
    pool.endpoints = []
    for tier, model in (("fast", fast), ("main", main), ("backup", backup)):
        if model is not None:
            pool.endpoints.append(router.Endpoint("groq", f"fake/{tier}", tier, model, tpm, {}))
    router._pool = pool
    return pool
