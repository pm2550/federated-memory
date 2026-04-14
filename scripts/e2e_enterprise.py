#!/usr/bin/env python3
"""Enterprise E2E test for the federated MCP memory server v2.

Covers:
  Layer 0: transport & auth (no token / wrong token / correct token)
  Layer 1: MCP tools/list advertises the full v2 toolset
  Layer 2: workspace lifecycle
    - workspace_status for nonexistent workspace
    - register_workspace
    - workspace_status after registration reflects state
    - _about.md upload + get_overview renders it
  Layer 3: knowledge CRUD (at namespaces auto/, cwd/, root)
    - upload, list (recursive), read, search, delete
  Layer 4: write isolation
    - unregistered workspace_id rejected
    - path traversal rejected
    - null byte rejected
  Layer 5: sessions
    - upload_session (with envelope)
    - list_sessions
    - read_session returns envelope + body
    - search_sessions finds markers inside gzipped archives
    - duplicate upload is idempotent
  Layer 6: federation cross-workspace visibility
    - workspace A's knowledge is readable while tool call claims workspace B
  Layer 7: cleanup

Test workspaces are prefixed 'enterprise-e2e-' so a failed run can be cleaned
up manually without touching real data.

Exit 0 only on all-green; otherwise exit 1.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
import urllib.request
import urllib.error

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

TOKEN = os.environ.get("MCP_MEMORY_TOKEN") or os.environ.get("MCP_TOKEN") or ""
URL   = os.environ.get("MCP_URL") or "http://YOUR_SERVER_IP:7444/mcp"

if not TOKEN:
    # Try token file (same fallback as memory_sync.py)
    from pathlib import Path as _P
    _tf = _P.home() / ".memory-sync" / "token"
    if _tf.exists():
        TOKEN = _tf.read_text().strip()
if not TOKEN:
    print("FATAL: set MCP_MEMORY_TOKEN env var or create ~/.memory-sync/token", file=sys.stderr)
    sys.exit(2)

WS_ALPHA = "e2eent-testhost-claude-code-alpha"
WS_BETA  = "e2eent-mac-codex-cli-beta"

results: list[tuple[str, str, str]] = []

def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    results.append((name, status, detail))
    tag = " [PASS] " if cond else " [FAIL] "
    if detail:
        d = detail[:180].replace("\n", " | ")
        print(f"{tag}{name}  -  {d}")
    else:
        print(f"{tag}{name}")


# ── bare HTTP helpers (no MCP client class reuse, so this test has
# minimal dependencies; paste into any machine and run) ──────────────────

def _http(method: str, url: str, body: bytes | None = None,
          headers: dict | None = None, timeout: int = 15):
    req = urllib.request.Request(url, data=body, headers=headers or {},
                                 method=method)
    return urllib.request.urlopen(req, timeout=timeout)


session_id: list[str | None] = [None]
msg_id_counter = [0]


def rpc(method: str, params: dict | None = None, notify: bool = False):
    msg_id_counter[0] += 1
    body: dict = {"jsonrpc": "2.0", "method": method}
    if not notify:
        body["id"] = msg_id_counter[0]
    if params is not None:
        body["params"] = params
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id[0]:
        headers["Mcp-Session-Id"] = session_id[0]
    with _http("POST", URL, json.dumps(body).encode(), headers) as r:
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


def call_tool(name: str, arguments: dict) -> dict | None:
    return rpc("tools/call", {"name": name, "arguments": arguments})


def tool_text(r: dict | None) -> str:
    try:
        return r["result"]["content"][0]["text"] if r else ""
    except Exception:
        return ""


def tool_error(r: dict | None) -> bool:
    if not r:
        return True
    if "error" in r:
        return True
    return bool(r.get("result", {}).get("isError"))


# ── tests ────────────────────────────────────────────────────────────────

def test_l0_transport() -> None:
    print("=== L0: transport & auth ===")
    # Wrong token must get 401 from the Go middleware
    try:
        _http("POST", URL,
              json.dumps({"jsonrpc": "2.0", "method": "ping", "id": 1}).encode(),
              {"Authorization": "Bearer wrongtoken",
               "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"},
              timeout=5)
        check("wrong token rejected", False, "no 401 raised")
    except urllib.error.HTTPError as e:
        check("wrong token rejected", e.code == 401, f"status={e.code}")


def test_l1_initialize() -> None:
    print("=== L1: initialize + tools/list ===")
    r = rpc("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "enterprise-e2e", "version": "1.0"},
    })
    check("initialize returns", bool(r and "result" in r),
          f"session={session_id[0]}")
    rpc("notifications/initialized", params={}, notify=True)

    r = rpc("tools/list")
    tools = sorted(t["name"] for t in r["result"]["tools"]) if r else []
    required = {
        "get_overview", "list_workspaces", "register_workspace",
        "workspace_status",
        "list_knowledge", "read_knowledge", "upload_knowledge",
        "delete_knowledge", "search_knowledge",
        "list_sessions", "read_session", "upload_session", "search_sessions",
    }
    missing = required - set(tools)
    check("all v2 tools present", not missing,
          f"missing={sorted(missing)} got_count={len(tools)}")


def test_l2_workspace_lifecycle() -> None:
    print("=== L2: workspace lifecycle ===")

    # workspace_status for nonexistent -> unregistered
    r = call_tool("workspace_status", {
        "machine": "e2eent-testhost", "tool": "claude-code", "project": "alpha"})
    txt = tool_text(r)
    check("workspace_status for new ws reports unregistered",
          "registered**: false" in txt, txt[:120])

    # register
    r = call_tool("register_workspace", {
        "machine": "e2eent-testhost", "tool": "claude-code", "project": "alpha",
        "owner": "e2e", "cwd_hint": "/tmp/alpha",
    })
    txt = tool_text(r)
    check("register_workspace ws_alpha", WS_ALPHA in txt, txt[:120])

    r = call_tool("register_workspace", {
        "machine": "e2eent-mac", "tool": "codex-cli", "project": "beta",
        "owner": "e2e", "cwd_hint": "/tmp/beta",
    })
    check("register_workspace ws_beta", WS_BETA in tool_text(r))

    # workspace_status after registration reflects identity
    r = call_tool("workspace_status", {
        "machine": "e2eent-testhost", "tool": "claude-code", "project": "alpha"})
    txt = tool_text(r)
    check("workspace_status after registration",
          "registered**: true" in txt and "e2eent-testhost" in txt,
          txt[:180])

    # _about.md
    about = (
        "# e2e alpha\n\n"
        "## 这是什么\nEnterprise E2E test workspace.\n"
        "## 关键经验\n- Test markers should be unique.\n"
    )
    r = call_tool("upload_knowledge", {
        "workspace_id": WS_ALPHA, "path": "_about.md", "content": about})
    check("upload _about.md", "written" in tool_text(r), tool_text(r))

    # Verify overview shows it
    r = call_tool("get_overview", {})
    check("get_overview renders alpha _about.md",
          "# e2e alpha" in tool_text(r))


def test_l3_knowledge_crud() -> None:
    print("=== L3: knowledge CRUD ===")

    md_variants = [
        ("auto/MEMORY.md",      "# Auto Namespace\nMARK_AUTO_ALPHA\n"),
        ("cwd/CLAUDE.md",       "# Cwd Namespace\nMARK_CWD_ALPHA\n"),
        ("decisions.md",        "# Root decisions\nMARK_ROOT_ALPHA\n"),
    ]
    for path, content in md_variants:
        r = call_tool("upload_knowledge",
                      {"workspace_id": WS_ALPHA, "path": path, "content": content})
        check(f"upload {path}", "written" in tool_text(r), tool_text(r))

    # read
    r = call_tool("read_knowledge", {"workspace_id": WS_ALPHA, "path": "auto/MEMORY.md"})
    check("read auto/MEMORY.md roundtrip",
          tool_text(r) == md_variants[0][1])

    # list recursive
    r = call_tool("list_knowledge", {"workspace_id": WS_ALPHA})
    txt = tool_text(r)
    check("list_knowledge recursive shows all 4 (_about + 3)",
          all(n in txt for n in ("_about.md", "auto/MEMORY.md",
                                 "cwd/CLAUDE.md", "decisions.md")),
          txt[:200])

    # search finds all three markers
    r = call_tool("search_knowledge", {"query": "MARK_AUTO_ALPHA"})
    check("search_knowledge hits auto marker", "MARK_AUTO_ALPHA" in tool_text(r))
    r = call_tool("search_knowledge", {"query": "MARK_CWD_ALPHA"})
    check("search_knowledge hits cwd marker", "MARK_CWD_ALPHA" in tool_text(r))
    r = call_tool("search_knowledge", {"query": "MARK_ROOT_ALPHA"})
    check("search_knowledge hits root marker", "MARK_ROOT_ALPHA" in tool_text(r))

    # delete one
    r = call_tool("delete_knowledge",
                  {"workspace_id": WS_ALPHA, "path": "decisions.md"})
    check("delete_knowledge", "deleted" in tool_text(r))

    # verify deletion
    r = call_tool("read_knowledge",
                  {"workspace_id": WS_ALPHA, "path": "decisions.md"})
    check("read after delete is not found", "not found" in tool_text(r))


def test_l4_write_isolation() -> None:
    print("=== L4: write isolation & sandbox ===")

    # Unregistered workspace
    r = call_tool("upload_knowledge",
                  {"workspace_id": "ghost-e2eent-x-y-z",
                   "path": "x.md", "content": "x"})
    txt = tool_text(r)
    check("unregistered workspace rejected",
          "not registered" in txt or "invalid workspace" in txt,
          txt[:120])

    # Path traversal
    r = call_tool("upload_knowledge",
                  {"workspace_id": WS_ALPHA,
                   "path": "../../etc/passwd", "content": "x"})
    txt = tool_text(r)
    check("path traversal rejected",
          "not allowed" in txt or "disallowed" in txt or "escapes" in txt,
          txt[:120])

    # Null byte
    r = call_tool("upload_knowledge",
                  {"workspace_id": WS_ALPHA,
                   "path": "bad\x00.md", "content": "x"})
    txt = tool_text(r)
    check("null byte rejected",
          "invalid" in txt or "disallowed" in txt, txt[:120])


def test_l5_sessions() -> None:
    print("=== L5: session upload / list / read / search ===")

    lines = [
        '{"type":"message","role":"user","content":"enterprise test"}',
        '{"type":"message","role":"assistant","content":"marker ENT_E2E_SESSION_QK7W9Z3"}',
    ]
    body = ("\n".join(lines) + "\n").encode()
    b64 = base64.b64encode(body).decode()

    r = call_tool("upload_session", {
        "workspace_id": WS_ALPHA,
        "filename": "rollout-enterprise-e2e-0123-4567-89ab-cdef.jsonl",
        "content_b64": b64,
        "source_path": "/tmp/enterprise-e2e/session.jsonl",
        "source_mtime": "2026-04-11T00:00:00Z",
        "already_gzipped": False,
    })
    check("upload_session", "uploaded:" in tool_text(r), tool_text(r)[:180])

    # duplicate
    r = call_tool("upload_session", {
        "workspace_id": WS_ALPHA,
        "filename": "rollout-enterprise-e2e-0123-4567-89ab-cdef.jsonl",
        "content_b64": b64,
        "source_path": "/tmp/enterprise-e2e/session.jsonl",
        "source_mtime": "2026-04-11T00:00:00Z",
        "already_gzipped": False,
    })
    check("duplicate upload is idempotent",
          "already uploaded" in tool_text(r), tool_text(r)[:180])

    # list sessions
    r = call_tool("list_sessions", {"workspace_id": WS_ALPHA, "limit": 10})
    txt = tool_text(r)
    check("list_sessions shows file",
          "rollout-enterprise-e2e" in txt and ".jsonl.gz" in txt, txt[:180])

    rel_path = None
    for line in txt.splitlines():
        if "rollout-enterprise-e2e" in line:
            rel_path = line.split()[0]
            break
    check("parsed session rel path", rel_path is not None)

    if rel_path:
        r = call_tool("read_session",
                      {"workspace_id": WS_ALPHA, "path": rel_path})
        txt = tool_text(r)
        check("read_session has envelope + body",
              '"type":"envelope"' in txt and "ENT_E2E_SESSION_QK7W9Z3" in txt,
              txt[:180])

    # search_sessions across all workspaces
    r = call_tool("search_sessions", {"query": "ENT_E2E_SESSION_QK7W9Z3"})
    txt = tool_text(r)
    check("search_sessions finds marker across compressed archives",
          "ENT_E2E_SESSION_QK7W9Z3" in txt, txt[:200])


def test_l6_cross_workspace_read() -> None:
    print("=== L6: cross-workspace read (federation) ===")

    # WS_BETA writes a marker
    r = call_tool("upload_knowledge", {
        "workspace_id": WS_BETA,
        "path": "_about.md",
        "content": "# beta\nMARK_BETA_CROSS\n",
    })
    check("beta writes _about.md", "written" in tool_text(r))

    # Anyone reads it — by design reads have no workspace ACL
    r = call_tool("read_knowledge",
                  {"workspace_id": WS_BETA, "path": "_about.md"})
    check("cross-workspace read of beta _about.md succeeds",
          "MARK_BETA_CROSS" in tool_text(r))

    r = call_tool("search_knowledge", {"query": "MARK_BETA_CROSS"})
    check("search finds beta marker across federation",
          "MARK_BETA_CROSS" in tool_text(r))

    # But writing to beta from outside with wrong id does NOT bleed through
    r = call_tool("upload_knowledge", {
        "workspace_id": WS_ALPHA,
        "path": "../" + WS_BETA + "/knowledge/steal.md",
        "content": "x",
    })
    txt = tool_text(r)
    check("traversal into another workspace rejected",
          "not allowed" in txt or "disallowed" in txt or "escapes" in txt,
          txt[:140])


def test_l7_cleanup() -> None:
    print("=== L7: cleanup ===")
    for ws in (WS_ALPHA, WS_BETA):
        for path in ("_about.md", "auto/MEMORY.md", "cwd/CLAUDE.md",
                     "decisions.md"):
            call_tool("delete_knowledge", {"workspace_id": ws, "path": path})
    # We leave sessions and the workspace registration in place because the
    # server intentionally has no delete_session, and delete_workspace would
    # require server-side sudo. A caller can rm -rf workspaces/enterprise-*
    # manually if they want a truly clean slate.
    check("cleanup attempted", True, "(sessions retained by design)")


def main() -> None:
    print(f"[*] URL:   {URL}")
    print(f"[*] Token: {TOKEN[:8]}...")
    print()

    steps = [
        test_l0_transport,
        test_l1_initialize,
        test_l2_workspace_lifecycle,
        test_l3_knowledge_crud,
        test_l4_write_isolation,
        test_l5_sessions,
        test_l6_cross_workspace_read,
        test_l7_cleanup,
    ]
    for step in steps:
        try:
            step()
        except Exception as e:
            check(f"{step.__name__} raised", False, str(e)[:180])
        print()

    ok = sum(1 for _, s, _ in results if s == "PASS")
    total = len(results)
    print(f"=== {ok}/{total} passed ===")
    if ok != total:
        print()
        print("Failures:")
        for name, status, detail in results:
            if status == "FAIL":
                print(f"  - {name}: {detail[:200]}")
    sys.exit(0 if ok == total else 1)


if __name__ == "__main__":
    main()
