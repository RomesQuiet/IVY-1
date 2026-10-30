# ivy/mcp_client.py
"""
MCP client: use any Model Context Protocol server as Ivy's tools.

List servers in mcp_servers.json in the project root — the same format Claude Desktop uses:

    {"mcpServers": {
        "filesystem": {"command": "npx",
                       "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/romeu/Documents"]},
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                   "env": {"GITHUB_TOKEN": "..."}}
    }}

Local (stdio) servers only. Each server's tools become their own group, loaded when a
request mentions the server or the model asks for it, so they cost no tokens otherwise.
Their output is treated as outside content and their actions ask first by default.
Add "trusted": true to a server to skip the confirmation for it.
"""

import atexit
import json
import os
import queue
import re
import shutil
import subprocess
import threading
from pathlib import Path

from . import config, toolkit

PROTOCOL_VERSION = "2024-11-05"
CONFIG_PATH = config.ROOT / "mcp_servers.json"
_servers: dict[str, "Server"] = {}
_errors: dict[str, str] = {}


class Server:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self._next_id = 0
        self._lock = threading.Lock()
        self._waiting: dict[int, queue.Queue] = {}
        env = {**os.environ, **{k: str(v) for k, v in spec.get("env", {}).items()}}
        # apps started from Finder get a bare PATH, which misses npx, uvx and friends
        env["PATH"] = os.pathsep.join([str(Path.home() / ".local/bin"), "/opt/homebrew/bin",
                                       "/usr/local/bin", env.get("PATH", "")])
        command = shutil.which(spec["command"], path=env["PATH"]) or spec["command"]
        self.proc = subprocess.Popen([command, *spec.get("args", [])], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1, env=env, cwd=spec.get("cwd"))
        threading.Thread(target=self._read_loop, daemon=True, name=f"mcp-{name}").start()
        self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "ivy", "version": "1.0"},
        }, timeout=90)    # npx may have to download the server the first time
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, message: dict):
        with self._lock:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()

    def _read_loop(self):
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue    # servers sometimes log to stdout
            waiter = self._waiting.pop(message.get("id"), None)
            if waiter:
                waiter.put(message)
        for waiter in list(self._waiting.values()):
            waiter.put({"error": {"message": "the server stopped"}})

    def request(self, method: str, params: dict, timeout: float = 60) -> dict:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
        answer: queue.Queue = queue.Queue()
        self._waiting[rid] = answer
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            message = answer.get(timeout=timeout)
        except queue.Empty:
            self._waiting.pop(rid, None)
            raise TimeoutError(f"{self.name} didn't answer {method} within {timeout:.0f}s")
        if "error" in message:
            raise RuntimeError(message["error"].get("message", str(message["error"])))
        return message.get("result", {})

    def tools(self) -> list[dict]:
        found, cursor = [], None
        while True:
            page = self.request("tools/list", {"cursor": cursor} if cursor else {})
            found += page.get("tools", [])
            cursor = page.get("nextCursor")
            if not cursor:
                return found

    def call(self, tool: str, arguments: dict) -> str:
        result = self.request("tools/call", {"name": tool, "arguments": arguments}, timeout=55)
        text = "\n".join(c.get("text", f"[{c.get('type')} content]") for c in result.get("content", []))
        if result.get("isError"):
            raise RuntimeError(text or "the tool reported an error")
        return text or "(done)"

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()


def _clean_schema(schema: dict | None) -> dict:
    """The model only needs the shape; dropping metadata saves tokens on every call."""
    schema = dict(schema or {"type": "object", "properties": {}})
    for key in ("$schema", "additionalProperties", "title"):
        schema.pop(key, None)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def _connect(name: str, spec: dict):
    server = Server(name, spec)
    _servers[name] = server
    group = f"mcp_{re.sub(r'[^a-zA-Z0-9]', '_', name)}"
    tools = server.tools()

    for t in tools:
        def call(_server=server, _tool=t["name"], **arguments):
            return _server.call(_tool, arguments)
        toolkit.register(f"{group}__{t['name']}"[:64], call, t.get("description", t["name"]),
                         _clean_schema(t.get("inputSchema")), group,
                         confirm=None if spec.get("trusted") else "action")
    toolkit.GROUPS[group] = (f"tools from the {name} MCP server ({len(tools)})",
                             rf"\b{re.escape(name.lower())}\b")
    print(f"🔌 MCP server '{name}': {len(tools)} tools")


def load_all():
    """Connect to every configured server. Slow ones don't hold the others up."""
    if not CONFIG_PATH.exists():
        return
    try:
        servers = json.loads(CONFIG_PATH.read_text()).get("mcpServers", {})
    except ValueError as e:
        print(f"⚠️  mcp_servers.json isn't valid JSON: {e}")
        return

    def connect(name, spec):
        try:
            _connect(name, spec)
        except Exception as e:
            _errors[name] = str(e)
            print(f"⚠️  MCP server '{name}' failed: {e}")

    workers = [threading.Thread(target=connect, args=item, daemon=True) for item in servers.items()]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=100)


def status() -> list[str]:
    lines = [f"{n}: connected" for n in _servers] + [f"{n}: failed — {e}" for n, e in _errors.items()]
    return lines or [f"none configured (add them to {CONFIG_PATH.name})"]


@atexit.register
def _shutdown():
    for s in _servers.values():
        s.close()
