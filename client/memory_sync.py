#!/usr/bin/env python3
"""
Memory-sync client: upload local AI sessions to the federated memory server.

Subcommands:
  claude-hook       Read SessionEnd hook JSON from stdin, upload transcript.
  codex-scan        Scan ~/.codex/sessions for new rollout-*.jsonl files.
  register          Manually register a workspace.
  list              Print local dedup db.
  test              Smoke test the remote MCP server.

Config:
  ~/.memory-sync/config.json    MCP url, default machine name, token env var.
  ~/.memory-sync/uploaded.tsv   Dedup: sha256 <tab> workspace_id <tab> source <tab> uploaded_at.
  ~/.memory-sync/workspaces.tsv Registered workspace ids (memoized).

Env:
  MCP_MEMORY_TOKEN       Required. Bearer token for the server.
  MCP_MEMORY_URL         Override config.json url.
  MEMORY_SYNC_HOME       Override ~/.memory-sync location.
"""
from __future__ import annotations

import argparse
import base64
import glob
import gzip
import hashlib
import io
import json
import os
import platform
import re
import socket
import ssl
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

# ── Windows console: force UTF-8 so non-ASCII chars print safely ─────────
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

HOME = Path(os.environ.get("MEMORY_SYNC_HOME") or (Path.home() / ".memory-sync"))
CONFIG_PATH = HOME / "config.json"
UPLOADED_DB = HOME / "uploaded.tsv"
WS_DB = HOME / "workspaces.tsv"
LOG_PATH = HOME / "sync.log"
MANIFESTS_DIR = HOME / "manifests"
STATE_DIR = HOME / "state"
LOCK_PATH = HOME / "sync.lock"
TRACKED_TXT = HOME / "tracked.txt"

DEFAULT_URL = "https://YOUR_SERVER_IP:7443/mcp"
DEFAULT_TOKEN_ENV = "MCP_MEMORY_TOKEN"
DEFAULT_MANIFEST_INTERVAL_MIN = 1440
DEFAULT_CODEX_SCAN_INTERVAL_MIN = 1440

# Per-file size caps
MAX_KNOWLEDGE_BYTES = 900_000  # ~1 MB server limit with headroom


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts} {msg}\n"
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="", file=sys.stderr)


# ── config ───────────────────────────────────────────────────────────────

def default_machine_name() -> str:
    # Short, stable, lowercase, alnum-dash.
    name = os.environ.get("COMPUTERNAME") or platform.node() or socket.gethostname() or "host"
    return slug(name)


