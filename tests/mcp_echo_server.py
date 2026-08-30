"""Minimal MCP stdio server used by the test-suite (JSON-RPC over stdin/stdout)."""
import json
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if rid is None:
            continue  # notification
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "echo", "version": "0.0.1"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [{
                "name": "echo",
                "description": "Echo the given text back",
                "inputSchema": {"type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"]},
            }]}})
        elif method == "tools/call":
            args = (msg.get("params") or {}).get("arguments") or {}
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "echo: " + str(args.get("text", ""))}],
                "isError": False}})
        else:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
