#!/usr/bin/env python3
"""Integrity check: does every local Claude/Codex session file have a
corresponding archived .jsonl.gz on the server?

Walks local session directories, computes sha256 of each file, then
queries the server's list_sessions for each relevant workspace and
pulls the envelope sha256. Reports any local file whose sha256 does
NOT appear on the server.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import ssl
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

TOKEN = os.environ.get("MCP_MEMORY_TOKEN") or os.environ.get("MCP_TOKEN") or ""
URL   = os.environ.get("MCP_URL") or "http://YOUR_SERVER_IP:7444/mcp"

if not TOKEN:
    # Try token file
    for tf in [Path.home() / ".memory-sync" / "token"]:
        if tf.exists():
            TOKEN = tf.read_text().strip()
            break
if not TOKEN:
    print("FATAL: MCP_MEMORY_TOKEN not set and no token file", file=sys.stderr)
    sys.exit(2)

session_id: list[str | None] = [None]
msg_id = [0]

def rpc(method, params=None, notify=False):
    msg_id[0] += 1
    body = {"jsonrpc": "2.0", "method": method}
    if not notify:
        body["id"] = msg_id[0]
    if params is not None:
        body["params"] = params
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id[0]:
        headers["Mcp-Session-Id"] = session_id[0]
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        sid = r.headers.get("Mcp-Session-Id")
        if sid and not session_id[0]:
            session_id[0] = sid
        ct = r.headers.get("Content-Type", "")
        raw = r.read().decode("utf-8", errors="replace")
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


def tool_text(r):
    try:
        return r["result"]["content"][0]["text"]
    except Exception:
        return ""


# ── MCP init ─────────────────────────────────────────────────────────────
rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "integrity", "version": "1"}})
rpc("notifications/initialized", params={}, notify=True)

# ── Get all workspaces ───────────────────────────────────────────────────
r = call("list_workspaces", {})
ws_list = json.loads(tool_text(r))
print(f"[*] total workspaces on server: {len(ws_list)}")

# Build set of sha256 per workspace by reading each session's envelope.
# To avoid pulling every gz, use list_sessions to get file names then
# read the first line of each via read_session (which decompresses).
# For a fast check, we instead pull envelope sha256 by reading the first
# line of each archive on the server through read_session.

server_shas_by_ws: dict[str, set[str]] = defaultdict(set)
server_session_count_by_ws: dict[str, int] = {}

for w in ws_list:
    ws_id = w["id"]
    r = call("list_sessions", {"workspace_id": ws_id, "limit": 500})
    txt = tool_text(r) or ""
    if txt == "(no sessions)":
        server_session_count_by_ws[ws_id] = 0
        continue
    # Each line: "<rel_path>  <size> B  <timestamp>"
    entries = [line.split()[0] for line in txt.splitlines()
               if line.strip() and ".jsonl.gz" in line]
    server_session_count_by_ws[ws_id] = len(entries)

    for rel in entries:
        # Read the archive, pull first line, extract sha256 from envelope
        r2 = call("read_session", {"workspace_id": ws_id, "path": rel})
        body = tool_text(r2) or ""
        first_nl = body.find("\n")
        if first_nl == -1:
            continue
        try:
            env = json.loads(body[:first_nl])
            if env.get("type") == "envelope" and env.get("sha256"):
                server_shas_by_ws[ws_id].add(env["sha256"])
        except Exception:
            pass

print()
print("=== server-side session counts ===")
for ws_id in sorted(server_session_count_by_ws):
    n = server_session_count_by_ws[ws_id]
    if n:
        print(f"  {ws_id:50s} {n:4d} sessions ({len(server_shas_by_ws[ws_id])} envelope sha256s read)")

# ── Walk local Claude + Codex sessions ───────────────────────────────────
print()
print("=== local session file inventory ===")

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()

local_files: dict[Path, str] = {}

claude_root = Path.home() / ".claude" / "projects"
if claude_root.is_dir():
    for f in claude_root.rglob("*.jsonl"):
        if f.is_file():
            local_files[f] = sha256_of(f)
    print(f"  claude projects: {sum(1 for p in local_files if '.claude' in str(p))} files")

codex_root = Path.home() / ".codex" / "sessions"
if codex_root.is_dir():
    for f in codex_root.rglob("rollout-*.jsonl"):
        if f.is_file():
            local_files[f] = sha256_of(f)
    print(f"  codex sessions: {sum(1 for p in local_files if '.codex' in str(p))} files")

print(f"  total local files hashed: {len(local_files)}")

# ── Check each local file's sha256 appears in at least one server ws ────
all_server_shas: set[str] = set()
for shas in server_shas_by_ws.values():
    all_server_shas.update(shas)

missing: list[Path] = []
present: list[Path] = []
for f, sha in local_files.items():
    if sha in all_server_shas:
        present.append(f)
    else:
        missing.append(f)

print()
print(f"=== integrity result ===")
print(f"  local present on server: {len(present)}")
print(f"  local missing on server: {len(missing)}")

if missing:
    print()
    print("Missing files (first 20):")
    for f in missing[:20]:
        size = f.stat().st_size
        print(f"  {size:10d} B  {f}")
    if len(missing) > 20:
        print(f"  … and {len(missing) - 20} more")

sys.exit(0 if not missing else 1)