def load_config() -> dict:
    HOME.mkdir(parents=True, exist_ok=True)
    MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # Backfill defaults for new keys
            cfg.setdefault("manifest_interval_minutes", DEFAULT_MANIFEST_INTERVAL_MIN)
            cfg.setdefault("codex_scan_interval_minutes", DEFAULT_CODEX_SCAN_INTERVAL_MIN)
            return cfg
        except Exception:
            pass
    cfg = {
        "url": DEFAULT_URL,
        "token_env": DEFAULT_TOKEN_ENV,
        "machine": default_machine_name(),
        "owner": os.environ.get("USER") or os.environ.get("USERNAME") or "",
        "manifest_interval_minutes": DEFAULT_MANIFEST_INTERVAL_MIN,
        "codex_scan_interval_minutes": DEFAULT_CODEX_SCAN_INTERVAL_MIN,
    }
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def save_config(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get_token(cfg: dict) -> str:
    """Get the bearer token for MCP authentication.

    Priority:
      1. SYNC_TOKEN env var (for sync script uploads)
      2. SYNC_TOKEN file (~/.memory-sync/sync_token)
      3. MCP_MEMORY_TOKEN env var (fallback for hook/AI)
      4. MCP_MEMORY_TOKEN file (~/.memory-sync/token)
    """
    # Try SYNC_TOKEN first (sync scripts need this for upload permission)
    sync_tok = os.environ.get("SYNC_TOKEN", "")
    if not sync_tok:
        sync_file = HOME / "sync_token"
        if sync_file.exists():
            try:
                sync_tok = sync_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass
    if sync_tok:
        return sync_tok

    # Fallback to AI token
    env_var = cfg.get("token_env") or DEFAULT_TOKEN_ENV
    token = os.environ.get(env_var, "")
    if token:
        return token
    token_file = HOME / "token"
    if token_file.exists():
        try:
            token = token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
        except Exception as e:
            log(f"token file read error: {e}")
    log(f"FATAL: no SYNC_TOKEN or {env_var} found")
    sys.exit(2)


TOKENS_DIR = HOME / "tokens"


def get_write_token(ws_id: str) -> str:
    """Read the per-workspace write_token from ~/.memory-sync/tokens/<ws_id>.token."""
    p = TOKENS_DIR / f"{ws_id}.token"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def save_write_token(ws_id: str, token: str) -> None:
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    p = TOKENS_DIR / f"{ws_id}.token"
    p.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(str(p), 0o600)
    except Exception:
        pass


def get_url(cfg: dict) -> str:
    return os.environ.get("MCP_MEMORY_URL") or cfg.get("url") or DEFAULT_URL


# ── dedup db ─────────────────────────────────────────────────────────────
#
# uploaded.tsv schema: <sha256>\t<workspace_id>\t<source_path>\t<uploaded_at>
#
# Dedup is scoped by (workspace_id, sha256) pair, NOT by sha256 alone.
# Reason: the same md content may legitimately need to live in multiple
# workspaces (e.g. Claude's project-a and Codex's project-b both want their
# own copy of MEMORY.md under their knowledge/auto/). A global-by-sha256
# key would silently prevent the second workspace from ever receiving
# the file.
#
# Session uploads also use (workspace_id, sha256): a session file is
# always produced by one tool in one machine, so the pair is effectively
# unique anyway, but using the same logic keeps both paths consistent.

def load_uploaded_set() -> set[tuple[str, str]]:
    """Return set of (workspace_id, sha256) pairs already uploaded."""
    if not UPLOADED_DB.exists():
        return set()
    out: set[tuple[str, str]] = set()
    for line in UPLOADED_DB.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.add((parts[1], parts[0]))  # (workspace_id, sha256)
    return out


def mark_uploaded(sha: str, workspace_id: str, source: str) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    with open(UPLOADED_DB, "a", encoding="utf-8") as f:
        f.write(f"{sha}\t{workspace_id}\t{source}\t{datetime.now(timezone.utc).isoformat()}\n")


def load_registered() -> set[str]:
    if not WS_DB.exists():
        return set()
    return {
        line.split("\t")[0]
        for line in WS_DB.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    }


def mark_registered(ws_id: str) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    with open(WS_DB, "a", encoding="utf-8") as f:
        f.write(f"{ws_id}\t{datetime.now(timezone.utc).isoformat()}\n")


# ── slug ────────────────────────────────────────────────────────────────

SLUG_RE = re.compile(r"[^a-z0-9]+")

def slug(s: str, fallback: str = "x") -> str:
    s = (s or "").strip().lower()
    s = SLUG_RE.sub("-", s).strip("-")
    if not s:
        return fallback
    return s[:48]


def stable_hash8(s: str) -> str:
    """Short deterministic id for non-ASCII strings we can't slug."""
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


# ── manifest parsing ─────────────────────────────────────────────────────
#
# Manifests are stored locally at ~/.memory-sync/manifests/<ws_id>.json,
# one per workspace. They are created and maintained by AI clients using
# their own file-write tools — memory_sync.py only reads them.
#
# JSON with `//` line comments is allowed (the AI may add annotations).
#
# Schema:
#   {
#     "workspace_id": "mymachine-claude-code-myproject",
#     "machine": "mymachine",
#     "tool": "claude-code",
#     "project": "myproject",
#     "tracked":       [{"local": "...", "upload": "..."}, ...],
#     "tracked_dirs":  [{"local": "...", "upload_prefix": "auto/",
#                        "glob": "*.md"}, ...]
#   }


_COMMENT_RE = re.compile(r'^\s*//.*$', re.MULTILINE)

def _strip_jsonc_comments(text: str) -> str:
    """Remove // line comments from JSON-with-comments text.

    Does NOT strip // inside strings. Uses a simple state machine
    rather than regex to be safe.
    """
    out = []
    i, n = 0, len(text)
    in_str = False
    escape = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        # not in string
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            # Skip to end of line
            j = text.find("\n", i)
            if j == -1:
                break
            i = j  # keep newline
            continue
        out.append(c)
        i += 1
    return "".join(out)


def load_jsonc(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return json.loads(_strip_jsonc_comments(text))


def expand_user_path(p: str) -> Path:
    """Expand ~ and normalize to a Path.

    Accepts '~/foo', '~\\foo' (Windows), 'C:/foo', '/home/user/foo'.
    """
    if not p:
        return Path("")
    # Normalize backslashes to forward first so Path('~\\foo') works
    p = p.replace("\\", "/")
    if p.startswith("~/") or p == "~":
        p = str(Path.home()) + p[1:]
    return Path(p)


def manifest_path_for(ws_id: str) -> Path:
    return MANIFESTS_DIR / f"{ws_id}.json"


def load_manifests() -> list[dict]:
    """Return all manifest dicts under ~/.memory-sync/manifests/*.json."""
    if not MANIFESTS_DIR.exists():
        return []
    out = []
    for f in sorted(MANIFESTS_DIR.glob("*.json")):
        try:
            data = load_jsonc(f)
            if isinstance(data, dict):
                data["_path"] = str(f)
                out.append(data)
            else:
                log(f"manifest: {f.name} is not an object, skipped")
        except Exception as e:
            log(f"manifest: {f.name} parse error: {e}")
    return out


def expand_manifest_entries(m: dict) -> list[tuple[str, Path]]:
    """Turn a manifest into (upload_path, local_path) tuples.

    Deduplicates by upload_path (first write wins).
    """
    seen: set[str] = set()
    out: list[tuple[str, Path]] = []

    def add(upload: str, local: Path) -> None:
        upload = upload.replace("\\", "/").lstrip("/")
        if upload in seen:
            return
        seen.add(upload)
        out.append((upload, local))

    for entry in m.get("tracked") or []:
        if not isinstance(entry, dict):
            continue
        local_raw = entry.get("local") or ""
        upload = entry.get("upload") or ""
        if not local_raw or not upload:
            continue
        add(upload, expand_user_path(local_raw))

    for entry in m.get("tracked_dirs") or []:
        if not isinstance(entry, dict):
            continue
        local_raw = entry.get("local") or ""
        prefix = (entry.get("upload_prefix") or "").replace("\\", "/").strip("/")
        glob_pat = entry.get("glob") or "*.md"
        if not local_raw:
            continue
        base = expand_user_path(local_raw)
        if not base.is_dir():
            continue
        for f in sorted(base.rglob(glob_pat)):
            if not f.is_file():
                continue
            rel = f.relative_to(base).as_posix()
            upload = f"{prefix}/{rel}" if prefix else rel
            add(upload, f)

    return out


def project_from_cwd(cwd: str) -> str:
    """Derive a stable project slug from a cwd path.

    If the last path segment has ASCII letters/digits, use those.
    Otherwise (e.g. 'Desktop/non-ascii-name'), fall back to 'proj-<hash8>' using
    the raw bytes of the original segment — still stable across runs.
    """
    if not cwd:
        return "default"
    cwd_norm = cwd.replace("\\", "/")
    parts = [p for p in cwd_norm.split("/") if p]
    if not parts:
        return "default"
    last = parts[-1]
    if len(last) == 2 and last.endswith(":"):
        return "default"

    s = slug(last, fallback="")
    if s:
        return s
    # Non-ASCII-only segment (CJK etc.): stable hash of raw bytes.
    # Include drive/parent context if available so different projects
    # of the same basename still differ.
    ctx = "/".join(parts[-2:]) if len(parts) >= 2 else last
    return "proj-" + stable_hash8(ctx)


# ── MCP client ───────────────────────────────────────────────────────────

class MCPClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.session_id: str | None = None
        self.msg_id = 0
        self._ctx = ssl._create_unverified_context() if url.startswith("https:") else None

    def _post(self, body: dict, *, notify: bool = False) -> dict | None:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        data = json.dumps(body).encode()
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30, context=self._ctx) as r:
            sid = r.headers.get("Mcp-Session-Id")
            if sid and not self.session_id:
                self.session_id = sid
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

    def rpc(self, method: str, params: dict | None = None, *, notify: bool = False) -> dict | None:
        self.msg_id += 1
        body: dict = {"jsonrpc": "2.0", "method": method}
        if not notify:
            body["id"] = self.msg_id
        if params is not None:
            body["params"] = params
        return self._post(body, notify=notify)

    def initialize(self) -> None:
        r = self.rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "memory-sync", "version": "2.0"},
        })
        if not r or "result" not in r:
            raise RuntimeError(f"initialize failed: {r}")
        self.rpc("notifications/initialized", params={}, notify=True)

    def call_tool(self, name: str, arguments: dict) -> dict | None:
        return self.rpc("tools/call", {"name": name, "arguments": arguments})

    @staticmethod
    def tool_text(resp: dict | None) -> str:
        if not resp or "result" not in resp:
            return ""
        try:
            return resp["result"]["content"][0].get("text", "")
        except Exception:
            return ""

    @staticmethod
    def tool_is_error(resp: dict | None) -> bool:
        if not resp:
            return True
        if "error" in resp:
            return True
        return bool(resp.get("result", {}).get("isError"))


