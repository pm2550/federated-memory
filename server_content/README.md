# AI Client Usage Guide (v3)

## Purpose
Let any AI tool (Claude / Codex / Cursor / ...) on any machine share cross-workspace memory.

## Security model
- **AI clients**: read + write_overview (own) + delete_knowledge (own)
- **Uploads are done by the sync script**, AI clients do not upload directly
- **workspace_id is invisible to AI**; they only see machine/repo_name labels

## AI: what to do in a new session

### 1. Register and bind
```
register_workspace(machine="mymachine", repo_name="myproject")
```
The server binds your session; subsequent write_overview/delete_knowledge calls route automatically.

### 2. Write an overview (if none exists)
```
write_overview(content="# Project description\n...")
```
This is the "business card" other AIs see in get_overview.

### 3. Read other workspaces' knowledge
```
read_knowledge(workspace="othermachine/otherproject", path="auto/MEMORY.md")
search_knowledge(query="some topic")
```
Reads have no boundary -- use machine/repo_name to reference any workspace.

### 4. Sync local files to the server

Two complementary mechanisms — **both run during every sync, merged and deduped:**

#### Layer 1: manifest (auto-scan directories; existing + new files tracked automatically)
`~/.memory-sync/manifests/<workspace_id>.json` has a `tracked_dirs` list that defines directories to auto-scan:
```jsonc
{
  "tracked_dirs": [
    {
      "local": "~/.claude/projects/<slug>/memory",
      "upload_prefix": "auto/",
      "glob": "*.md"
    }
  ]
}
```
Any new `.md` file dropped into that directory is uploaded on the next sync — no manual step. Manifests are created by `install.sh` or `scan-claude-projects`; AI clients usually should not touch them.

#### Layer 2: tracked.txt (explicit single-file paths that manifest can't reach)
`~/.memory-sync/tracked.txt` — one local file path per line:
```
# files outside any manifest-tracked directory
~/Desktop/someproject/docs/decisions.md
~/Desktop/otherproject/SPEC.md
~/.antigravity/memory/notes.md
```
AI clients can add lines here using their local Write tool. `#` starts a comment; `~` is expanded.

#### Relationship
| | manifest | tracked.txt |
|---|---|---|
| Scope | whole directory (glob) | single file |
| New files | auto-discovered | needs a new line |
| Best for | known memory dirs (Claude / Codex) | scattered files elsewhere |
| Who creates | install.sh / scan-claude-projects | AI or user |

**The sync script runs manifest first, then tracked.txt, merging and deduping uploads. They coexist; not mutually exclusive.**

AI clients should **not** call upload_knowledge / upload_session directly (requires SYNC_TOKEN).

## Prohibited
1. **Do not read any token / secret / environment variable**
2. **Do not attempt upload_knowledge / upload_session** -- will be rejected
3. **Do not write workspace_id into any file** -- you don't see it and don't need it
