# ivy/safety.py
"""
What Ivy may do without asking.

Shell commands are judged by an allowlist, not a blocklist: only commands known to be
read-only or part of building a project run freely. Anything else shows Romeu the exact
command every time, whatever the approval settings. This exists because a "keep these
apps, remove the rest" loop once ran unasked and wiped most of /Applications.
"""

import re
import shlex
from pathlib import Path

from . import config

# Places Ivy never deletes from or moves out of with a command, even with an OK:
# apps are removed one at a time with uninstall_package, which uses the Trash.
_FORBIDDEN_PATH = r"(/Applications|/System|/Library|/usr|/bin|/sbin|/private|/Users/\w+/?(\s|\"|'|$)|~/?(\s|\"|'|$)|\$HOME/?(\s|\"|'|$)|/\s|/\*)"
_DESTROY = r"\b(rm|rmdir|mv|unlink|shred|trash|srm|dd|mkfs|chown|chmod)\b|\bdelete\b|-delete\b"
_FORBIDDEN = re.compile(rf"(?is)({_DESTROY}).*{_FORBIDDEN_PATH}|{_FORBIDDEN_PATH}.*({_DESTROY})")
_PY_DESTROY = r"\b(remove|unlink|rmdir|removedirs|rmtree|rename|replace|move|delete)\s*\("
_FORBIDDEN_PY = re.compile(rf"(?is){_PY_DESTROY}.*/Applications|/Applications.*{_PY_DESTROY}")

_READ_ONLY = {"ls", "cat", "head", "tail", "wc", "grep", "egrep", "rg", "pwd", "which", "whoami", "date", "cal",
              "uptime", "df", "du", "ps", "echo", "printf", "sort", "uniq", "cut", "tr", "file", "stat",
              "mdfind", "mdls", "sw_vers", "uname", "hostname", "basename", "dirname", "realpath", "diff",
              "cd", "true", "test", "sleep", "env", "printenv", "pmset", "ifconfig", "system_profiler"}
_BUILD = {"mkdir", "touch"}
_SUBCOMMANDS = {
    "git": {"status", "log", "diff", "show", "branch", "init", "add", "commit", "remote", "tag", "ls-files"},
    "brew": {"list", "search", "info", "--version", "doctor", "outdated", "install"},
    "pip": {"list", "show", "install", "freeze", "--version"},
    "pip3": {"list", "show", "install", "freeze", "--version"},
}
_SHELL_TRICKS = re.compile(r"\$\(|`|<\(|\beval\b|\bexec\b")
_LOOPS = re.compile(r"(^|[;&|\s])(for|while|until|xargs)\b")
_PY_RISK = re.compile(r"\b(os\.(remove|unlink|rmdir|removedirs|rename|replace|system|popen|exec\w*|kill)|shutil\.|"
                      r"subprocess|send2trash|\.unlink\(|\.rmdir\(|\.rename\(|\.replace\(\s*['\"]?/|"
                      r"exec\(|eval\(|__import__|ctypes|importlib)")
_APPLESCRIPT_RISK = re.compile(r"\b(delete|erase|empty|move|duplicate|do shell script|send|quit|shut down|restart|"
                               r"log out|set volume|make new)\b", re.I)


def forbidden(command: str) -> str | None:
    """A reason this must not run at all, even with Romeu's OK."""
    if _FORBIDDEN.search(command):
        return ("NOT DONE: Ivy doesn't delete or move things in system folders, your home folder or "
                "/Applications with commands. To remove an app use uninstall_package, one app at a "
                "time; it goes to the Trash.")
    return None


def forbidden_python(code: str) -> str | None:
    if _FORBIDDEN_PY.search(code):
        return "NOT DONE: Ivy doesn't delete apps with code. Use uninstall_package, one app at a time."
    return None


def _python_command_risk(args: list[str]) -> str | None:
    """Running a script is only as safe as the script, so read it first. Otherwise Ivy could
    write a deleting script into her project folder and run it unasked."""
    if args[:2] == ["-c", args[1] if len(args) > 1 else ""]:
        return python_risk(args[1] if len(args) > 1 else "")
    if args[:1] == ["-m"]:
        return None if args[1:3] == ["pip", "install"] or args[1:2] == ["pytest"] else f"'python -m {args[1:2]}' can change things"
    script = next((a for a in args if a.endswith(".py")), None)
    if not script:
        return None if args == ["--version"] or args == ["-V"] else "it runs code Ivy can't read first"
    path = Path(script).expanduser()
    try:
        text = (path if path.is_absolute() else config.WORKSPACE / path).read_text()
    except OSError:
        matches = sorted(config.WORKSPACE.rglob(script.rsplit("/", 1)[-1]))
        if len(matches) != 1:
            return f"{script} can't be read first"
        text = matches[0].read_text()
    return python_risk(text) or ("it would delete apps" if forbidden_python(text) else None)


def _segment_risk(segment: str) -> str | None:
    try:
        words = shlex.split(segment)
    except ValueError:
        return "it can't be read safely"
    while words and re.fullmatch(r"\w+=.*", words[0]):      # VAR=value command
        words.pop(0)
    if words[:1] == ["timeout"]:
        words = words[2:]
    if not words:
        return None
    name = words[0].rsplit("/", 1)[-1]
    if name == "find":
        return "it could change files" if {"-delete", "-exec", "-execdir", "-ok"} & set(words) else None
    if name == "sed":
        return "it edits files in place" if any(w.startswith("-i") for w in words) else None
    if name in ("python", "python3"):
        return _python_command_risk(words[1:])
    if name in _READ_ONLY or name in _BUILD:
        return None
    if name in _SUBCOMMANDS:
        sub = next((w for w in words[1:] if not w.startswith("-") or w == "--version"), "")
        return None if sub in _SUBCOMMANDS[name] else f"'{name} {sub}' can change things"
    return f"'{name}' isn't on Ivy's list of safe commands"


def shell_risk(args: dict) -> str | None:
    """Why this command needs Romeu's OK each time, or None if it's safe to run."""
    command = args.get("command", "")
    if _SHELL_TRICKS.search(command):
        return "it builds part of the command at run time"
    if _LOOPS.search(command):
        return "it repeats an action over many items"
    for target in re.findall(r"(?<![<>&\d])>{1,2}\s*([^\s;&|]+)", command):
        if target.startswith(("/", "~", "$")) and target != "/dev/null" or ".." in target:
            return f"it writes to {target}"
    for segment in re.split(r"&&|\|\||[;|\n&]", re.sub(r"<<-?\s*'?\w+'?.*", "", command, flags=re.S)):
        risk = _segment_risk(re.sub(r"\d?>>?\s*\S+", "", segment).strip())
        if risk:
            return risk
    return None


def python_risk(code) -> str | None:
    text = code.get("code", "") if isinstance(code, dict) else code
    m = _PY_RISK.search(text)
    return f"this code uses {m.group(0).strip('(. ')}, which can change or delete things" if m else None


def applescript_risk(args: dict) -> str | None:
    m = _APPLESCRIPT_RISK.search(args.get("code", ""))
    return f'this script can "{m.group(1).lower()}" things' if m else None