# ── high-level operations ───────────────────────────────────────────────

def ensure_workspace(client: MCPClient, cfg: dict, machine: str,
                     tool: str, project: str, cwd_hint: str = "") -> str:
    """Register (or re-register) a workspace and cache its write_token.

    v3: register_workspace accepts machine + tool + project (backward compat).
    workspace_id = slug(machine)-slug(tool)-slug(project).
    """
    ws_id = f"{slug(machine)}-{slug(tool)}-{slug(project)}"

    if ws_id in load_registered() and get_write_token(ws_id):
        return ws_id

    args: dict = {
        "machine": slug(machine),
        "tool": slug(tool),
        "project": slug(project),
    }
    if cfg.get("owner"):
        args["owner"] = cfg["owner"]
    if cwd_hint:
        args["cwd_hint"] = cwd_hint

    resp = client.call_tool("register_workspace", args)
    if MCPClient.tool_is_error(resp):
        log(f"register_workspace failed: {MCPClient.tool_text(resp)}")
        return ws_id

    # Parse write_token from response (only returned for SYNC_TOKEN callers)
    text = MCPClient.tool_text(resp)
    for line in text.splitlines():
        if line.startswith("write_token:"):
            wt = line.split(":", 1)[1].strip()
            if wt:
                save_write_token(ws_id, wt)
                log(f"saved write_token for {ws_id}")
            break

    mark_registered(ws_id)
    log(f"registered workspace: {ws_id}")
    return ws_id


def upload_knowledge_file(client: MCPClient, ws_id: str,
                          upload_path: str, local: Path) -> tuple[bool, str]:
    """Upload one knowledge md. Returns (uploaded_now, msg).

    v3: requires workspace_id + write_token (SYNC_TOKEN auth).
    """
    try:
        raw = local.read_bytes()
    except Exception as e:
        return False, f"read error: {e}"
    if not raw:
        return False, "empty"
    if len(raw) > MAX_KNOWLEDGE_BYTES:
        return False, f"too large ({len(raw)} B)"

    sha = hashlib.sha256(raw).hexdigest()
    if (ws_id, sha) in load_uploaded_set():
        return False, "already uploaded (local db)"

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False, "not valid utf-8"

    wt = get_write_token(ws_id)
    if not wt:
        return False, f"no write_token for {ws_id} (re-register to get one)"

    resp = client.call_tool("upload_knowledge", {
        "workspace_id": ws_id,
        "write_token": wt,
        "path": upload_path,
        "content": content,
    })
    if MCPClient.tool_is_error(resp):
        return False, f"server error: {MCPClient.tool_text(resp)}"

    mark_uploaded(sha, ws_id, str(local))
    return True, MCPClient.tool_text(resp)


# ── state cache (avoid rehashing unchanged files) ─────────────────────────
#
# Each workspace gets one JSON cache file at ~/.memory-sync/state/<ws>.json:
#   { "<absolute_local_path>": {"mtime": 169..., "size": 123, "sha256": "..."} }
#
# The cache is used to skip sha256 computation when mtime+size are unchanged.
# The authoritative dedup is still uploaded.tsv, which contains SHA256s of
# files we have seen uploaded anywhere before.

def _state_path(ws_id: str) -> Path:
    return STATE_DIR / f"{ws_id}.json"


