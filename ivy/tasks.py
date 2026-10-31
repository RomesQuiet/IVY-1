# ivy/tasks.py
"""
Background tasks: "research standing desks in the background".

Each task gets its own Brain and conversation and runs on one worker thread, one at a
time, because the free token budget can't feed several. Interactive requests go first.
A task can't touch the mouse, keyboard or windows, since Romeu is using them. When it
finishes, the result goes to the chat and into a file in REPORTS_DIR.

Tasks live in memory: quitting Ivy ends them.
"""

import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config
from .toolkit import describe_call, tool


_BUILDING = re.compile(r"\b(build|create|make|generate|write|develop|code)\b.{0,80}\b(game|app|project|"
                       r"script|program|website|tool|bot|platformer|site)\b", re.I | re.S)


@dataclass
class Task:
    id: int
    goal: str
    status: str = "queued"          # queued, running, done, failed, stopped
    step: str = ""
    result: str = ""
    report: Path | None = None
    started: float = 0.0
    finished: float = 0.0
    brain: object = None


_tasks: dict[int, Task] = {}
_queue: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None
_confirm = None
_on_done = None


def configure(confirm=None, on_done=None):
    """confirm(title, detail) -> bool for risky steps; on_done(task) when one ends."""
    global _confirm, _on_done
    _confirm, _on_done = confirm, on_done


def active() -> list[Task]:
    return [t for t in _tasks.values() if t.status in ("queued", "running")]


def _save_report(task: Task) -> Path | None:
    name = re.sub(r"[^\w -]", "", task.goal)[:50].strip() or f"task {task.id}"
    body = f"# {task.goal}\n\n{task.result}\n"
    for folder in (config.REPORTS_DIR, config.ROOT / "reports"):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{time.strftime('%Y-%m-%d %H%M')} {name}.md"
            path.write_text(body)
            return path
        except OSError:
            continue        # Documents can be blocked by macOS privacy settings
    return None


def _made_files(task: Task) -> bool:
    return "write_file" in task.brain.tools_used


def _run(task: Task):
    from .brain import Brain

    def ask(title, detail):
        return bool(_confirm) and _confirm(f"Background task {task.id}: {title}", detail)

    def on_tool(name, args):
        task.step = describe_call(name, args)

    task.status, task.started = "running", time.time()
    task.brain = Brain(confirm=ask, background=True)
    try:
        task.result = task.brain.chat(task.goal, on_tool=on_tool)
        if _BUILDING.search(task.goal) and task.brain.outcome == "done" and not _made_files(task):
            # it described the work instead of doing it: hold it to the real thing, once
            task.result = task.brain.chat(
                "No files were created, so nothing was built. Do it now: create the real files "
                "with write_file, run them, then report.", on_tool=on_tool)
            if not _made_files(task):
                task.brain.outcome = "no_files"
                task.result = "Nothing was built: no files were created."
        task.status = {"done": "done", "stopped": "stopped"}.get(task.brain.outcome, "failed")
    except Exception as e:
        task.result, task.status = f"The task hit an error: {e}", "failed"
    task.finished = time.time()
    if task.status == "failed" and task.brain and "write_file" in task.brain.tools_used:
        task.result += f"\n\nPartial work is in {config.WORKSPACE}."
    if task.status == "done":
        task.report = _save_report(task)
    if _on_done and task.status != "stopped":
        _on_done(task)


def _loop():
    while True:
        task = _queue.get()
        if task.status == "queued":
            _run(task)


def start(goal: str) -> Task:
    global _worker
    task = Task(max(_tasks, default=0) + 1, " ".join(goal.split()))
    _tasks[task.id] = task
    _queue.put(task)
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_loop, daemon=True, name="tasks")
        _worker.start()
    return task


@tool(confirm="task", goal="The whole job in one self-contained instruction, with every detail needed")
def start_task(goal: str) -> str:
    """Run a long job in the background so Romeu can keep working: research, comparisons,
    reports, anything taking many steps that doesn't need the screen, mouse or keyboard."""
    task = start(goal)
    ahead = len(active()) - 1
    wait = f" It starts after {ahead} other task{'s' if ahead != 1 else ''}." if ahead else ""
    return f"Started task {task.id} in the background.{wait} I'll tell you when it's done."


@tool(group="tasks")
def list_tasks() -> str:
    """What background tasks are running or finished, and what each is doing now."""
    if not _tasks:
        return "No background tasks."
    now = time.time()
    lines = []
    for t in _tasks.values():
        if t.status == "running":
            detail = f"running for {int(now - t.started)}s" + (f", now: {t.step}" if t.step else "")
        else:
            detail = t.status
        lines.append(f"Task {t.id} ({detail}): {t.goal[:80]}")
    return "\n".join(lines)


@tool(group="tasks", task_id="Which task; 0 = the one running now")
def cancel_task(task_id: int = 0) -> str:
    """Stop a background task."""
    running = active()
    task = _tasks.get(task_id) if task_id else (running[0] if running else None)
    if not task or task.status not in ("queued", "running"):
        return "There's no such task running."
    if task.brain:
        task.brain.cancel()
    task.status = "stopped"
    return f"Stopped task {task.id}."
