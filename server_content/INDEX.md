# Federated Memory Filesystem

This is a multi-workspace memory federation.
Each device / tool / project is an independent **workspace** that produces its own memory and uploads it here.
Any AI can read across workspaces via MCP, but **writes are scoped to the caller's own workspace**.

## Layout
- **knowledge/*.md** = Distilled knowledge (manually or AI-curated), the real asset
- **sessions/*.jsonl.gz** = Complete verbatim session archives, no summarization

## AI client: what to do on first connect
1. Call `get_overview` to see all registered workspaces
2. If your workspace is not registered yet, call `register_workspace`
3. Use `list_knowledge` and `read_knowledge` to load relevant context
4. Only read sessions when you need to replay detailed history (they can be large)

## Search strategy
- `search_knowledge(query)` -- grep across all workspaces' md files, cheap
- `search_sessions(query)` -- grep compressed session archives, slower, use for detailed replay