def load_state(ws_id: str) -> dict:
    p = _state_path(ws_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(ws_id: str, state: dict) -> None:
    p = _state_path(ws_id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


def sync_manifest_once(client: MCPClient, manifest: dict) -> dict:
    """Sync one manifest. Returns stats dict.

    Algorithm:
      1. Ensure workspace is registered.
      2. Expand tracked / tracked_dirs into (upload, local) pairs.
      3. For each pair:
         - Fast-path skip if local state cache matches current mtime+size AND
           sha256 is in global uploaded.tsv.
         - Otherwise hash the file, upload if not already in uploaded.tsv,
           update the state cache either way.
      4. Save state cache back.
    """
    ws_id = manifest.get("workspace_id") or ""
    machine = manifest.get("machine") or ""
    tool = manifest.get("tool") or ""
    project = manifest.get("project") or ""
    stats = {"workspace_id": ws_id, "uploaded": 0, "skipped": 0, "errors": 0,
             "missing": 0}

    if not (machine and tool and project):
        log(f"sync: manifest missing machine/tool/project: {ws_id}")
        stats["errors"] += 1
        return stats

    expected = f"{slug(machine)}-{slug(tool)}-{slug(project)}"
    if ws_id and ws_id != expected:
        log(f"sync: manifest workspace_id={ws_id} != derived {expected}; using derived")
    ws_id = expected
    stats["workspace_id"] = ws_id

    ensure_workspace(client, load_config(), machine, tool, project, cwd_hint="")

    state = load_state(ws_id)
    new_state: dict = {}
    entries = expand_manifest_entries(manifest)
    uploaded_set = load_uploaded_set()

    for upload_path, local in entries:
        key = str(local)
        try:
            st = local.stat()
        except FileNotFoundError:
            stats["missing"] += 1
            continue
        except Exception as e:
            stats["errors"] += 1
            log(f"sync: stat fail {local}: {e}")
            continue

        prev = state.get(key) or {}
        unchanged = (prev.get("mtime") == int(st.st_mtime)
                     and prev.get("size") == st.st_size
                     and prev.get("sha256")
                     and (ws_id, prev.get("sha256")) in uploaded_set)

        if unchanged:
            new_state[key] = prev
            stats["skipped"] += 1
            continue

        ok, msg = upload_knowledge_file(client, ws_id, upload_path, local)
        if ok:
            stats["uploaded"] += 1
            log(f"sync+ {ws_id}/{upload_path}  ({local})")
        elif "already uploaded" in msg:
            stats["skipped"] += 1
        else:
            stats["errors"] += 1
            log(f"sync! {ws_id}/{upload_path}: {msg[:120]}")

        # Cache for next run, regardless of outcome
        try:
            raw = local.read_bytes()
            new_state[key] = {
                "mtime": int(st.st_mtime),
                "size": st.st_size,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "upload_path": upload_path,
            }
        except Exception:
            pass

    save_state(ws_id, new_state)
    return stats


def sync_tracked_txt(client: MCPClient, cfg: dict) -> None:
    """Read ~/.memory-sync/tracked.txt and upload any files listed there.

    Format: one local file path per line. # comments allowed. Empty lines skipped.
    Paths starting with ~ are expanded. Backslashes normalized to forward slashes.

    Workspace routing:
      - If the path is inside a Claude project dir (~/.claude/projects/<slug>/...),
        derive workspace from the slug.
      - Otherwise, use config machine + "tracked" as the workspace repo_name.

    Upload path on server: tracked/<filename> (flat, to avoid collisions
    with auto/ and cwd/ namespaces).
    """
    if not TRACKED_TXT.exists():
        return

    lines = TRACKED_TXT.read_text(encoding="utf-8", errors="replace").splitlines()
    entries: list[Path] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = expand_user_path(line)
        if p.is_file():
            entries.append(p)
        else:
            log(f"tracked.txt: not found or not a file: {line}")

    if not entries:
        return

    machine = cfg.get("machine") or default_machine_name()
    uploaded = 0
    skipped = 0
    errors = 0

    # Determine workspace for each file
    claude_projects = Path.home() / ".claude" / "projects"

    for local in entries:
        # Try to match to a Claude project
        local_str = str(local).replace("\\", "/")
        claude_proj_str = str(claude_projects).replace("\\", "/")
        ws_tool = "tracked"
        ws_project = "default"

        if local_str.startswith(claude_proj_str + "/"):
            # Extract the project slug (first dir under projects/)
            rel = local_str[len(claude_proj_str) + 1:]
            proj_slug = rel.split("/")[0]
            ws_tool = "claude-code"
            # Check if manifest-based routing can find a better name
            owner = _existing_manifest_tracking(
                str(claude_projects / proj_slug / "memory").replace("\\", "/"))
            if owner:
                prefix = f"{slug(machine)}-claude-code-"
                if owner.startswith(prefix):
                    ws_project = owner[len(prefix):]
                else:
                    ws_project = _reverse_claude_slug(proj_slug)
            else:
                ws_project = _reverse_claude_slug(proj_slug)

        ws_id = f"{slug(machine)}-{slug(ws_tool)}-{slug(ws_project)}"
        ensure_workspace(client, cfg, machine, ws_tool, ws_project)

        # Upload path: tracked/<filename>
        upload_path = "tracked/" + local.name

        ok, msg = upload_knowledge_file(client, ws_id, upload_path, local)
        if ok:
            uploaded += 1
            log(f"tracked.txt: uploaded {local.name} -> {ws_id}/{upload_path}")
        elif "already uploaded" in msg:
            skipped += 1
        else:
            errors += 1
            log(f"tracked.txt: failed {local.name}: {msg[:80]}")

    if uploaded or errors:
        log(f"tracked.txt: uploaded={uploaded} skipped={skipped} errors={errors}")


def sync_all_manifests(client: MCPClient) -> list[dict]:
    results = []
    for m in load_manifests():
        try:
            results.append(sync_manifest_once(client, m))
        except Exception as e:
            log(f"sync: manifest {m.get('_path')} raised: {e}")
    return results


def upload_session_file(client: MCPClient, ws_id: str, tool: str, path: Path,
                        tool_version: str = "") -> tuple[bool, str]:
    """Upload one session file. Returns (uploaded, msg).

    The raw bytes are gzipped client-side before base64, to stay comfortably
    under nginx body limits on large sessions. The server handles gzipped or
    plain based on already_gzipped flag.
    """
    try:
        raw = path.read_bytes()
    except Exception as e:
        return False, f"read error: {e}"
    if not raw:
        return False, "empty file"

    sha = hashlib.sha256(raw).hexdigest()
    if (ws_id, sha) in load_uploaded_set():
        return False, "already uploaded (local db)"

    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    filename = path.name
    if not (filename.endswith(".jsonl") or filename.endswith(".jsonl.gz")):
        filename = filename + ".jsonl"

    # Always gzip client-side for transport efficiency. If filename is
    # already .jsonl.gz we assume raw bytes are already gzipped content.
    already_gz = filename.endswith(".gz")
    if already_gz:
        payload = raw
    else:
        payload = gzip.compress(raw, compresslevel=6)
        already_gz = True  # we're sending gz bytes now

    wt = get_write_token(ws_id)
    if not wt:
        return False, f"no write_token for {ws_id} (re-register to get one)"

    args = {
        "workspace_id": ws_id,
        "write_token": wt,
        "filename": filename,
        "content_b64": base64.b64encode(payload).decode("ascii"),
        "source_path": str(path),
        "source_mtime": mtime,
        "tool": tool,
        "already_gzipped": already_gz,
    }
    if tool_version:
        args["tool_version"] = tool_version

    resp = client.call_tool("upload_session", args)
    text = MCPClient.tool_text(resp)
    if MCPClient.tool_is_error(resp):
        return False, f"server error: {text}"

    mark_uploaded(sha, ws_id, str(path))
    return True, text


# ── Claude hook ─────────────────────────────────────────────────────────

def cmd_claude_hook(args) -> int:
    """Read SessionEnd hook JSON from stdin; upload the transcript."""
    try:
        hook_raw = sys.stdin.read()
        hook = json.loads(hook_raw) if hook_raw.strip() else {}
    except Exception as e:
        log(f"claude-hook: bad stdin JSON: {e}")
        return 0  # never block the CLI

    transcript = hook.get("transcript_path") or ""
    cwd = hook.get("cwd") or os.getcwd()
    session_id = hook.get("session_id") or ""
    matcher = hook.get("matcher") or ""

    log(f"claude-hook: session={session_id} matcher={matcher} cwd={cwd} transcript={transcript}")

    if not transcript:
        log("claude-hook: transcript path missing in hook payload, skipping")
        return 0

    # Race window: Claude Code may fire SessionEnd before it has finished
    # flushing the final JSONL to disk. Poll for up to ~8 seconds waiting
    # for the file to appear and stop growing.
    tp = Path(transcript)
    for attempt in range(16):  # 16 * 0.5s = 8s
        if tp.exists() and tp.stat().st_size > 0:
            # Wait half a sec then check size stopped changing.
            size1 = tp.stat().st_size
            time.sleep(0.5)
            try:
                size2 = tp.stat().st_size
            except FileNotFoundError:
                continue
            if size2 == size1:
                break
        else:
            time.sleep(0.5)
    else:
        log(f"claude-hook: transcript still missing or empty after 8s, skipping: {transcript}")
        return 0

    cfg = load_config()
    machine = args.machine or cfg.get("machine") or default_machine_name()

    # Try to match this cwd's Claude project memory dir to an existing
    # manifest — so that a hand-named workspace like 'myproject' is used
    # instead of generating a fresh 'proj-<hash>' from the non-ASCII cwd.
    project = args.project or None
    if not project and cwd:
        claude_slug = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
        for variant in {claude_slug, claude_slug[:1].lower() + claude_slug[1:] if claude_slug else claude_slug}:
            mem_dir = str(Path.home() / ".claude" / "projects" / variant / "memory").replace("\\", "/")
            owner = _existing_manifest_tracking(mem_dir)
            if owner:
                # Extract the project slug from the owner workspace_id
                # (format: <machine>-<tool>-<project>)
                prefix = f"{slug(machine)}-claude-code-"
                if owner.startswith(prefix):
                    project = owner[len(prefix):]
                break
    if not project:
        project = project_from_cwd(cwd)

    client = MCPClient(get_url(cfg), get_token(cfg))
    try:
        client.initialize()
        ws_id = ensure_workspace(client, cfg, machine, "claude-code", project, cwd_hint=cwd)
        # 1) upload this session (the hook's main job)
        ok, msg = upload_session_file(client, ws_id, "claude-code", Path(transcript))
        log(f"claude-hook: session ws={ws_id} result={msg[:120]}")
        # 2) run manifest sync (picks up any md file changes since last time)
        results = sync_all_manifests(client)
        if results:
            totals = {k: sum(r.get(k, 0) for r in results)
                      for k in ("uploaded", "skipped", "errors", "missing")}
            log(f"claude-hook: manifest sync {totals}")
    except Exception as e:
        log(f"claude-hook: exception: {e}")
    return 0  # always exit 0 so hook never blocks


# ── Codex scan ──────────────────────────────────────────────────────────

def cmd_codex_scan(args) -> int:
    cfg = load_config()
    machine = args.machine or cfg.get("machine") or default_machine_name()

    codex_home = Path(args.codex_home) if args.codex_home else Path.home() / ".codex"
    sessions_root = codex_home / "sessions"
    if not sessions_root.exists():
        log(f"codex-scan: {sessions_root} does not exist, nothing to do")
        return 0

    # Find all rollout-*.jsonl files under sessions/YYYY/MM/DD/
    pattern = str(sessions_root / "**" / "rollout-*.jsonl")
    files = [Path(p) for p in glob.glob(pattern, recursive=True)]
    if not files:
        log("codex-scan: no rollout files")
        return 0

    client = MCPClient(get_url(cfg), get_token(cfg))
    client.initialize()

    ws_cache: dict[tuple[str, str], str] = {}
    uploaded_count = 0
    skipped_count = 0

    for f in sorted(files, key=lambda p: p.stat().st_mtime):
        try:
            # Peek first line to learn cwd (Codex session_meta)
            cwd = ""
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    first = fh.readline()
                if first:
                    meta = json.loads(first)
                    if meta.get("type") == "session_meta":
                        cwd = meta.get("payload", {}).get("cwd", "") or ""
            except Exception:
                pass
            project = project_from_cwd(cwd) if cwd else "default"

            key = (machine, project)
            if key not in ws_cache:
                ws_cache[key] = ensure_workspace(
                    client, cfg, machine, "codex-cli", project, cwd_hint=cwd)
            ws_id = ws_cache[key]

            ok, msg = upload_session_file(client, ws_id, "codex-cli", f)
            if ok:
                uploaded_count += 1
                log(f"codex-scan: uploaded {f.name} -> {ws_id} ({msg[:80]})")
            else:
                skipped_count += 1
                if "already uploaded" not in msg:
                    log(f"codex-scan: skipped {f.name}: {msg[:80]}")
        except Exception as e:
            log(f"codex-scan: failed {f.name}: {e}")

    log(f"codex-scan: done. uploaded={uploaded_count} skipped={skipped_count}")
    return 0


# ── Claude scan ─────────────────────────────────────────────────────────
#
# Scans ~/.claude/projects/<slug>/*.jsonl for all Claude Code sessions
# that were never captured by SessionEnd (old sessions, crashed runs,
# or versions of Claude Code whose hook never fired). Also covers
# subagent branch transcripts at */subagents/*.jsonl. Idempotent via
# per-workspace sha256 dedup.

def cmd_claude_scan(args) -> int:
    cfg = load_config()
    machine = args.machine or cfg.get("machine") or default_machine_name()

    claude_home = Path(args.claude_home) if args.claude_home else Path.home() / ".claude"
    projects_root = claude_home / "projects"
    if not projects_root.is_dir():
        log(f"claude-scan: {projects_root} does not exist, nothing to do")
        return 0

    files: list[Path] = []
    for proj in sorted(projects_root.iterdir()):
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            if f.is_file():
                files.append(f)
        # subagent branches: projects/<slug>/<session_uuid>/subagents/*.jsonl
        for sub in proj.glob("*/subagents/*.jsonl"):
            if sub.is_file():
                files.append(sub)

    if not files:
        log("claude-scan: no jsonl files")
        return 0

    client = MCPClient(get_url(cfg), get_token(cfg))
    client.initialize()

    ws_cache: dict[tuple[str, str], str] = {}
    uploaded_count = 0
    skipped_count = 0

    for f in sorted(files, key=lambda p: p.stat().st_mtime):
        try:
            # Find the project slug dir by walking up from this file until
            # we're a direct child of projects_root.
            # Walk up to find the project slug directory
            proj_slug = None
            cur = f.parent
            while cur != projects_root and cur.parent != projects_root:
                cur = cur.parent
                if cur == projects_root or cur.parent == projects_root:
                    break
            if cur.parent == projects_root:
                proj_slug = cur.name
            else:
                proj_slug = f.parent.name

            # Check if an existing manifest already tracks this project's
            # memory dir — if so, reuse its workspace id's project field
            # instead of reverse-slugging (avoids 'myproject' vs 'proj-<hash>'
            # split for non-ASCII cwd names).
            project = None
            mem_dir = str(projects_root / proj_slug / "memory").replace("\\", "/")
            owner = _existing_manifest_tracking(mem_dir)
            if owner:
                prefix = f"{slug(machine)}-claude-code-"
                if owner.startswith(prefix):
                    project = owner[len(prefix):]
            if not project:
                project = _reverse_claude_slug(proj_slug)

            key = (machine, project)
            if key not in ws_cache:
                ws_cache[key] = ensure_workspace(
                    client, cfg, machine, "claude-code", project,
                    cwd_hint=f"~/.claude/projects/{proj_slug}")
            ws_id = ws_cache[key]

            ok, msg = upload_session_file(client, ws_id, "claude-code", f)
            if ok:
                uploaded_count += 1
                log(f"claude-scan: uploaded {f.name} -> {ws_id} ({msg[:80]})")
            else:
                skipped_count += 1
                if "already uploaded" not in msg:
                    log(f"claude-scan: skipped {f.name}: {msg[:80]}")
        except Exception as e:
            log(f"claude-scan: failed {f.name}: {e}")

    log(f"claude-scan: done. uploaded={uploaded_count} skipped={skipped_count}")
    return 0


# ── sync (unified: manifests + codex scan) ──────────────────────────────

def cmd_sync(args) -> int:
    """Unified sync:
       1. Acquire simple file lock to prevent overlapping runs.
       2. Run manifest-based knowledge sync.
       3. Run codex session scan.

    Safe to run often. Idempotent. Exit 0 even on partial failure; errors
    go to sync.log so the scheduler never shows red."""
    lock_acquired = False
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        # Best-effort lock via O_EXCL. If another sync is running, skip.
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            lock_acquired = True
        except FileExistsError:
            # Stale lock check: if older than 15 min, steal it.
            try:
                age = time.time() - LOCK_PATH.stat().st_mtime
            except Exception:
                age = 0
            if age > 15 * 60:
                log(f"sync: stealing stale lock ({int(age)}s old)")
                try:
                    LOCK_PATH.unlink()
                except Exception:
                    pass
                fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                lock_acquired = True
            else:
                log("sync: another sync is running, exit")
                return 0

        cfg = load_config()
        client = MCPClient(get_url(cfg), get_token(cfg))
        client.initialize()

        # The interval config only controls the scheduler cadence; a manual
        # `sync` invocation always does real work. Set `sync_disabled: true`
        # in config.json to hard-disable manual sync if you really want to.
        if cfg.get("sync_disabled"):
            log("sync: sync_disabled=true, exit")
            return 0

        # 1. Manifest sync
        results = sync_all_manifests(client)
        if results:
            totals = {k: sum(r.get(k, 0) for r in results)
                      for k in ("uploaded", "skipped", "errors", "missing")}
            log(f"sync: manifests total={totals} across {len(results)} ws")
        else:
            log("sync: no manifests found")

        # 2. tracked.txt — AI-maintained list of extra files to sync
        try:
            sync_tracked_txt(client, cfg)
        except Exception as e:
            log(f"sync: tracked.txt failed: {e}")

        # 3. Codex session scan — reuse cmd_codex_scan logic inline
        class _Codex:
            machine = None
            codex_home = None
        try:
            cmd_codex_scan(_Codex())
        except SystemExit:
            raise
        except Exception as e:
            log(f"sync: codex scan failed: {e}")

        # 4. Claude session scan — catches sessions the SessionEnd hook
        # missed (historical sessions, crashed runs, old Claude versions).
        class _Claude:
            machine = None
            claude_home = None
        try:
            cmd_claude_scan(_Claude())
        except SystemExit:
            raise
        except Exception as e:
            log(f"sync: claude scan failed: {e}")

        return 0
    finally:
        if lock_acquired:
            try:
                LOCK_PATH.unlink()
            except Exception:
                pass


# ── scan-knowledge (manual / diagnostic) ───────────────────────────────

def cmd_init_manifest(args) -> int:
    """Write a starter manifest for a workspace if one doesn't exist.

    This is mostly a convenience for manual setup / tests. AIs should write
    their own manifests using their own file-write tools.
    """
    cfg = load_config()
    machine = args.machine or cfg.get("machine") or default_machine_name()
    tool = args.tool
    cwd = args.cwd or os.getcwd()
    project = args.project or project_from_cwd(cwd)
    ws_id = f"{slug(machine)}-{slug(tool)}-{slug(project)}"
    mf_path = manifest_path_for(ws_id)

    if mf_path.exists() and not args.force:
        print(f"[=] manifest already exists: {mf_path}")
        return 0

    tracked_dirs = []
    # Auto-seed for Claude Code: its auto-memory dir for this cwd
    if tool == "claude-code" and cwd:
        encoded = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
        for variant in {encoded, encoded[:1].lower() + encoded[1:] if encoded else encoded}:
            candidate = Path.home() / ".claude" / "projects" / variant / "memory"
            if candidate.is_dir():
                tracked_dirs.append({
                    "local": str(candidate).replace("\\", "/"),
                    "upload_prefix": "auto/",
                    "glob": "*.md",
                })
                break

    manifest = {
        "workspace_id": ws_id,
        "machine": machine,
        "tool": tool,
        "project": project,
        "tracked": [],
        "tracked_dirs": tracked_dirs,
    }

    MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    mf_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                       encoding="utf-8")
    print(f"[+] wrote manifest: {mf_path}")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


def _reverse_claude_slug(slug_dir: str) -> str:
    """Best-effort project name from a Claude project dir name.

    Claude encodes cwd as re.sub(r'[^a-zA-Z0-9]', '-', cwd). The reverse
    is ambiguous because '.' and '_' both become '-', and Unicode chars
    like CJK characters collapse to empty runs producing a trailing '---'.

    Strategy:
      1. Split on '-' and drop empty parts.
      2. If the string ends with empty runs (i.e. the original cwd ended
         in non-ASCII), we cannot recover the leaf name. Return a
         deterministic hash-based id so different non-ASCII projects do
         not collide.
      3. Otherwise, find the last segment that starts with a letter
         (the real leaf word), then keep it plus any trailing purely-
         numeric segments (so 'mindcraft-0-1-3' stays whole).

    Examples:
      'c--Users-pm-Desktop-Repvit'          -> 'repvit'
      'c--Users-pm-Desktop-mindcraft-0-1-3' -> 'mindcraft-0-1-3'
      '-home-pm-TPU'                        -> 'tpu'
      'c--Users-you-Desktop---'             -> 'proj-<hash8>'  (non-ASCII case)
    """
    # Detect trailing empty-runs (original cwd ended in non-ASCII)
    if slug_dir.endswith("--") or slug_dir.endswith("-"):
        # Only treat as non-ASCII leaf if there are real trailing dashes
        # beyond what a regular filename would produce. A simple rule:
        # three or more trailing dashes = definitely non-ASCII collapse.
        trailing = len(slug_dir) - len(slug_dir.rstrip("-"))
        if trailing >= 3:
            return "proj-" + hashlib.sha1(slug_dir.encode()).hexdigest()[:8]

    parts = [p for p in slug_dir.split("-") if p]
    if not parts:
        return "default"

    # Find last segment whose first char is alphabetic (the leaf word).
    i = len(parts) - 1
    while i >= 0 and not parts[i][:1].isalpha():
        i -= 1
    if i < 0:
        # All numeric: unlikely but safe fallback
        return slug("-".join(parts)) or "default"

    # Keep parts[i] plus any purely-digit segments after it.
    tail = [parts[i]]
    j = i + 1
    while j < len(parts) and parts[j].isdigit():
        tail.append(parts[j])
        j += 1
    return slug("-".join(tail)) or "default"


def _existing_manifest_tracking(local_path: str) -> str | None:
    """Return workspace_id of any existing manifest whose tracked_dirs or
    tracked already covers local_path. Used by scan-claude-projects to
    avoid creating a duplicate manifest when the user has already
    manually set one up (e.g. mymachine-claude-code-myproject already tracks
    the project's memory dir).
    """
    if not MANIFESTS_DIR.exists():
        return None
    target = local_path.replace("\\", "/").rstrip("/")
    for f in MANIFESTS_DIR.glob("*.json"):
        try:
            m = load_jsonc(f)
        except Exception:
            continue
        for d in (m.get("tracked_dirs") or []):
            if (d.get("local") or "").replace("\\", "/").rstrip("/") == target:
                return m.get("workspace_id") or f.stem
        for t in (m.get("tracked") or []):
            if (t.get("local") or "").replace("\\", "/").rstrip("/") == target:
                return m.get("workspace_id") or f.stem
    return None


def cmd_scan_claude_projects(args) -> int:
    """Walk ~/.claude/projects/ and auto-create manifests for each project
    that has a non-empty memory/ subdirectory.

    Each manifest is placed at
    ~/.memory-sync/manifests/<machine>-claude-code-<project>.json
    with a single tracked_dirs entry pointing at
    ~/.claude/projects/<slug>/memory.

    Skips:
      - Empty memory/ dirs (nothing to sync)
      - Dirs whose path is already tracked by any existing manifest
        (so a hand-maintained workspace like 'myproject' is never
        shadowed by an auto-generated 'proj-<hash>')
      - Existing manifests at the same ws_id unless --force
    """
    cfg = load_config()
    machine = args.machine or cfg.get("machine") or default_machine_name()
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.is_dir():
        print(f"[-] {projects_root} does not exist, nothing to scan")
        return 0

    created = 0
    skipped = 0
    empty = 0
    already_tracked = 0
    collisions: dict[str, list[str]] = {}

    for proj_dir in sorted(projects_root.iterdir()):
        if not proj_dir.is_dir():
            continue
        mem = proj_dir / "memory"
        if not mem.is_dir():
            continue
        mds = [p for p in mem.glob("*.md") if p.is_file()]
        if not mds:
            empty += 1
            continue

        # If some other manifest already tracks this memory dir, skip —
        # the user (or another scanner pass) owns it.
        mem_str = str(mem).replace("\\", "/")
        owner = _existing_manifest_tracking(mem_str)
        if owner and not args.force:
            print(f"[=] already tracked by {owner}: {proj_dir.name}")
            already_tracked += 1
            continue

        project = _reverse_claude_slug(proj_dir.name)
        ws_id = f"{slug(machine)}-claude-code-{project}"
        mf_path = manifest_path_for(ws_id)

        collisions.setdefault(ws_id, []).append(proj_dir.name)

        existing_dirs: list[dict] = []
        if mf_path.exists():
            if not args.force and len(collisions[ws_id]) == 1:
                print(f"[=] exists: {mf_path.name}  ({proj_dir.name}, {len(mds)} md)")
                skipped += 1
                continue
            try:
                existing = load_jsonc(mf_path)
                existing_dirs = list(existing.get("tracked_dirs") or [])
            except Exception:
                existing_dirs = []

        tracked_dir_entry = {
            "local": mem_str,
            "upload_prefix": "auto/",
            "glob": "*.md",
        }
        merged = [d for d in existing_dirs
                  if (d.get("local") or "").replace("\\", "/") != tracked_dir_entry["local"]]
        merged.append(tracked_dir_entry)

        manifest = {
            "workspace_id": ws_id,
            "machine": machine,
            "tool": "claude-code",
            "project": project,
            "tracked": [],
            "tracked_dirs": merged,
        }
        MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
        mf_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                           encoding="utf-8")
        print(f"[+] wrote: {mf_path.name}  <-  {proj_dir.name}  ({len(mds)} md)")
        created += 1

    collisions_real = {k: v for k, v in collisions.items() if len(v) > 1}
    if collisions_real:
        print()
        print("[!] multiple Claude project dirs collapsed into the same workspace:")
        for ws_id, dirs in collisions_real.items():
            print(f"    {ws_id}:")
            for d in dirs:
                print(f"      - {d}")
        print("    They are merged into one workspace via multiple tracked_dirs.")

    print()
    print(f"summary: created={created} skipped={skipped} empty_memory={empty}")
    if created:
        print()
        print("Next step: run sync to upload:")
        print("  python ~/.memory-sync/bin/memory_sync.py sync")
    return 0


