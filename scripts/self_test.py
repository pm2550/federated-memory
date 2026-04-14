#!/usr/bin/env python3
"""Self-test: exercise every MCP tool as both AI and sync client."""
import io, json, os, sys, urllib.request
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

AI_TOKEN = Path.home().joinpath(".memory-sync/token").read_text().strip()
SYNC_TOKEN = Path.home().joinpath(".memory-sync/sync_token").read_text().strip()
URL = "http://YOUR_SERVER_IP:7444/mcp"

class C:
    def __init__(self, token):
        self.url, self.token, self.sid, self.mid = URL, token, None, 0
    def rpc(self, m, p=None, notify=False):
        self.mid += 1
        b = {"jsonrpc":"2.0","method":m}
        if not notify: b["id"] = self.mid
        if p: b["params"] = p
        h = {"Authorization":f"Bearer {self.token}","Content-Type":"application/json","Accept":"application/json, text/event-stream"}
        if self.sid: h["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(self.url, json.dumps(b).encode(), h, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            s = r.headers.get("Mcp-Session-Id")
            if s and not self.sid: self.sid = s
            ct = r.headers.get("Content-Type","")
            raw = r.read().decode()
            if notify: return
            if "event-stream" in ct:
                for ln in raw.splitlines():
                    if ln.startswith("data:"): return json.loads(ln[5:].strip())
                return
            return json.loads(raw) if raw else None
    def init(self):
        self.rpc("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"self","version":"1"}})
        self.rpc("notifications/initialized", notify=True)
    def call(self, n, a=None): return self.rpc("tools/call",{"name":n,"arguments":a or {}})
    def txt(self, r):
        try: return r["result"]["content"][0]["text"]
        except: return ""

ok = 0; fail = 0
def check(name, cond, detail=""):
    global ok, fail
    if cond: ok += 1; tag = "PASS"
    else: fail += 1; tag = "FAIL"
    d = (": " + detail[:120].replace("\n"," | ")) if detail else ""
    print(f"  [{tag}] {name}{d}")

# ── AI client tests ──────────────────────────────────────────────────────
print("=== AI CLIENT ===")
ai = C(AI_TOKEN); ai.init()

r = ai.call("register_workspace", {"machine":"testmachine","repo_name":"testproject","owner":"testuser"})
check("register + bind", "bound" in ai.txt(r).lower(), ai.txt(r)[:80])

r = ai.call("write_overview", {"content":"# testproject\nSELF_TEST_MARKER_2026\n"})
check("write_overview", "updated" in ai.txt(r), ai.txt(r))

r = ai.call("upload_knowledge", {"workspace_id":"x","write_token":"x","path":"x.md","content":"x"})
check("upload_knowledge REJECTED", "SYNC_TOKEN" in ai.txt(r), ai.txt(r)[:80])

r = ai.call("upload_session", {"workspace_id":"x","write_token":"x","filename":"x.jsonl","content_b64":"e30K"})
check("upload_session REJECTED", "SYNC_TOKEN" in ai.txt(r), ai.txt(r)[:80])

r = ai.call("get_overview")
t = ai.txt(r)
check("get_overview has marker", "SELF_TEST_MARKER_2026" in t)
check("get_overview hides raw id", "testmachine-testproject" not in t and "testmachine/testproject" in t)

r = ai.call("list_workspaces")
t = ai.txt(r)
check("list_workspaces shows label", "testmachine/testproject" in t)
check("list_workspaces hides id", "testmachine-testproject" not in t)

r = ai.call("list_knowledge")
check("list_knowledge (own, empty)", ai.txt(r) in ["(empty)",""], ai.txt(r)[:80])

r = ai.call("list_knowledge", {"workspace":"all"})
check("list_knowledge (all)", len(ai.txt(r)) > 10, f"{len(ai.txt(r))} chars")

# Cross-read: list sessions across all workspaces
r = ai.call("list_sessions", {"workspace":"all"})
sessions_txt = ai.txt(r)
check("list_sessions across all",
      "(no sessions)" not in sessions_txt and ".jsonl.gz" in sessions_txt,
      f"{len(sessions_txt)} chars, first line: {sessions_txt.splitlines()[0][:80] if sessions_txt else '(empty)'}")

# Pagination: upload a test session to our own workspace via sync, then read it
import base64 as b64m
test_lines = [f'{{"line":{i},"data":"pagintest"}}' for i in range(10)]
test_body = "\n".join(test_lines) + "\n"
test_b64 = b64m.b64encode(test_body.encode()).decode()
# Upload via sync client (which we'll create inline below)
sc_pag = C(SYNC_TOKEN); sc_pag.init()
ws_pag = sc_pag.call("register_workspace", {"machine":"testmachine","repo_name":"testproject"})
wt_pag = ""
for ln in sc_pag.txt(ws_pag).splitlines():
    if ln.startswith("write_token:"): wt_pag = ln.split(":",1)[1].strip()
sc_pag.call("upload_session", {
    "workspace_id":"testmachine-testproject","write_token":wt_pag,
    "filename":"pagintest.jsonl","content_b64":test_b64,"already_gzipped":False})
# Now list own sessions
r = ai.call("list_sessions")
sess_txt = ai.txt(r)
pag_rel = None
for ln in sess_txt.splitlines():
    if "pagintest" in ln:
        # format: "[label] rel  size  time"
        if ln.startswith("["):
            pag_rel = ln[ln.index("]")+2:].split()[0]
        else:
            pag_rel = ln.split()[0]
        break
if pag_rel:
    r = ai.call("read_session", {"path": pag_rel, "offset": 1, "limit": 3})
    t = ai.txt(r)
    check("read_session pagination (limit=3)", "lines 1-3" in t, t[-80:])
else:
    check("read_session pagination (limit=3)", False, "pagintest session not found in list")

# Search — overview.md is NOT in knowledge/ so search_knowledge won't find it.
# Instead, verify search works by searching for content we'll upload via sync.
# For now, verify the search mechanism itself works (may return "no matches" if
# there's no matching knowledge md, which is fine — the tool itself works).
r = ai.call("search_knowledge", {"query":"SELF_TEST_MARKER_2026"})
check("search_knowledge runs (marker in overview not knowledge)",
      "no matches" in ai.txt(r) or "SELF_TEST_MARKER" in ai.txt(r), ai.txt(r)[:80])

r = ai.call("search_sessions", {"query":"pagination","context_lines":1})
check("search_sessions with context", len(ai.txt(r)) > 5, ai.txt(r)[:100])

# Delete own (write some knowledge first via sync, then delete as AI)
print()
print("=== SYNC CLIENT ===")
sc = C(SYNC_TOKEN); sc.init()
r = sc.call("register_workspace", {"machine":"testmachine","repo_name":"testproject"})
wt = ""
for ln in sc.txt(r).splitlines():
    if ln.startswith("write_token:"): wt = ln.split(":",1)[1].strip()
check("sync gets write_token", len(wt) > 0)

r = sc.call("upload_knowledge", {"workspace_id":"testmachine-testproject","write_token":wt,
    "path":"selftest.md","content":"# self test delete target\n"})
check("sync upload_knowledge", "written" in sc.txt(r))

# Overwrite to test versioning
r = sc.call("upload_knowledge", {"workspace_id":"testmachine-testproject","write_token":wt,
    "path":"selftest.md","content":"# self test v2\n"})
check("sync overwrite (versioning)", "written" in sc.txt(r))

# Origin-tagged upload
r = sc.call("upload_knowledge", {"workspace_id":"testmachine-testproject","write_token":wt,
    "path":"pulled/remote.md","content":"# pulled\n","origin":"pulled:oracle/tpu"})
check("sync origin-tagged upload", "written" in sc.txt(r))

# Wrong write_token
r = sc.call("upload_knowledge", {"workspace_id":"testmachine-testproject","write_token":"wrong",
    "path":"hack.md","content":"x"})
check("wrong write_token REJECTED", "mismatch" in sc.txt(r).lower(), sc.txt(r)[:80])

# Verify list shows origin tag
r = sc.call("list_knowledge", {"workspace":"testmachine/testproject"})
check("list shows origin tag", "pulled:oracle/tpu" in sc.txt(r), sc.txt(r)[:200])

# Now AI deletes
print()
print("=== AI DELETE ===")
r = ai.call("delete_knowledge", {"path":"selftest.md"})
check("AI delete own knowledge", "deleted" in ai.txt(r), ai.txt(r))

r = ai.call("delete_knowledge", {"path":"pulled/remote.md"})
check("AI delete pulled file", "deleted" in ai.txt(r), ai.txt(r))

r = ai.call("list_knowledge")
check("knowledge empty after delete", ai.txt(r).strip() in ["(empty)",""], ai.txt(r)[:80])

# Cleanup overview
ai.call("write_overview", {"content":""})

print()
print(f"=== TOTAL: {ok}/{ok+fail} passed ===")
if fail:
    sys.exit(1)
