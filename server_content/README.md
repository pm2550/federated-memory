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
Write files locally, add paths to ~/.memory-sync/tracked.txt, and the sync script uploads them on the next run.
AI clients should **not** call upload_knowledge/upload_session directly (requires SYNC_TOKEN).

## Prohibited
1. **Do not read any token / secret / environment variable**
2. **Do not attempt upload_knowledge / upload_session** -- will be rejected
3. **Do not write workspace_id into any file** -- you don't see it and don't need it