def cmd_workspace_status(args) -> int:
    """Local summary of a workspace's manifest state."""
    cfg = load_config()
    machine = args.machine or cfg.get("machine") or default_machine_name()
    tool = args.tool
    project = args.project or project_from_cwd(args.cwd or os.getcwd())
    ws_id = f"{slug(machine)}-{slug(tool)}-{slug(project)}"
    mf_path = manifest_path_for(ws_id)
    state_path = _state_path(ws_id)

    print(f"workspace_id:  {ws_id}")
    print(f"manifest:      {mf_path}  {'(exists)' if mf_path.exists() else '(MISSING)'}")
    print(f"state cache:   {state_path}  {'(exists)' if state_path.exists() else '(none)'}")
    if mf_path.exists():
        try:
            m = load_jsonc(mf_path)
            entries = expand_manifest_entries(m)
            print(f"entries:       {len(entries)}")
            for up, loc in entries[:20]:
                flag = "ok " if loc.exists() else "!! "
                print(f"  {flag}{up}  <-  {loc}")
            if len(entries) > 20:
                print(f"  … and {len(entries) - 20} more")
        except Exception as e:
            print(f"[-] manifest parse error: {e}")
    return 0


# ── register ────────────────────────────────────────────────────────────

def cmd_register(args) -> int:
    cfg = load_config()
    client = MCPClient(get_url(cfg), get_token(cfg))
    client.initialize()
    ws_id = ensure_workspace(client, cfg,
                             args.machine or cfg["machine"],
                             args.tool,
                             args.project,
                             cwd_hint=args.cwd or os.getcwd())
    print(ws_id)
    return 0


