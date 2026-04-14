#!/usr/bin/env python3
"""E2E test for the federated MCP tools (v2)."""
import base64
import io
import json
import os
import sys
import urllib.request

# Force UTF-8 on Windows consoles
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

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

session_id = [None]
msg_id_counter = [0]
results = []

def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status, detail))
    prefix = "  [" + status + "]"
    if detail:
        print(f"{prefix} {name}  - {detail[:200]}")
    else:
        print(f"{prefix} {name}")

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
    with urllib.request.urlopen(req, timeout=15) as r:
        sid = r.headers.get("Mcp-Session-Id")
        if sid and not session_id[0]:
            session_id[0] = sid
        ct = r.headers.get("Content-Type", "")
        raw = r.read().decode()
        if notify:
            return None
        if "event-stream" in ct:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            return None
        return json.loads(raw) if raw else None

def call(name, args):
    return rpc("tools/call", {"name": name, "arguments": args})

def extract_text(resp):
    try:
        return resp["result"]["content"][0]["text"]
    except Exception:
        return None

# ── init ──────────────────────────────────────────────────────────────────
rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "fed-e2e", "version": "1"}})
rpc("notifications/initialized", params={}, notify=True)

print("=== tools registered ===")
r = rpc("tools/list")
tools = sorted(t["name"] for t in r["result"]["tools"])
print("  " + "\n  ".join(tools))
required = {"get_overview", "list_workspaces", "register_workspace",
            "list_knowledge", "read_knowledge", "upload_knowledge",
            "delete_knowledge", "search_knowledge",
            "list_sessions", "read_session", "upload_session", "search_sessions"}
check("all federation tools present", required.issubset(set(tools)),
      f"missing={sorted(required - set(tools))}")

# ── overview on empty system ──────────────────────────────────────────────
print()
print("=== get_overview (empty) ===")
r = call("get_overview", {})
txt = extract_text(r) or ""
check("overview contains /INDEX.md", "/INDEX.md" in txt)
check("overview contains /README.md", "/README.md" in txt)
check("overview lists zero workspaces", "(none registered yet" in txt)

# ── register workspace ───────────────────────────────────────────────────
print()
print("=== register_workspace ===")
r = call("register_workspace", {
    "machine": "testhost",
    "tool": "claude-code",
    "tool_version": "2.1.87",
    "project": "fed-test",
    "owner": "testuser",
    "cwd_hint": "/tmp/fed-test",
})
txt = extract_text(r) or ""
check("register ok", "testhost-claude-code-fed-test" in txt, txt[:120])
ws = "testhost-claude-code-fed-test"

r = call("list_workspaces", {})
txt = extract_text(r) or ""
check("list_workspaces shows new ws", ws in txt)

# ── upload_knowledge ─────────────────────────────────────────────────────
print()
print("=== knowledge CRUD ===")
md_content = "# Fed Test Knowledge\n\nThis is the MARKER ABC_FED_TEST content.\n"
r = call("upload_knowledge", {"workspace_id": ws, "path": "intro.md", "content": md_content})
txt = extract_text(r) or ""
check("upload_knowledge", "written" in txt, txt)

r = call("read_knowledge", {"workspace_id": ws, "path": "intro.md"})
check("read_knowledge roundtrip", extract_text(r) == md_content)

r = call("list_knowledge", {"workspace_id": ws})
txt = extract_text(r) or ""
check("list_knowledge shows file + title",
      "intro.md" in txt and "Fed Test Knowledge" in txt, txt)

r = call("search_knowledge", {"query": "ABC_FED_TEST"})
txt = extract_text(r) or ""
check("search_knowledge finds marker", "ABC_FED_TEST" in txt, txt[:200])

# ── wrong workspace write = rejected ─────────────────────────────────────
print()
print("=== write isolation ===")
r = call("upload_knowledge", {"workspace_id": "ghost-ws", "path": "x.md", "content": "x"})
txt = extract_text(r) or ""
check("upload to unregistered ws rejected",
      "not registered" in txt or "invalid workspace" in txt, txt[:150])

# path traversal
r = call("upload_knowledge", {"workspace_id": ws, "path": "../other.md", "content": "x"})
txt = extract_text(r) or ""
check("../other.md rejected in knowledge",
      "not allowed" in txt or "disallowed" in txt, txt[:100])

# ── upload_session ───────────────────────────────────────────────────────
print()
print("=== session upload ===")
# Fake a small JSONL session
lines = [
    '{"type":"message","role":"user","content":"hello"}',
    '{"type":"message","role":"assistant","content":"hi MARKER_SESSION_XYZ"}',
]
body = "\n".join(lines) + "\n"
b64 = base64.b64encode(body.encode()).decode()
r = call("upload_session", {
    "workspace_id": ws,
    "filename": "rollout-2026-04-11T08-30-00-abcd1234-ef56-7890-abcd-123456789012.jsonl",
    "content_b64": b64,
    "source_path": "/tmp/fake/rollout.jsonl",
    "source_mtime": "2026-04-11T08:30:00Z",
    "already_gzipped": False,
})
txt = extract_text(r) or ""
check("upload_session", "uploaded:" in txt, txt[:200])

# Same upload again → dedup idempotent
r = call("upload_session", {
    "workspace_id": ws,
    "filename": "rollout-2026-04-11T08-30-00-abcd1234-ef56-7890-abcd-123456789012.jsonl",
    "content_b64": b64,
    "source_path": "/tmp/fake/rollout.jsonl",
    "source_mtime": "2026-04-11T08:30:00Z",
    "already_gzipped": False,
})
txt = extract_text(r) or ""
check("duplicate upload is idempotent", "already uploaded" in txt, txt[:200])

# list
r = call("list_sessions", {"workspace_id": ws})
txt = extract_text(r) or ""
check("list_sessions shows the file",
      "2026/04/" in txt and ".jsonl.gz" in txt, txt[:200])

# Extract the exact relative path from the list output
rel_path = None
for line in txt.splitlines():
    if line.strip() and ".jsonl.gz" in line:
        rel_path = line.split()[0]
        break

# read
r = call("read_session", {"workspace_id": ws, "path": rel_path})
txt = extract_text(r) or ""
check("read_session envelope + body", '"type":"envelope"' in txt and "MARKER_SESSION_XYZ" in txt,
      txt[:200])

# search_sessions
r = call("search_sessions", {"query": "MARKER_SESSION_XYZ"})
txt = extract_text(r) or ""
check("search_sessions finds marker", "MARKER_SESSION_XYZ" in txt, txt[:200])

# ── overview now populated ────────────────────────────────────────────────
print()
print("=== overview (populated) ===")
r = call("get_overview", {})
txt = extract_text(r) or ""
check("overview lists ws", ws in txt)

# ── delete knowledge ─────────────────────────────────────────────────────
print()
print("=== delete_knowledge ===")
r = call("delete_knowledge", {"workspace_id": ws, "path": "intro.md"})
check("delete_knowledge", "deleted" in (extract_text(r) or ""))
r = call("read_knowledge", {"workspace_id": ws, "path": "intro.md"})
check("read after delete fails", "not found" in (extract_text(r) or ""))

print()
ok = sum(1 for _, s, _ in results if s == "PASS")
print(f"=== {ok}/{len(results)} passed ===")
sys.exit(0 if ok == len(results) else 1)
