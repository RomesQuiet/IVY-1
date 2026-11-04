#!/usr/bin/env python3
"""
Do the tests really catch bugs? Puts past bugs back, one at a time, into a throwaway
copy of the code and checks that the suite fails each time.

    python tests/mutation_check.py              all of them, about twenty minutes
    python tests/mutation_check.py passwords    only bugs whose description matches
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# (what the bug was, file, correct code, buggy code)
BUGS = [
    ("search loop isn't limited", "ivy/brain.py", '"web_search": 3,', '"web_search": 99,'),
    ("outside content can run the shell", "ivy/brain.py", "guarded = self._tainted and t.name not in _UI_TOOLS", "guarded = False"),
    ("context doesn't count as outside content", "ivy/brain.py", "        self._tainted = True\n        return context.render(found)", "        return context.render(found)"),
    ("reasoning isn't passed back", "ivy/brain.py", 'msg["reasoning"] = reasoning[-4000:]', "pass"),
    ("invented tools crash the request", "ivy/brain.py", "and nudges < 2:", "and nudges < 0:"),
    ("oversized requests bench the model", "ivy/brain.py", "if err.status_code == 413 or", "if False and"),
    ("exit words get filtered out", "ivy/voice.py", "if _normalize(text) in EXIT_EXACT:", "if False:"),
    ("silence wakes Ivy", "ivy/wake.py", "SILENCE_RMS = 2", "SILENCE_RMS = -1"),
    ("unknown readings count as changes", "ivy/senses.py", "return before.get(key) is not None and after.get(key) is not None and before[key] != after[key]", "return before.get(key) != after.get(key)"),
    ("MCP tools without 'required' crash", "ivy/toolkit.py", 'params.get("required", [])', 'params["required"]'),
    ("YouTube request isn't instant", "ivy/intents.py", '"play_youtube", {"query": _strip_video(m.group(1))}', '"web_search", {"query": m.group(1)}'),
    ("main-thread JS deadlocks the app", "ivy_gui.py", "if threading.current_thread() is threading.main_thread():", "if False:"),
    ("a bad colour stops the app opening", "ivy_gui.py", 'return colour if isinstance(colour, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", colour) else "#1c1c1e"', 'return colour or "#1c1c1e"'),
    ("text can't be selected", "ivy_gui.py", "text_select=True,", "text_select=False,"),
    ("instant commands ignore lessons", "ivy/intents.py", "if found and found[0] and learning.applies(found[0], text):", "if False:"),
    ("tasks can use the screen", "ivy/brain.py", 'self.denied = _NOT_IN_BACKGROUND if background else frozenset()', "self.denied = frozenset()"),
    ("a page can start a clean task", "ivy/brain.py", 'if t.confirm == "task" and not guarded:', 'if t.confirm == "task":'),
    ("passwords get typed", "ivy/browser.py", "if _SENSITIVE_FIELD.search(about):", "if False:"),
    ("purchases don't ask", "ivy/brain.py", "guarded = guarded or bool(risk)", "pass"),
    ("local files can be opened", "ivy/browser.py", 'if "://" in url or', 'if False and'),
    ("retry storm after a 429", "ivy/router.py", "e.blocked_until = e.seen_at = time.time() + wait", "e.seen_at = time.time() + wait"),
    ("replies don't reach the chat", "ivy/brain.py", "        if on_token:\n            on_token(reply)\n", ""),
    ("a build task needs no files", "ivy/tasks.py", "and not _made_files(task):\n            # it described", "and False:\n            # it described"),
    ("the shell runs anything", "ivy/brain.py", '"run_shell": safety.shell_risk,', '"run_shell": lambda a: None,'),
    ("/Applications can be deleted with OK", "ivy/tools.py", "refused = safety.forbidden(command)\n    if refused:\n        return refused\n    r = _sh", "r = _sh"),
    ("Ivy can uninstall herself", "ivy/tools.py", 'if name.lower().removesuffix(".app") in _NEVER_UNINSTALL:', "if False:"),
    ("scripts dodge the allowlist", "ivy/safety.py", 'return python_risk(text) or ("it would delete apps" if forbidden_python(text) else None)', "return None"),
    ("unnamed apps can be uninstalled", "ivy/brain.py", 'if c["name"] == "uninstall_package" and not self._named_by_romeu(str(args.get("name", ""))):', "if False:"),
    ("the shell can delete", "ivy/tools.py", "if safety.deletes(command):", "if False:"),
    ("restore overwrites", "ivy/tools.py", "if home.exists():", "if False:"),
    ("secrets reach the log", "ivy/actionlog.py", 'line = _redact(json.dumps(entry, ensure_ascii=False, default=str))', 'line = json.dumps(entry, ensure_ascii=False, default=str)'),
    ("old copies keep acting", "ivy/toolkit.py", 'if t.confirm in ("shell", "always") and out_of_date():', "if False:"),
    ("Ivy guesses about herself", "ivy/intents.py", 'if asked == "status":\n        return "my_status", {}', 'if False:\n        return "my_status", {}'),
    ("thinking doesn't scale", "ivy/brain.py", '**({"reasoning_effort": self._effort(e)} if e.thinks else {}),', ""),
    ("a quick command gets every tool", "ivy/brain.py", 'if self._route.level == "quick":\n            tools =', 'if False:\n            tools ='),
    ("refusals reach Romeu", "ivy/brain.py", "refused = not calls and bool(_REFUSAL.match(content)) and len(content) < _REFUSAL_LENGTH", "refused = False"),
    ("she can end in silence", "ivy/brain.py", "if not calls and not content and not emptied:", "if False:"),
    ("a failed command counts as done", "ivy/brain.py", 'return None if intents.FAILED.search(result) else', "return"),
    ("short follow-ups get the lightest model", "ivy/router.py", "s -= 2", "s -= 6"),
    ("backup thinking is shown", "ivy/brain.py", 'held = e.tier == "backup"', "held = False"),
    ("the model can't switch", "ivy/brain.py", 'if name == "switch_model":', "if False:"),
    ("file patterns search as words", "ivy/tools.py", 'if "*" in query or "?" in query:', "if False:"),
    ("page function renamed", "ivy/ui/index.html", "window.toolDone =", "window.toolFinished ="),
]


def main():
    missed = []
    with tempfile.TemporaryDirectory(prefix="ivy-mutation-") as folder:
        copy = Path(folder) / "ivy-copy"
        shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", "*.wav"))
        (copy / ".env").unlink(missing_ok=True)
        chosen = [b for b in BUGS if not sys.argv[1:] or any(w in b[0] for w in sys.argv[1:])]
        for what, file, good, bad in chosen:
            path = copy / file
            original = path.read_text()
            if good not in original:
                print(f"STALE   {what}: the code moved, update this check", flush=True)
                missed.append(what)
                continue
            path.write_text(original.replace(good, bad, 1))
            run = subprocess.run([sys.executable, "-m", "tests.run"], cwd=copy, capture_output=True, text=True, timeout=300)
            path.write_text(original)
            caught = run.returncode != 0
            print(f"{'caught ' if caught else 'MISSED '} {what}", flush=True)
            if not caught:
                missed.append(what)
    print(f"\n{len(chosen) - len(missed)}/{len(chosen)} caught")
    sys.exit(1 if missed else 0)


if __name__ == "__main__":
    main()
