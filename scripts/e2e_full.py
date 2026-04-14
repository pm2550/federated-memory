#!/usr/bin/env python3
"""Comprehensive E2E test for memory-server."""
import json
import os
import ssl
import sys
import threading
import time
import urllib.request
import urllib.error

TOKEN = os.environ.get("MCP_MEMORY_TOKEN") or os.environ.get("MCP_TOKEN") or ""
BASE = os.environ.get("MCP_URL") or "https://YOUR_SERVER_IP:7443"
CTX = ssl._create_unverified_context()
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

def http_code(url, headers=None, timeout=5):
    """Return HTTP status or 0 on connection failure."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0

print("=== Layer 1: HTTP auth enforcement ===")
# No auth header → nginx 444 (connection closed) → urllib sees 0
code = http_code(BASE + "/")
check("no auth → connection closed", code == 0, f"got {code}")

# Wrong token → 401 from Go middleware
code = http_code(BASE + "/sse", headers={"Authorization": "Bearer wrongtoken"})
check("wrong token → 401", code == 401, f"got {code}")

# Right token → 200
code = http_code(BASE + "/sse", headers={"Authorization": "Bearer " + TOKEN}, timeout=3)
check("right token → 200 (or timeout=stream open)", code in (200, 0), f"got {code}")

print()
print("=== Layer 2: MCP protocol E2E ===")

sse_events = []
session_id_holder = [None]

def read_sse():
    try:
        req = urllib.request.Request(BASE + "/sse",
            headers={"Authorization": "Bearer " + TOKEN})
        with urllib.request.urlopen(req, context=CTX, timeout=20) as r:
            current = {}
            for raw in r:
                line = raw.decode().rstrip()
                if not line:
                    if current:
                        sse_events.append(current)
                        if current.get("event") == "endpoint":
                            d = current.get("data", "")
                            if "sessionId=" in d:
                                session_id_holder[0] = d.split("sessionId=")[1]
                    current = {}
                    continue
                if line.startswith("event:"):
                    current["event"] = line[6:].strip()
                elif line.startswith("data:"):
                    current["data"] = line[5:].strip()
    except Exception:
        pass

threading.Thread(target=read_sse, daemon=True).start()
for _ in range(50):
    if session_id_holder[0]: break
    time.sleep(0.1)

session = session_id_holder[0]
check("SSE session established", session is not None, f"sessionId={session}")
if not session: sys.exit(1)

def post(method, params=None, msg_id=1):
    body = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None: body["id"] = msg_id
    if params is not None: body["params"] = params
    req = urllib.request.Request(
        f"{BASE}/message?sessionId={session}",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + TOKEN,
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, context=CTX, timeout=5) as r:
        return r.read().decode() or ""

def find_response(msg_id):
    """Find response in received SSE events."""
    for _ in range(20):
        for ev in sse_events:
            if ev.get("event") == "message":
                try:
                    d = json.loads(ev.get("data", ""))
                    if d.get("id") == msg_id:
                        return d
                except Exception:
                    pass
        time.sleep(0.1)
    return None

def extract_text(resp):
    try:
        return resp["result"]["content"][0]["text"]
    except Exception:
        return None

# initialize
post("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "e2e", "version": "1.0"}}, msg_id=1)
r = find_response(1)
check("initialize response", r is not None and "result" in r)

post("notifications/initialized", params={}, msg_id=None)
time.sleep(0.3)

# tools/list
post("tools/list", msg_id=2)
r = find_response(2)
tools = [t["name"] for t in r["result"]["tools"]] if r else []
check("tools/list returns 5 tools",
      set(tools) == {"list_files", "read_file", "write_file", "delete_file", "search"},
      f"got {tools}")

# write_file
post("tools/call", {"name": "write_file",
                    "arguments": {"path": "verify/a.md", "content": "# marker ABC123\n"}}, msg_id=3)
r = find_response(3)
check("write_file", extract_text(r) == "written: verify/a.md")

# read_file
post("tools/call", {"name": "read_file",
                    "arguments": {"path": "verify/a.md"}}, msg_id=4)
r = find_response(4)
check("read_file returns content", extract_text(r) == "# marker ABC123\n")

# list_files
post("tools/call", {"name": "list_files",
                    "arguments": {"path": "verify"}}, msg_id=5)
r = find_response(5)
txt = extract_text(r) or ""
check("list_files shows file", "verify/a.md" in txt, txt.replace("\n", " | "))

# search
post("tools/call", {"name": "search",
                    "arguments": {"query": "ABC123"}}, msg_id=6)
r = find_response(6)
txt = extract_text(r) or ""
check("search finds marker", "ABC123" in txt, txt.replace("\n", " | "))

print()
print("=== Layer 3: Sandbox (path traversal) ===")

# path traversal
post("tools/call", {"name": "read_file",
                    "arguments": {"path": "../../etc/passwd"}}, msg_id=7)
r = find_response(7)
err_txt = extract_text(r) or ""
check("../../etc/passwd rejected", "not allowed" in err_txt or "disallowed" in err_txt or r.get("result", {}).get("isError"), err_txt)

# absolute path attempt — rejected because of disallowed ':'
post("tools/call", {"name": "read_file",
                    "arguments": {"path": "/etc/passwd"}}, msg_id=8)
r = find_response(8)
err_txt = extract_text(r) or ""
check("/etc/passwd rejected", "escapes" in err_txt or "disallowed" in err_txt or "not found" in err_txt, err_txt)

# null byte
post("tools/call", {"name": "read_file",
                    "arguments": {"path": "verify/a.md\x00.txt"}}, msg_id=9)
r = find_response(9)
err_txt = extract_text(r) or ""
check("null byte rejected",
      "invalid" in err_txt or "disallowed" in err_txt,
      err_txt)

print()
print("=== Layer 4: delete_file ===")

post("tools/call", {"name": "delete_file",
                    "arguments": {"path": "verify/a.md"}}, msg_id=10)
r = find_response(10)
check("delete_file", extract_text(r) == "deleted: verify/a.md")

# Re-read should fail
post("tools/call", {"name": "read_file",
                    "arguments": {"path": "verify/a.md"}}, msg_id=11)
r = find_response(11)
err_txt = extract_text(r) or ""
check("read after delete fails", "not found" in err_txt, err_txt)

# delete directory should fail
post("tools/call", {"name": "delete_file",
                    "arguments": {"path": "verify"}}, msg_id=12)
r = find_response(12)
err_txt = extract_text(r) or ""
check("delete directory rejected", "directories" in err_txt or "not found" in err_txt, err_txt)

print()
print("=== Summary ===")
ok = sum(1 for _, s, _ in results if s == "PASS")
total = len(results)
print(f"{ok}/{total} passed")
sys.exit(0 if ok == total else 1)
