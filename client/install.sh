#!/usr/bin/env bash
# One-line installer for memory-server client config.
# Usage: bash install.sh <TOKEN> [--machine NAME] [--interval MIN]
#
# Supported hosts: Windows (Git Bash/MSYS), WSL (Ubuntu et al), macOS, Linux.
#
# What it does:
#   1. Persists MCP_MEMORY_TOKEN to the OS-appropriate place
#      (Windows user env via PowerShell, or ~/.bashrc / ~/.zshrc on Unix).
#   2. Adds the memory MCP server to Claude Code / Codex CLI configs (if found).
#   3. Installs memory_sync.py under ~/.memory-sync/bin/.
#   4. Hooks Claude Code SessionEnd → memory_sync.py claude-hook
#      (so every Claude session end triggers an upload + manifest resync).
#   5. Schedules `memory_sync.py sync` every N minutes (Windows schtasks / cron).
#   6. Smoke-tests the connection and prints next-steps.
set -euo pipefail

URL="${MCP_URL:-http://YOUR_SERVER_IP:7444/mcp}"
TOKEN=""
SYNC_TOKEN_ARG=""
MACHINE=""
INTERVAL="60"   # minutes; 0 disables the scheduled sync
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_SYNC="$SCRIPT_DIR/memory_sync.py"
HOOK_DIR="$HOME/.memory-sync"
SYNC_PY="$HOOK_DIR/bin/memory_sync.py"

# Parse args: first positional is AI token, rest are flags.
while [ $# -gt 0 ]; do
    case "$1" in
        --machine)      MACHINE="$2"; shift 2 ;;
        --interval)     INTERVAL="$2"; shift 2 ;;
        --url)          URL="$2"; shift 2 ;;
        --sync-token)   SYNC_TOKEN_ARG="$2"; shift 2 ;;
        --help|-h)
            cat <<EOF
Usage: bash install.sh <AI_TOKEN> --sync-token <SYNC_TOKEN> [OPTIONS]

Arguments:
  AI_TOKEN           Bearer token for AI clients (read + write_overview + delete own)
  --sync-token TOK   Bearer token for sync script (upload knowledge + sessions).
                     REQUIRED for the sync daemon to work.

Options:
  --machine NAME     Machine label (default: hostname slug).
  --interval MIN     Sync interval in minutes (default: 60, 0=off).
  --url URL          Override memory server URL.

You can also set MCP_MEMORY_TOKEN and SYNC_TOKEN in the environment.
EOF
            exit 0 ;;
        *)
            if [ -z "${TOKEN:-}" ]; then TOKEN="$1"; shift;
            else echo "[-] unexpected arg: $1" >&2; exit 1; fi ;;
    esac
done
TOKEN="${TOKEN:-${MCP_TOKEN:-${MCP_MEMORY_TOKEN:-}}}"
SYNC_TOKEN_ARG="${SYNC_TOKEN_ARG:-${SYNC_TOKEN:-}}"

if [ -z "$TOKEN" ]; then
    echo "Usage: bash install.sh <AI_TOKEN> --sync-token <SYNC_TOKEN> [--machine NAME]" >&2
    exit 1
fi
if [ -z "$SYNC_TOKEN_ARG" ]; then
    echo "[!] WARNING: --sync-token not provided. Sync daemon will not be able to upload."
    echo "    Pass --sync-token <TOKEN> to enable uploads."
    echo "    Continuing with read-only setup..."
fi

echo "[*] Memory Server: $URL"
echo "[*] Interval:      ${INTERVAL} min  (0 = disabled)"

# ── Detect OS ────────────────────────────────────────────────────────────
OS="$(uname -s)"
case "$OS" in
    Darwin*)            PLATFORM=mac ;;
    Linux*)
        if [ -r /proc/version ] && grep -qiE "microsoft|wsl" /proc/version; then
            PLATFORM=wsl
        else
            PLATFORM=linux
        fi
        ;;
    MINGW*|MSYS*|CYGWIN*) PLATFORM=windows ;;
    *)                  PLATFORM=unknown ;;