# ── list ────────────────────────────────────────────────────────────────

def cmd_list(args) -> int:
    print(f"config:      {CONFIG_PATH}")
    print(f"uploaded db: {UPLOADED_DB}")
    print(f"workspaces:  {WS_DB}")
    print(f"log:         {LOG_PATH}")
    print()
    if UPLOADED_DB.exists():
        lines = UPLOADED_DB.read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"=== uploaded ({len(lines)}) ===")
        for line in lines[-20:]:
            parts = line.split("\t")
            if len(parts) >= 4:
                print(f"  {parts[0][:12]}  {parts[1]}  {parts[2][:60]}  {parts[3]}")
    if WS_DB.exists():
        print()
        print("=== registered workspaces ===")
        for line in WS_DB.read_text(encoding="utf-8", errors="replace").splitlines():
            print(f"  {line}")
    return 0


# ── test ────────────────────────────────────────────────────────────────

def cmd_test(args) -> int:
    cfg = load_config()
    print(f"[*] url:     {get_url(cfg)}")
    env_var = cfg.get("token_env", DEFAULT_TOKEN_ENV)
    if os.environ.get(env_var):
        tok_src = f"<env {env_var}>"
    elif (HOME / "token").exists():
        tok_src = f"<file {HOME / 'token'}>"
    else:
        tok_src = "<MISSING>"
    print(f"[*] token:   {tok_src}")
    print(f"[*] machine: {cfg.get('machine')}")
    client = MCPClient(get_url(cfg), get_token(cfg))
    try:
        client.initialize()
        r = client.call_tool("get_overview", {})
        text = MCPClient.tool_text(r)
        print("[+] connected, overview preview:")
        print("    " + "\n    ".join(text.splitlines()[:10]))
        return 0
    except Exception as e:
        print(f"[-] test failed: {e}")
        return 1


