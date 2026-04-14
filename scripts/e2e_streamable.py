#!/usr/bin/env python3
"""E2E test for Streamable HTTP MCP transport."""
import json
import os
import sys
import urllib.request

TOKEN = os.environ.get("MCP_MEMORY_TOKEN") or os.environ.get("MCP_TOKEN") or ""
URL   = os.environ.get("MCP_URL") or "http://YOUR_SERVER_IP:7444/mcp"
if not TOKEN:
    from pathlib import Path as _P
    _tf = _P.home() / ".memory-sync" / "token"
    if _tf.exists():
        TOKEN = _tf.read_text().strip()
if not TOKEN:
    print("FATAL: set MCP_MEMORY_TOKEN env var", file=sys.stderr)
    sys.exit(2)

results = []
def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status, detail))
    print(f"  [{status}] {name}  {detail}")

session_id = [None]
msg_id_counter = [0]

def rpc(method, params=None, notify=False):
    msg_id_counter[0] += 1
    body = {"jsonrpc": "2.0", "method": method}
    if not notify:
        body["id"] = msg_id_counter[0]
    if params is not None:
        body["params"] = params
    headers = {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id[0]:
        headers["Mcp-Session-Id"] = session_id[0]
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        sid = r.headers.get("Mcp-Session-Id")
        if sid and not session_id[0]:
            session_id[0] = sid
        ct = r.headers.get("Content-Type", "")
        raw = r.read().decode()
        if notify:
            return None
        # SSE stream response — parse data: lines
        if "event-stream" in ct:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            return None
        return json.loads(raw) if raw else None

def extract_text(resp):
    try:
        return resp["result"]["content"][0]["text"]
    except Exception:
        return None

print("=== initialize ===")
r = rpc("initialize", {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "e2e", "version": "1.0"},
})
check("initialize OK", r is not None and "result" in r, f"session={session_id[0]}")
rpc("notifications/initialized", params={}, notify=True)

print()
print("=== tools/list ===")
r = rpc("tools/list")
tools = [t["name"] for t in r["result"]["tools"]] if r else []
check("5 tools registered",
      set(tools) == {"list_files", "read_file", "write_file", "delete_file", "search"},
      f"got {tools}")

print()
print("=== full CRUD + search ===")
r = rpc("tools/call", {"name": "write_file",
                       "arguments": {"path": "verify/stream.md",
                                     "content": "# streamable test XYZ999\n"}})
check("write_file", extract_text(r) == "written: verify/stream.md")

r = rpc("tools/call", {"name": "read_file",
                       "arguments": {"path": "verify/stream.md"}})
check("read_file", extract_text(r) == "# streamable test XYZ999\n")

r = rpc("tools/call", {"name": "list_files", "arguments": {"path": "verify"}})
txt = extract_text(r) or ""
check("list_files", "stream.md" in txt, txt.replace("\n", " | "))

r = rpc("tools/call", {"name": "search", "arguments": {"query": "XYZ999"}})
txt = extract_text(r) or ""
check("search", "XYZ999" in txt, txt.replace("\n", " | "))

r = rpc("tools/call", {"name": "delete_file",
                       "arguments": {"path": "verify/stream.md"}})
check("delete_file", extract_text(r) == "deleted: verify/stream.md")

print()
print("=== sandbox ===")
r = rpc("tools/call", {"name": "read_file",
                       "arguments": {"path": "../../etc/passwd"}})
err = extract_text(r) or ""
check("path traversal rejected",
      "not allowed" in err or "disallowed" in err, err)

r = rpc("tools/call", {"name": "read_file",
                       "arguments": {"path": "verify/x\x00.md"}})
err = extract_text(r) or ""
check("null byte rejected", "invalid" in err or "disallowed" in err, err)

print()
ok = sum(1 for _, s, _ in results if s == "PASS")
print(f"=== {ok}/{len(results)} passed ===")
sys.exit(0 if ok == len(results) else 1)