esac
echo "[*] Platform: $PLATFORM"

# Default machine name: hostname slug, with wsl distro suffix if under WSL.
if [ -z "${MACHINE:-}" ]; then
    HOST="$(hostname 2>/dev/null || uname -n 2>/dev/null || echo host)"
    MACHINE="$(echo "$HOST" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/^-*//;s/-*$//')"
    if [ "$PLATFORM" = "wsl" ]; then
        DISTRO="$(. /etc/os-release 2>/dev/null && echo "${ID:-}" || echo "wsl")"
        MACHINE="${MACHINE}-wsl-${DISTRO}"
    fi
fi
echo "[*] Machine:  $MACHINE"

# ── Helpers ──────────────────────────────────────────────────────────────
has() { command -v "$1" >/dev/null 2>&1; }

# Convert Windows path to MSYS/Cygwin style if needed
winpath() {
    local p="$1"
    case "$PLATFORM" in
        windows)
            # C:\foo\bar → /c/foo/bar
            echo "$p" | sed -e 's|\\|/|g' -e 's|^\([A-Za-z]\):|/\L\1|'
            ;;
        *)  echo "$p" ;;
    esac
}

# Pick the newest match from a list of candidate paths.
# Expands globs, filters to existing files, sorts by mtime desc.
newest_match() {
    local best= best_mtime=0
    for p in "$@"; do
        [ -e "$p" ] || continue
        local m
        m=$(stat -c %Y "$p" 2>/dev/null || stat -f %m "$p" 2>/dev/null || echo 0)
        if [ "$m" -gt "$best_mtime" ]; then
            best="$p"; best_mtime="$m"
        fi
    done
    [ -n "$best" ] && echo "$best"
}

