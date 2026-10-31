"""Minimal MCP server over stdio, for testing Ivy's client."""
import json
import sys

TOOLS = [
    {"name": "get_secret", "description": "Returns the test secret word.",
     "inputSchema": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                     "properties": {"topic": {"type": "string"}}, "required": ["topic"],
                     "additionalProperties": False}},
    {"name": "always_fails", "description": "Always reports an error.",
     "inputSchema": {"type": "object", "properties": {}}},     # no "required": real servers omit it
]

print("a log line that isn't JSON", flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif msg["params"]["name"] == "always_fails":
        result = {"content": [{"type": "text", "text": "disk on fire"}], "isError": True}
    else:
        topic = msg["params"]["arguments"].get("topic")
        result = {"content": [{"type": "text", "text": f"The secret about {topic} is heliotrope."}]}
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
