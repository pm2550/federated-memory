#!/usr/bin/env python3
"""Seed _about.md for this workspace. Demo of the convention.

Usage:
  Set MCP_MEMORY_TOKEN and optionally SYNC_TOKEN in your environment,
  then edit WORKSPACE_ID and ABOUT below and run:

    python scripts/seed_about.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "client"))
from memory_sync import MCPClient, get_token, load_config  # noqa: E402

# ── EDIT THESE ──────────────────────────────────────────────────────────
WORKSPACE_ID = "mymachine-claude-code-myproject"

ABOUT = r"""# mymachine-claude-code-myproject

## What is this
Example workspace description. Edit this to describe your workspace.

## Key learnings
- Architecture: each workspace produces its own memory, the server just stores
- Sessions are archived verbatim (no summarization); knowledge md is distilled
- Write permissions are partitioned by workspace_id; reads have no boundary

## Status
- Federated structure + MCP tools: DONE
- Client sync (memory_sync.py + SessionEnd hook): DONE
"""


def main() -> None:
    cfg = load_config()
    c = MCPClient(cfg["url"], get_token(cfg))
    c.initialize()
    r = c.call_tool("upload_knowledge", {
        "workspace_id": WORKSPACE_ID,
        "path": "_about.md",
        "content": ABOUT,
    })
    print("upload:", MCPClient.tool_text(r))

    r = c.call_tool("get_overview", {})
    text = MCPClient.tool_text(r)
    in_ws = False
    for line in text.splitlines():
        if line.startswith(f"## {WORKSPACE_ID}"):
            in_ws = True
        elif line.startswith("## "):
            in_ws = False
        if in_ws:
            print(line)


if __name__ == "__main__":
    main()