find_claude() {
    # 1. On PATH
    if has claude; then command -v claude; return; fi
    if has claude.exe; then command -v claude.exe; return; fi

    local candidates=()

    # 2. Windows native install: %APPDATA%\Claude\claude-code\<ver>\claude.exe
    local roots=()
    [ -n "${APPDATA:-}" ] && roots+=("$(winpath "$APPDATA")")
    roots+=("$HOME/AppData/Roaming")
    [ -n "${USERNAME:-}" ] && roots+=("/c/Users/$USERNAME/AppData/Roaming")
    [ -n "${USER:-}" ]     && roots+=("/c/Users/$USER/AppData/Roaming")
    for r in "${roots[@]}"; do
        for f in "$r"/Claude/claude-code/*/claude.exe "$r"/npm/claude.cmd; do
            [ -f "$f" ] && candidates+=("$f")
        done
    done

    # 3. VSCode extension (Linux + WSL + Mac + Windows): Claude Code is
    #    shipped as a VSCode extension that bundles a native binary at
    #    resources/native-binary/claude[.exe].
    local vscode_exts=(
        "$HOME/.vscode-server/extensions"        # VSCode Remote / WSL
        "$HOME/.vscode/extensions"
        "$HOME/.cursor-server/extensions"
        "$HOME/.cursor/extensions"
        "$HOME/.vscode-insiders/extensions"
    )
    for root in "${vscode_exts[@]}"; do
        [ -d "$root" ] || continue
        for f in "$root"/anthropic.claude-code-*/resources/native-binary/claude \
                 "$root"/anthropic.claude-code-*/resources/native-binary/claude.exe \
                 "$root"/anthropic.claude-code-*/bin/claude \
                 "$root"/anthropic.claude-code-*/bin/claude.exe \
                 "$root"/anthropic.claude-code-*/bin/*/claude \
                 "$root"/anthropic.claude-code-*/bin/*/claude.exe; do
            [ -f "$f" ] && candidates+=("$f")
        done
    done

    # 4. Unix standard locations
    for p in /usr/local/bin/claude /opt/homebrew/bin/claude "$HOME/.local/bin/claude" \
             "$HOME/.npm-global/bin/claude" /usr/bin/claude; do
        [ -f "$p" ] && candidates+=("$p")
    done

    newest_match "${candidates[@]}"
}

find_codex() {
    if has codex; then command -v codex; return; fi
    if has codex.exe; then command -v codex.exe; return; fi

    local candidates=()
    # Per-user Codex layout (Windows + Linux + Mac share ~/.codex/)
    for p in "$HOME"/.codex/.sandbox-bin/codex.exe \
             "$HOME"/.codex/.sandbox-bin/codex \
             "$HOME"/.codex/bin/codex \
             "$HOME"/.codex/bin/codex.exe; do
        [ -f "$p" ] && candidates+=("$p")
    done

    # VSCode / Cursor / vscode-server extensions.
    # The binary may be shipped by the OpenAI ChatGPT extension
    # (openai.chatgpt-*) or any future extension with 'codex' in the name.
    # Actual layouts seen in the wild:
    #   openai.chatgpt-<ver>-<os>/bin/<arch>/codex       (e.g. bin/linux-x86_64/codex)
    #   openai.chatgpt-<ver>-<os>/bin/<arch>/codex.exe
    #   *codex*/resources/native-binary/codex
    #   *codex*/codex
    local ext_roots=(
        "$HOME/.vscode/extensions"
        "$HOME/.vscode-server/extensions"
        "$HOME/.cursor/extensions"
        "$HOME/.cursor-server/extensions"
        "$HOME/.vscode-insiders/extensions"
    )
    [ -n "${USERPROFILE:-}" ] && ext_roots+=("$(winpath "$USERPROFILE")/.vscode/extensions")

    # Detect current arch for filtering cross-compiled binaries.
    local arch
    case "$(uname -m)" in
        x86_64|amd64) arch="x86_64|x64|amd64" ;;
        aarch64|arm64) arch="arm64|aarch64" ;;
        *) arch="$(uname -m)" ;;
    esac
    # On Windows/MSYS, also accept win32-* platform dirs.
    local plat_re=""
    case "$PLATFORM" in
        windows) plat_re="win32" ;;
        mac)     plat_re="darwin" ;;
        linux|wsl) plat_re="linux" ;;
    esac

    for r in "${ext_roots[@]}"; do
        [ -d "$r" ] || continue
        for pat in "$r"/openai.chatgpt-* "$r"/*codex*; do
            for ext in "$pat"; do
                [ -d "$ext" ] || continue
                for f in "$ext"/codex \
                         "$ext"/codex.exe \
                         "$ext"/bin/codex \
                         "$ext"/bin/codex.exe \
                         "$ext"/bin/*/codex \
                         "$ext"/bin/*/codex.exe \
                         "$ext"/out/bin/codex \
                         "$ext"/out/bin/codex.exe \
                         "$ext"/resources/native-binary/codex \
                         "$ext"/resources/native-binary/codex.exe; do
                    [ -f "$f" ] || continue
                    # Filter out cross-platform binaries. If the path has a
                    # platform/arch segment (e.g. bin/linux-x86_64/codex), reject
                    # if it doesn't match the current platform.
                    local fpath="$f"
                    if [ -n "$plat_re" ]; then
                        # If path contains a platform dir that is NOT ours, skip.
                        local parent_dir
                        parent_dir="$(basename "$(dirname "$fpath")")"
                        case "$parent_dir" in
                            linux-*|darwin-*|win32-*)
                                if ! echo "$parent_dir" | grep -qiE "$plat_re"; then
                                    continue   # wrong platform
                                fi
                                ;;
                        esac
                    fi
                    candidates+=("$f")
                done
            done
        done
    done

    # Common Unix install locations
    for p in /usr/local/bin/codex /opt/homebrew/bin/codex \
             "$HOME/.local/bin/codex" "$HOME/.npm-global/bin/codex"; do
        [ -f "$p" ] && candidates+=("$p")
    done

    newest_match "${candidates[@]}"
}

# ── Persist env var for Codex ────────────────────────────────────────────
persist_env() {
    case "$PLATFORM" in
        windows)
            powershell.exe -NoProfile -Command \
                "[Environment]::SetEnvironmentVariable('MCP_MEMORY_TOKEN','$TOKEN','User')" 2>/dev/null \
                && echo "[+] Windows user env: MCP_MEMORY_TOKEN set"
            if [ -n "$SYNC_TOKEN_ARG" ]; then
                powershell.exe -NoProfile -Command \
                    "[Environment]::SetEnvironmentVariable('SYNC_TOKEN','$SYNC_TOKEN_ARG','User')" 2>/dev/null \
                    && echo "[+] Windows user env: SYNC_TOKEN set"
            fi
            ;;
        mac|linux|wsl)
            for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile"; do
                [ -f "$rc" ] || continue
                if ! grep -q "MCP_MEMORY_TOKEN" "$rc"; then
                    echo "" >> "$rc"
                    echo "# MCP memory server tokens" >> "$rc"
                    echo "export MCP_MEMORY_TOKEN='$TOKEN'" >> "$rc"
                    echo "[+] Added MCP_MEMORY_TOKEN to $rc"
                fi
                if [ -n "$SYNC_TOKEN_ARG" ] && ! grep -q "SYNC_TOKEN" "$rc"; then
                    echo "export SYNC_TOKEN='$SYNC_TOKEN_ARG'" >> "$rc"
                    echo "[+] Added SYNC_TOKEN to $rc"
                fi
            done
            ;;
    esac
}

# ── Configure Claude CLI ─────────────────────────────────────────────────
configure_claude() {
    local claude_bin
    claude_bin="$(find_claude || true)"
    if [ -z "$claude_bin" ]; then
        echo "[-] Claude CLI not found, skipping"
        return
    fi
    echo "[*] Claude CLI: $claude_bin"
    "$claude_bin" mcp remove memory -s user 2>/dev/null || true
    "$claude_bin" mcp add \
        --transport http memory "$URL" \
        --header "Authorization: Bearer $TOKEN" \
        -s user >/dev/null
    echo "[+] Claude: memory MCP server added"

    # Health check
    if "$claude_bin" mcp get memory 2>/dev/null | grep -q "Connected"; then
        echo "[✓] Claude: memory Connected"
    else
        echo "[!] Claude: connect check failed (may be transient)"
    fi
}

# ── Configure Codex CLI ──────────────────────────────────────────────────
configure_codex() {
    local codex_bin
    codex_bin="$(find_codex || true)"
    if [ -z "$codex_bin" ]; then
        echo "[-] Codex CLI not found, skipping"
        return
    fi
    echo "[*] Codex CLI: $codex_bin"
    "$codex_bin" mcp remove memory 2>/dev/null || true
    "$codex_bin" mcp add memory \
        --url "$URL" \
        --bearer-token-env-var MCP_MEMORY_TOKEN >/dev/null
    echo "[+] Codex: memory MCP server added"
    echo "[!] Codex: restart your terminal / VSCode to pick up MCP_MEMORY_TOKEN"
}

# ── Install memory_sync.py ───────────────────────────────────────────────
install_sync_script() {
    mkdir -p "$HOOK_DIR/bin"
    if [ -f "$SRC_SYNC" ]; then
        cp "$SRC_SYNC" "$SYNC_PY"
        chmod +x "$SYNC_PY"
        echo "[+] Installed: $SYNC_PY"
    else
        echo "[-] memory_sync.py not found at $SRC_SYNC" >&2
        exit 1
    fi
}

# ── Write ~/.memory-sync/config.json with machine + interval ─────────────
write_config() {
    local py
    py="$(find_python)"
    [ -n "$py" ] || { echo "[-] no python; config.json not written"; return; }
    mkdir -p "$HOOK_DIR"
    CFG_PATH="$HOOK_DIR/config.json" \
    CFG_URL="$URL" \
    CFG_MACHINE="$MACHINE" \
    CFG_INTERVAL="$INTERVAL" \
    "$py" - <<'PYEOF'
import json, os
path = os.environ["CFG_PATH"]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    data = {}
data["url"] = os.environ["CFG_URL"]
data["token_env"] = "MCP_MEMORY_TOKEN"
data["machine"] = os.environ["CFG_MACHINE"]
interval = int(os.environ["CFG_INTERVAL"])
data["manifest_interval_minutes"] = interval
data["codex_scan_interval_minutes"] = interval
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
print(f"[+] Wrote config: {path}  machine={data['machine']} interval={interval}")
PYEOF
}

# ── Find a Python interpreter ────────────────────────────────────────────
# Tries candidates and filters out the Microsoft Store Python stub (on
# Windows, a bare `python` on PATH may be a redirector to the store).
find_python() {
    # Common conda/anaconda install paths on Windows.
    local win_hints=(
        "$HOME/anaconda3/python.exe"
        "$HOME/miniconda3/python.exe"
        "$HOME/AppData/Local/Programs/Python/Python312/python.exe"
        "$HOME/AppData/Local/Programs/Python/Python311/python.exe"
        "$HOME/AppData/Local/Programs/Python/Python310/python.exe"
        "/c/Python312/python.exe" "/c/Python311/python.exe" "/c/Python310/python.exe"
    )
    for p in "${win_hints[@]}"; do
        [ -x "$p" ] && { echo "$p"; return; }
    done

    # Env-var pointing candidates (respected over PATH).
    [ -n "${PYTHON:-}" ] && [ -x "$PYTHON" ] && { echo "$PYTHON"; return; }

    # Candidates on PATH, checking that they actually run (Windows Store
    # shim exits non-zero for `python -V` without any install).
    for p in python3 python py; do
        if has "$p"; then
            if "$p" -V >/dev/null 2>&1; then
                command -v "$p"
                return
            fi
        fi
    done
}

# ── Install Claude SessionEnd hook ───────────────────────────────────────
install_claude_hook() {
    local py
    py="$(find_python)"
    if [ -z "$py" ]; then
        echo "[-] No python interpreter found, skipping Claude hook" >&2
        return
    fi

    # IMPORTANT: Claude spawns the hook command with an inherited env, but
    # when VSCode / cursor-server launches Claude, the Claude process's env
    # often does NOT include user shell exports (no ~/.bashrc sourced).
    # So we never point Claude directly at python; instead we generate a
    # tiny wrapper that loads config.json, exports the token, and execs
    # memory_sync.py.
    #
    # config.json stores the machine/url, but NOT the token (by design).
    # The wrapper reads the token from the user env: if it isn't there,
    # it falls back to a 0600 token file we write from install.sh.

    local hook_wrapper="$HOOK_DIR/bin/hook-wrapper.sh"
    local token_file="$HOOK_DIR/token"

    # Write AI token to a restricted file for the hook to read when the
    # shell env is empty.
    umask 077
    printf '%s\n' "$TOKEN" > "$token_file"
    # Write SYNC token to a separate file (used by sync scripts for uploads).
    local sync_token_path="$HOOK_DIR/sync_token"
    if [ -n "$SYNC_TOKEN_ARG" ]; then
        printf '%s\n' "$SYNC_TOKEN_ARG" > "$sync_token_path"
        echo "[+] Sync token written to $sync_token_path"
    fi
    umask 022
    chmod 600 "$token_file" 2>/dev/null || true
    [ -f "$sync_token_path" ] && chmod 600 "$sync_token_path" 2>/dev/null || true
    # On Windows, chmod is a no-op; use PowerShell to restrict ACL.
    if [ "$PLATFORM" = "windows" ]; then
        powershell.exe -NoProfile -Command "
          \$p = '$(cygpath -w "$token_file" 2>/dev/null || echo "$token_file")'
          \$acl = Get-Acl \$p
          \$acl.SetAccessRuleProtection(\$true, \$false)
          \$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            [System.Security.Principal.WindowsIdentity]::GetCurrent().Name,
            'FullControl', 'Allow')
          \$acl.SetAccessRule(\$rule)
          Set-Acl \$p \$acl
        " 2>/dev/null && echo "[+] Token file ACL restricted to current user" \
                       || echo "[!] Could not restrict token file ACL (non-critical)"
    fi

    case "$PLATFORM" in
        windows)
            # Windows: use a .cmd shim that sets the env var then runs python.
            local hook_cmd_win="$HOOK_DIR/bin/hook-wrapper.cmd"
            local py_win sync_win token_win
            py_win="$(cygpath -w "$py" 2>/dev/null || echo "$py")"
            sync_win="$(cygpath -w "$SYNC_PY" 2>/dev/null || echo "$SYNC_PY")"
            token_win="$(cygpath -w "$token_file" 2>/dev/null || echo "$token_file")"
            local hook_cmd_win_w
            hook_cmd_win_w="$(cygpath -w "$hook_cmd_win" 2>/dev/null || echo "$hook_cmd_win")"
            local sync_token_win
            sync_token_win="$(cygpath -w "$HOOK_DIR/sync_token" 2>/dev/null || echo "$HOOK_DIR/sync_token")"
            cat > "$hook_cmd_win" <<CMDEOF
@echo off
if "%MCP_MEMORY_TOKEN%"=="" (
    for /f "usebackq delims=" %%t in ("$token_win") do set MCP_MEMORY_TOKEN=%%t
)
if "%SYNC_TOKEN%"=="" (
    if exist "$sync_token_win" (
        for /f "usebackq delims=" %%t in ("$sync_token_win") do set SYNC_TOKEN=%%t
    )
)
"$py_win" "$sync_win" claude-hook
CMDEOF
            HOOK_COMMAND="\"$hook_cmd_win_w\""
            ;;
        *)
            # Unix/WSL/Mac: bash wrapper that sources token from file if env is empty.
            local sync_token_file="$HOOK_DIR/sync_token"
            cat > "$hook_wrapper" <<WRAPEOF
#!/usr/bin/env bash
# Auto-generated by install.sh. Do not edit.
set -e
if [ -z "\${MCP_MEMORY_TOKEN:-}" ]; then
    if [ -f "$token_file" ]; then
        export MCP_MEMORY_TOKEN="\$(cat "$token_file")"
    fi
fi
if [ -z "\${SYNC_TOKEN:-}" ]; then
    if [ -f "$sync_token_file" ]; then
        export SYNC_TOKEN="\$(cat "$sync_token_file")"
    fi
fi
export HOME="${HOME}"
exec "$py" "$SYNC_PY" claude-hook
WRAPEOF
            chmod +x "$hook_wrapper"
            HOOK_COMMAND="bash $hook_wrapper"
            ;;
    esac
    echo "[+] Hook wrapper: $hook_wrapper (token file: $token_file)"

    local settings="$HOME/.claude/settings.json"
    mkdir -p "$(dirname "$settings")"
    [ -f "$settings" ] || echo "{}" > "$settings"

    # Use python to merge the hook into the existing JSON
    "$py" - "$settings" "$HOOK_COMMAND" <<'PYEOF'
import json, sys
settings_path, hook_cmd = sys.argv[1], sys.argv[2]
with open(settings_path, "r", encoding="utf-8") as f:
    data = json.load(f)
hooks = data.setdefault("hooks", {})
session_end = hooks.setdefault("SessionEnd", [])
# Remove any existing memory-sync hook entry (idempotent)
filtered = []
for entry in session_end:
    inner = entry.get("hooks", [])
    keep = [h for h in inner if "memory_sync.py" not in (h.get("command") or "")]
    if keep:
        entry["hooks"] = keep
        filtered.append(entry)
session_end = filtered
session_end.append({
    "matcher": "*",
    "hooks": [{
        "type": "command",
        "command": hook_cmd,
        "timeout": 30,
        "async": True,
    }],
})
hooks["SessionEnd"] = session_end
with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
print(f"[+] Claude SessionEnd hook installed: {hook_cmd}")
PYEOF
}

# ── Schedule sync ────────────────────────────────────────────────────────
install_sync_scheduler() {
    local py
    py="$(find_python)"
    [ -n "$py" ] || { echo "[-] No python, can't install scheduler"; return; }

    if [ "$INTERVAL" = "0" ]; then
        echo "[=] Interval=0, skipping scheduler install (run 'memory_sync.py sync' manually)"
        return
    fi

    local task_name="MemorySyncSync"

    case "$PLATFORM" in
        windows)
            local py_win sync_win
            py_win="$(cygpath -w "$py" 2>/dev/null || echo "$py")"
            sync_win="$(cygpath -w "$SYNC_PY" 2>/dev/null || echo "$SYNC_PY")"

            # Remove any prior scheduled task (including old naming).
            schtasks.exe //Delete //TN "MemorySyncCodexScan" //F >/dev/null 2>&1 || true
            schtasks.exe //Delete //TN "$task_name"          //F >/dev/null 2>&1 || true

            if schtasks.exe //Create //SC MINUTE //MO "$INTERVAL" //TN "$task_name" \
                //TR "\"$py_win\" \"$sync_win\" sync" \
                //RL LIMITED //F >/dev/null 2>&1; then
                echo "[+] Windows task scheduled: $task_name (every $INTERVAL min)"
            else
                echo "[!] schtasks failed — run manually: $py_win $sync_win sync"
            fi
            ;;
        mac|linux|wsl)
            # Write a shell wrapper that loads the env file then runs sync.
            # This avoids cron's empty PATH / missing env vars problem.
            local wrapper="$HOOK_DIR/bin/sync-wrapper.sh"
            local sync_tok_for_wrapper="${SYNC_TOKEN_ARG:-$TOKEN}"
            cat > "$wrapper" <<WRAPEOF
#!/usr/bin/env bash
# Auto-generated by install.sh. Runs memory_sync.py sync with the correct env.
export MCP_MEMORY_TOKEN='$TOKEN'
export SYNC_TOKEN='$sync_tok_for_wrapper'
export HOME='$HOME'
"$py" "$SYNC_PY" sync
WRAPEOF
            chmod +x "$wrapper"

            # Remove old lines then add fresh cron entry.
            local tmpcron
            tmpcron=$(mktemp)
            crontab -l 2>/dev/null | grep -vF "$SYNC_PY" | grep -vF "$wrapper" > "$tmpcron" || true
            echo "*/$INTERVAL * * * * $wrapper >/dev/null 2>&1" >> "$tmpcron"
            if crontab "$tmpcron" 2>/dev/null; then
                echo "[+] cron entry installed: every $INTERVAL min → $wrapper"
            else
                echo "[!] crontab install failed — on some WSL distros, start cron with: sudo service cron start"
                echo "    then re-run this installer."
            fi
            rm -f "$tmpcron"
            ;;
        *)
            echo "[!] Unknown platform '$PLATFORM'; no scheduler installed."
            ;;
    esac
}

# ── Run ──────────────────────────────────────────────────────────────────
persist_env
install_sync_script
write_config
configure_claude
configure_codex
install_claude_hook
install_sync_scheduler

echo ""
echo "[✓] Done."
echo ""
echo "Next steps:"
echo "  1. Restart your terminal / VSCode so MCP_MEMORY_TOKEN propagates."
echo "  2. Verify: $SYNC_PY test"
echo "  3. When you start a fresh AI session, ask it to call"
echo "     workspace_status(machine=$MACHINE, tool=claude-code, project=<cwd>)"
echo "     so it creates its local manifest and _about.md."
echo ""
if [ "$INTERVAL" != "0" ]; then
    echo "Scheduled sync will run every $INTERVAL min, pushing any changed"
    echo "manifest files to the server."
fi