# ── main ────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(prog="memory-sync")
    sub = p.add_subparsers(dest="cmd", required=True)

    ch = sub.add_parser("claude-hook", help="Read SessionEnd hook JSON from stdin")
    ch.add_argument("--machine")
    ch.add_argument("--project")
    ch.set_defaults(func=cmd_claude_hook)

    cx = sub.add_parser("codex-scan", help="Scan ~/.codex/sessions")
    cx.add_argument("--machine")
    cx.add_argument("--codex-home", help="Override ~/.codex location")
    cx.set_defaults(func=cmd_codex_scan)

    cs = sub.add_parser("claude-scan",
                        help="Scan ~/.claude/projects/**/*.jsonl for historical sessions")
    cs.add_argument("--machine")
    cs.add_argument("--claude-home", help="Override ~/.claude location")
    cs.set_defaults(func=cmd_claude_scan)

    sy = sub.add_parser("sync",
                        help="Unified periodic sync: manifest knowledge + Codex sessions")
    sy.set_defaults(func=cmd_sync)

    im = sub.add_parser("init-manifest",
                        help="Create a starter manifest for a workspace")
    im.add_argument("--cwd", help="Workspace cwd (default: current directory)")
    im.add_argument("--machine")
    im.add_argument("--tool", default="claude-code")
    im.add_argument("--project")
    im.add_argument("--force", action="store_true",
                    help="Overwrite existing manifest")
    im.set_defaults(func=cmd_init_manifest)

    sc = sub.add_parser("scan-claude-projects",
                        help="Walk ~/.claude/projects and create a manifest "
                             "for every project with a non-empty memory/")
    sc.add_argument("--machine",
                    help="Machine label (default: from config.json)")
    sc.add_argument("--force", action="store_true",
                    help="Overwrite existing manifests")
    sc.set_defaults(func=cmd_scan_claude_projects)

    ws = sub.add_parser("workspace-status",
                        help="Show local manifest + state for a workspace")
    ws.add_argument("--cwd")
    ws.add_argument("--machine")
    ws.add_argument("--tool", default="claude-code")
    ws.add_argument("--project")
    ws.set_defaults(func=cmd_workspace_status)

    rg = sub.add_parser("register", help="Manually register a workspace")
    rg.add_argument("--machine")
    rg.add_argument("--tool", required=True)
    rg.add_argument("--project", required=True)
    rg.add_argument("--cwd")
    rg.set_defaults(func=cmd_register)

    ls = sub.add_parser("list", help="Show local state")
    ls.set_defaults(func=cmd_list)

    ts = sub.add_parser("test", help="Smoke test connection")
    ts.set_defaults(func=cmd_test)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
