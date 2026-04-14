package federation

import (
	"bufio"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"

	"memory-server/internal/auth"
)

const (
	maxKnowledgeSize = 1 * 1024 * 1024   // 1 MB per md
	maxSessionSize   = 200 * 1024 * 1024 // 200 MB per session (raw, pre-gzip)
	maxOverviewSize  = 50 * 1024         // 50 KB for overview.md
	searchLimit      = 5000              // bytes of search output
)

type Service struct {
	paths    *Paths
	reg      *Registry
	bindings sync.Map // MCP sessionID (string) → workspace_id (string)
}

func NewService(memoryRoot string) (*Service, error) {
	p := NewPaths(memoryRoot)
	reg := NewRegistry(p)
	if err := reg.Ensure(); err != nil {
		return nil, err
	}
	return &Service{paths: p, reg: reg}, nil
}

// ── session binding ──────────────────────────────────────────────────────

func (s *Service) bindSession(ctx context.Context, wsID string) {
	sess := server.ClientSessionFromContext(ctx)
	if sess != nil {
		s.bindings.Store(sess.SessionID(), wsID)
	}
}

func (s *Service) boundWorkspace(ctx context.Context) string {
	sess := server.ClientSessionFromContext(ctx)
	if sess == nil {
		return ""
	}
	if v, ok := s.bindings.Load(sess.SessionID()); ok {
		return v.(string)
	}
	return ""
}

// ── resolve helpers ──────────────────────────────────────────────────────

// resolveRead resolves a workspace for read operations.
// Accepts "machine/repo_name" label, raw workspace_id, "all", or "".
// Empty = session-bound workspace. "all" = all workspaces.
func (s *Service) resolveRead(ctx context.Context, param string) (string, error) {
	if param == "" {
		ws := s.boundWorkspace(ctx)
		if ws == "" {
			return "", fmt.Errorf("no workspace specified and no session binding — call register_workspace first, or pass machine/repo_name")
		}
		return ws, nil
	}
	if param == "all" {
		return "all", nil
	}
	// Try label "machine/repo_name"
	if w, err := s.reg.FindByLabel(param); err == nil {
		return w.ID, nil
	}
	// Try raw workspace_id (for sync token backward compat)
	if _, err := s.reg.Get(param); err == nil {
		return param, nil
	}
	return "", fmt.Errorf("workspace not found: %s (use machine/repo_name format)", param)
}

// resolveWriteSync validates workspace_id + write_token for sync script writes.
func (s *Service) resolveWriteSync(wsID, writeToken string) error {
	if wsID == "" || writeToken == "" {
		return fmt.Errorf("workspace_id and write_token are required for sync writes")
	}
	return s.reg.ValidateWriteToken(wsID, writeToken)
}

// ── Register tools ──────────────────────────────────────────────────────

func (s *Service) Register(srv *server.MCPServer) {
	// === TOOLS FOR BOTH AI AND SYNC ===

	srv.AddTool(mcp.NewTool("get_overview",
		mcp.WithDescription("Entry point. Returns global README, and all workspaces with their overview.md summaries. Call this FIRST."),
	), s.getOverview)

	srv.AddTool(mcp.NewTool("list_workspaces",
		mcp.WithDescription("List all registered workspaces with display names and counts."),
	), s.listWorkspaces)

	srv.AddTool(mcp.NewTool("register_workspace",
		mcp.WithDescription("Bind this session to a workspace. Provide machine + repo_name (or machine + tool + project for backward compat). Sync token callers also get write_token in response."),
		mcp.WithString("machine", mcp.Required(), mcp.Description("Machine label (e.g. desktop1, macmini)")),
		mcp.WithString("repo_name", mcp.Description("Human-readable name (e.g. my-project). If omitted, derived from tool+project.")),
		mcp.WithString("tool", mcp.Description("Tool name (claude-code, codex-cli) — for backward compat with v2")),
		mcp.WithString("project", mcp.Description("Project name — for backward compat with v2")),
		mcp.WithString("cwd_hint", mcp.Description("Working directory (informational)")),
		mcp.WithString("owner", mcp.Description("Owner name")),
	), s.registerWorkspace)

	srv.AddTool(mcp.NewTool("list_knowledge",
		mcp.WithDescription("List knowledge md files. Default: own workspace. Specify 'workspace' as machine/repo_name for others, or 'all'."),
		mcp.WithString("workspace", mcp.Description("machine/repo_name, 'all', or empty for own")),
	), s.listKnowledge)

	srv.AddTool(mcp.NewTool("read_knowledge",
		mcp.WithDescription("Read a knowledge md file."),
		mcp.WithString("workspace", mcp.Description("machine/repo_name or empty for own")),
		mcp.WithString("path", mcp.Required(), mcp.Description("Relative path within knowledge/")),
	), s.readKnowledge)

	srv.AddTool(mcp.NewTool("search_knowledge",
		mcp.WithDescription("Full-text search across knowledge md files."),
		mcp.WithString("query", mcp.Required()),
		mcp.WithString("workspace", mcp.Description("machine/repo_name, 'all', or empty for own")),
	), s.searchKnowledge)

	srv.AddTool(mcp.NewTool("list_sessions",
		mcp.WithDescription("List session archives (newest first)."),
		mcp.WithString("workspace", mcp.Description("machine/repo_name or empty for own")),
		mcp.WithNumber("limit", mcp.Description("Max entries (default 50)")),
	), s.listSessions)

	srv.AddTool(mcp.NewTool("read_session",
		mcp.WithDescription("Read a session archive. Supports pagination via offset+limit (line numbers)."),
		mcp.WithString("workspace", mcp.Description("machine/repo_name or empty for own")),
		mcp.WithString("path", mcp.Required()),
		mcp.WithNumber("offset", mcp.Description("Start from line N (default 0, skips envelope)")),
		mcp.WithNumber("limit", mcp.Description("Max lines to return (default 200)")),
	), s.readSession)

	srv.AddTool(mcp.NewTool("search_sessions",
		mcp.WithDescription("Search across compressed session archives. Supports multi-pattern and context lines."),
		mcp.WithString("query", mcp.Required(), mcp.Description("Search pattern (regex). Multiple patterns: separate with |")),
		mcp.WithString("workspace", mcp.Description("machine/repo_name, 'all', or empty for own")),
		mcp.WithNumber("context_lines", mcp.Description("Lines of context around each match (default 0, like grep -C)")),
	), s.searchSessions)

	// === AI-ONLY: write_overview + delete_knowledge (session-bound) ===

	srv.AddTool(mcp.NewTool("write_overview",
		mcp.WithDescription("Write your workspace's overview.md. This is the description other AIs see in get_overview. Only writes to YOUR bound workspace."),
		mcp.WithString("content", mcp.Required(), mcp.Description("Markdown content for overview.md")),
	), s.writeOverview)

	srv.AddTool(mcp.NewTool("delete_knowledge",
		mcp.WithDescription("Delete a knowledge md from YOUR workspace only. Cannot delete sessions or other workspaces' files."),
		mcp.WithString("path", mcp.Required(), mcp.Description("Relative path within knowledge/")),
	), s.deleteKnowledge)

	// === SYNC-ONLY: upload_knowledge, upload_session (require write_token) ===

	srv.AddTool(mcp.NewTool("upload_knowledge",
		mcp.WithDescription("Upload a knowledge md file. Requires SYNC_TOKEN + workspace_id + write_token. Saves old version to .versions/ before overwrite."),
		mcp.WithString("workspace_id", mcp.Required()),
		mcp.WithString("write_token", mcp.Required()),
		mcp.WithString("path", mcp.Required()),
		mcp.WithString("content", mcp.Required()),
		mcp.WithString("origin", mcp.Description("'local' or 'pulled:<source_label>' (default: local)")),
	), s.uploadKnowledge)

	srv.AddTool(mcp.NewTool("upload_session",
		mcp.WithDescription("Upload a session archive. Requires SYNC_TOKEN + workspace_id + write_token."),
		mcp.WithString("workspace_id", mcp.Required()),
		mcp.WithString("write_token", mcp.Required()),
		mcp.WithString("filename", mcp.Required()),
		mcp.WithString("content_b64", mcp.Required()),
		mcp.WithString("tool", mcp.Description("Tool name")),
		mcp.WithString("tool_version", mcp.Description("Tool version")),
		mcp.WithString("source_path", mcp.Description("Original file path")),
		mcp.WithString("source_mtime", mcp.Description("Original mtime (RFC3339)")),
		mcp.WithBoolean("already_gzipped", mcp.Description("Content is already gzip")),
		mcp.WithString("origin", mcp.Description("'local' or 'pulled:<source_label>'")),
	), s.uploadSession)
}

// ── helpers ──────────────────────────────────────────────────────────────

func errResult(msg string) *mcp.CallToolResult { return mcp.NewToolResultError(msg) }
func okText(s string) *mcp.CallToolResult      { return mcp.NewToolResultText(s) }

func firstLine(path string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 4096), 64*1024)
	if sc.Scan() {
		return strings.TrimSpace(sc.Text())
	}
	return ""
}

func jsonPretty(v any) string {
	b, _ := json.MarshalIndent(v, "", "  ")
	return string(b)
}

func wsLabel(w Identity) string {
	if w.RepoName != "" {
		return w.Machine + "/" + w.RepoName
	}
	if w.Tool != "" && w.Project != "" {
		return w.Machine + "/" + w.Tool + "/" + w.Project
	}
	if w.Tool != "" {
		return w.Machine + "/" + w.Tool
	}
	return w.ID
}

func strOr(m map[string]any, k string) string {
	v, _ := m[k].(string)
	return v
}

func numOr(m map[string]any, k string, def int) int {
	if v, ok := m[k].(float64); ok && v >= 0 {
		return int(v)
	}
	return def
}

// ── get_overview ─────────────────────────────────────────────────────────

func (s *Service) getOverview(ctx context.Context, _ mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	var buf strings.Builder
	role := auth.RoleFromContext(ctx)

	if data, err := os.ReadFile(s.paths.GlobalReadme); err == nil {
		buf.Write(data)
		buf.WriteString("\n\n")
	}

	list, err := s.reg.List()
	if err != nil {
		return errResult(err.Error()), nil
	}
	buf.WriteString("# Workspaces\n\n")
	if len(list) == 0 {
		buf.WriteString("(none yet)\n")
		return okText(buf.String()), nil
	}

	for _, w := range list {
		label := wsLabel(w)
		if role == auth.RoleSync {
			fmt.Fprintf(&buf, "## %s  (id: %s)\n", label, w.ID)
		} else {
			fmt.Fprintf(&buf, "## %s\n", label)
		}
		if w.Owner != "" {
			fmt.Fprintf(&buf, "- owner: %s\n", w.Owner)
		}

		kdir, _ := s.paths.KnowledgeDir(w.ID)
		sdir, _ := s.paths.SessionsDir(w.ID)

		// Count knowledge + sessions
		mds, sessions := 0, 0
		_ = filepath.WalkDir(kdir, func(_ string, d fs.DirEntry, _ error) error {
			if d != nil && !d.IsDir() && strings.HasSuffix(d.Name(), ".md") {
				mds++
			}
			return nil
		})
		_ = filepath.WalkDir(sdir, func(p string, d fs.DirEntry, _ error) error {
			if d != nil && !d.IsDir() && strings.HasSuffix(p, ".jsonl.gz") {
				sessions++
			}
			return nil
		})
		fmt.Fprintf(&buf, "- knowledge: %d md  |  sessions: %d\n", mds, sessions)

		// Show overview.md if present
		overviewPath := filepath.Join(s.paths.WorkspacesDir, w.ID, "overview.md")
		if data, err := os.ReadFile(overviewPath); err == nil {
			snippet := string(data)
			if len(snippet) > 2048 {
				snippet = snippet[:2048] + "\n...(truncated)"
			}
			buf.WriteString("\n")
			buf.WriteString(snippet)
			if !strings.HasSuffix(snippet, "\n") {
				buf.WriteString("\n")
			}
		} else {
			buf.WriteString("- _(no overview.md yet)_\n")
		}
		buf.WriteString("\n")
	}
	return okText(buf.String()), nil
}

// ── list_workspaces ──────────────────────────────────────────────────────

func (s *Service) listWorkspaces(ctx context.Context, _ mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	list, err := s.reg.List()
	if err != nil {
		return errResult(err.Error()), nil
	}
	role := auth.RoleFromContext(ctx)

	var buf strings.Builder
	for _, w := range list {
		label := wsLabel(w)
		kdir, _ := s.paths.KnowledgeDir(w.ID)
		sdir, _ := s.paths.SessionsDir(w.ID)
		mds, sessions := 0, 0
		_ = filepath.WalkDir(kdir, func(_ string, d fs.DirEntry, _ error) error {
			if d != nil && !d.IsDir() && strings.HasSuffix(d.Name(), ".md") {
				mds++
			}
			return nil
		})
		_ = filepath.WalkDir(sdir, func(p string, d fs.DirEntry, _ error) error {
			if d != nil && !d.IsDir() && strings.HasSuffix(p, ".jsonl.gz") {
				sessions++
			}
			return nil
		})
		if role == auth.RoleSync {
			fmt.Fprintf(&buf, "%s  (id=%s)  knowledge=%d  sessions=%d\n", label, w.ID, mds, sessions)
		} else {
			fmt.Fprintf(&buf, "%s  knowledge=%d  sessions=%d\n", label, mds, sessions)
		}
	}
	return okText(buf.String()), nil
}

// ── register_workspace ───────────────────────────────────────────────────

func (s *Service) registerWorkspace(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args := req.GetArguments()
	ws := Identity{
		Machine:  strOr(args, "machine"),
		RepoName: strOr(args, "repo_name"),
		Tool:     strOr(args, "tool"),
		Project:  strOr(args, "project"),
		CwdHint:  strOr(args, "cwd_hint"),
		Owner:    strOr(args, "owner"),
	}
	out, err := s.reg.Upsert(ws)
	if err != nil {
		return errResult(err.Error()), nil
	}

	role := auth.RoleFromContext(ctx)
	if role == auth.RoleAI {
		s.bindSession(ctx, out.ID)
		return okText(fmt.Sprintf(
			"Workspace bound: %s/%s\n"+
				"All write_overview and delete_knowledge calls route here automatically.\n"+
				"To upload files, add paths to ~/.memory-sync/tracked.txt and run sync.",
			out.Machine, out.RepoName)), nil
	}
	// Sync: return full info including write_token.
	return okText(fmt.Sprintf("registered: %s\nwrite_token: %s\n%s",
		out.ID, out.WriteToken, jsonPretty(out))), nil
}

// ── write_overview (AI only, session-bound) ──────────────────────────────

func (s *Service) writeOverview(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	role := auth.RoleFromContext(ctx)
	if role != auth.RoleAI {
		return errResult("write_overview is for AI clients only (use upload_knowledge for sync)"), nil
	}
	ws := s.boundWorkspace(ctx)
	if ws == "" {
		return errResult("no workspace bound — call register_workspace first"), nil
	}
	content := strOr(req.GetArguments(), "content")
	if len(content) > maxOverviewSize {
		return errResult(fmt.Sprintf("overview too large (%d B, max %d)", len(content), maxOverviewSize)), nil
	}

	overviewPath := filepath.Join(s.paths.WorkspacesDir, ws, "overview.md")

	// Version old file before overwrite
	versionBeforeOverwrite(overviewPath)

	if err := atomicWrite(overviewPath, []byte(content)); err != nil {
		return errResult("write: " + err.Error()), nil
	}
	return okText("overview.md updated"), nil
}

// ── delete_knowledge (session-bound, own workspace only) ─────────────────

func (s *Service) deleteKnowledge(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	ws := s.boundWorkspace(ctx)
	if ws == "" {
		// Sync token: use explicit workspace_id
		role := auth.RoleFromContext(ctx)
		if role == auth.RoleSync {
			ws = strOr(req.GetArguments(), "workspace_id")
		}
	}
	if ws == "" {
		return errResult("no workspace bound — call register_workspace first"), nil
	}

	rel := strOr(req.GetArguments(), "path")
	abs, err := s.paths.ResolveKnowledge(ws, rel)
	if err != nil {
		return errResult(err.Error()), nil
	}
	info, err := os.Stat(abs)
	if err != nil {
		return errResult("not found: " + rel), nil
	}
	if info.IsDir() {
		return errResult("cannot delete directories"), nil
	}
	if err := os.Remove(abs); err != nil {
		return errResult("delete: " + err.Error()), nil
	}
	// Also remove .meta sidecar if present
	_ = os.Remove(abs + ".meta")
	return okText("deleted: " + rel), nil
}

// ── list_knowledge ───────────────────────────────────────────────────────

func (s *Service) listKnowledge(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	param := strOr(req.GetArguments(), "workspace")
	id, err := s.resolveRead(ctx, param)
	if err != nil {
		return errResult(err.Error()), nil
	}

	var buf strings.Builder
	emit := func(wsID string) {
		dir, err := s.paths.KnowledgeDir(wsID)
		if err != nil {
			return
		}
		ws, _ := s.reg.Get(wsID)
		label := wsID
		if ws != nil {
			label = wsLabel(*ws)
		}
		_ = filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
			if err != nil || d.IsDir() {
				return nil
			}
			if !strings.HasSuffix(strings.ToLower(d.Name()), ".md") {
				return nil
			}
			// Skip .versions dir
			rel, _ := filepath.Rel(dir, p)
			if strings.HasPrefix(filepath.ToSlash(rel), ".versions/") {
				return nil
			}
			rel = filepath.ToSlash(rel)
			title := firstLine(p)
			info, _ := d.Info()

			// Check for origin tag
			origin := "local"
			if metaData, err := os.ReadFile(p + ".meta"); err == nil {
				var meta map[string]string
				if json.Unmarshal(metaData, &meta) == nil {
					if o := meta["origin"]; o != "" {
						origin = o
					}
				}
			}

			originTag := ""
			if origin != "local" {
				originTag = "  [" + origin + "]"
			}
			fmt.Fprintf(&buf, "[%s] %s  %s  (%d B)%s\n",
				label, rel, title, info.Size(), originTag)
			return nil
		})
	}

	if id == "all" {
		list, _ := s.reg.List()
		for _, w := range list {
			emit(w.ID)
		}
	} else {
		emit(id)
	}
	if buf.Len() == 0 {
		return okText("(empty)"), nil
	}
	return okText(buf.String()), nil
}

// ── read_knowledge ───────────────────────────────────────────────────────

func (s *Service) readKnowledge(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args := req.GetArguments()
	param := strOr(args, "workspace")
	rel := strOr(args, "path")
	id, err := s.resolveRead(ctx, param)
	if err != nil {
		return errResult(err.Error()), nil
	}
	abs, err := s.paths.ResolveKnowledge(id, rel)
	if err != nil {
		return errResult(err.Error()), nil
	}
	data, err := os.ReadFile(abs)
	if err != nil {
		return errResult("not found: " + rel), nil
	}
	return okText(string(data)), nil
}

// ── search_knowledge ─────────────────────────────────────────────────────

func (s *Service) searchKnowledge(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args := req.GetArguments()
	query := strOr(args, "query")
	param := strOr(args, "workspace")
	if query == "" {
		return errResult("query required"), nil
	}

	id, _ := s.resolveRead(ctx, param)

	var paths []string
	if id == "" || id == "all" {
		list, _ := s.reg.List()
		for _, w := range list {
			d, _ := s.paths.KnowledgeDir(w.ID)
			paths = append(paths, d)
		}
	} else {
		d, _ := s.paths.KnowledgeDir(id)
		paths = append(paths, d)
	}
	return runGrep(ctx, query, paths, true, 0, s.paths.Root)
}

// ── list_sessions ────────────────────────────────────────────────────────

func (s *Service) listSessions(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args := req.GetArguments()
	param := strOr(args, "workspace")
	limit := numOr(args, "limit", 50)

	id, err := s.resolveRead(ctx, param)
	if err != nil {
		return errResult(err.Error()), nil
	}

	type entry struct {
		display string // "machine/repo: rel_path"
		mtime   time.Time
		size    int64
	}
	var entries []entry

	scanWS := func(wsID string) {
		base, err := s.paths.SessionsDir(wsID)
		if err != nil {
			return
		}
		ws, _ := s.reg.Get(wsID)
		label := wsID
		if ws != nil {
			label = wsLabel(*ws)
		}
		_ = filepath.WalkDir(base, func(p string, d fs.DirEntry, err error) error {
			if err != nil || d.IsDir() || !strings.HasSuffix(p, ".jsonl.gz") {
				return nil
			}
			info, _ := d.Info()
			rel, _ := filepath.Rel(base, p)
			display := fmt.Sprintf("[%s] %s", label, filepath.ToSlash(rel))
			entries = append(entries, entry{display, info.ModTime(), info.Size()})
			return nil
		})
	}

	if id == "all" {
		list, _ := s.reg.List()
		for _, w := range list {
			scanWS(w.ID)
		}
	} else {
		scanWS(id)
	}

	sort.Slice(entries, func(i, j int) bool { return entries[i].mtime.After(entries[j].mtime) })
	if len(entries) > limit {
		entries = entries[:limit]
	}

	var buf strings.Builder
	for _, e := range entries {
		fmt.Fprintf(&buf, "%s  %d B  %s\n", e.display, e.size, e.mtime.UTC().Format(time.RFC3339))
	}
	if buf.Len() == 0 {
		return okText("(no sessions)"), nil
	}
	return okText(buf.String()), nil
}

// ── read_session (with pagination) ───────────────────────────────────────

func (s *Service) readSession(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args := req.GetArguments()
	param := strOr(args, "workspace")
	rel := strOr(args, "path")
	offset := numOr(args, "offset", 0)
	limit := numOr(args, "limit", 200)

	id, err := s.resolveRead(ctx, param)
	if err != nil {
		return errResult(err.Error()), nil
	}
	abs, err := s.paths.ResolveSession(id, rel)
	if err != nil {
		return errResult(err.Error()), nil
	}
	f, err := os.Open(abs)
	if err != nil {
		return errResult("not found: " + rel), nil
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return errResult("not gzip: " + err.Error()), nil
	}
	defer gz.Close()

	sc := bufio.NewScanner(gz)
	sc.Buffer(make([]byte, 0, 64*1024), 1<<20)

	var buf strings.Builder
	lineNum := 0
	collected := 0
	for sc.Scan() {
		if lineNum >= offset && collected < limit {
			buf.Write(sc.Bytes())
			buf.WriteByte('\n')
			collected++
		}
		lineNum++
		if collected >= limit {
			break
		}
	}
	if collected == 0 {
		return okText(fmt.Sprintf("(no lines at offset %d, total %d lines)", offset, lineNum)), nil
	}
	fmt.Fprintf(&buf, "\n--- lines %d-%d of %d+ ---", offset, offset+collected-1, lineNum)
	return okText(buf.String()), nil
}

// ── search_sessions (with context_lines) ─────────────────────────────────

func (s *Service) searchSessions(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args := req.GetArguments()
	query := strOr(args, "query")
	param := strOr(args, "workspace")
	contextLines := numOr(args, "context_lines", 0)
	if query == "" {
		return errResult("query required"), nil
	}

	id, _ := s.resolveRead(ctx, param)

	var roots []string
	if id == "" || id == "all" {
		list, _ := s.reg.List()
		for _, w := range list {
			d, _ := s.paths.SessionsDir(w.ID)
			roots = append(roots, d)
		}
	} else {
		d, _ := s.paths.SessionsDir(id)
		roots = append(roots, d)
	}
	return runGrep(ctx, query, roots, false, contextLines, s.paths.Root)
}

// ── upload_knowledge (SYNC_TOKEN + write_token only) ─────────────────────

func (s *Service) uploadKnowledge(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	role := auth.RoleFromContext(ctx)
	if role != auth.RoleSync {
		return errResult("upload_knowledge requires SYNC_TOKEN. AI clients: write files locally and use tracked.txt."), nil
	}
	args := req.GetArguments()
	wsID := strOr(args, "workspace_id")
	writeToken := strOr(args, "write_token")
	rel := strOr(args, "path")
	content := strOr(args, "content")
	origin := strOr(args, "origin")
	if origin == "" {
		origin = "local"
	}

	if err := s.resolveWriteSync(wsID, writeToken); err != nil {
		return errResult(err.Error()), nil
	}
	if len(content) > maxKnowledgeSize {
		return errResult(fmt.Sprintf("too large (%d B, max %d)", len(content), maxKnowledgeSize)), nil
	}

	abs, err := s.paths.ResolveKnowledge(wsID, rel)
	if err != nil {
		return errResult(err.Error()), nil
	}
	if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
		return errResult("mkdir: " + err.Error()), nil
	}

	// Version old file before overwrite
	versionBeforeOverwrite(abs)

	if err := atomicWrite(abs, []byte(content)); err != nil {
		return errResult("write: " + err.Error()), nil
	}

	// Write origin .meta sidecar
	meta := map[string]string{"origin": origin, "updated_at": time.Now().UTC().Format(time.RFC3339)}
	metaJSON, _ := json.Marshal(meta)
	_ = os.WriteFile(abs+".meta", metaJSON, 0o644)

	return okText("written: " + rel), nil
}

// ── upload_session (SYNC_TOKEN + write_token only) ───────────────────────

func (s *Service) uploadSession(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	role := auth.RoleFromContext(ctx)
	if role != auth.RoleSync {
		return errResult("upload_session requires SYNC_TOKEN. Sessions are uploaded by the sync script automatically."), nil
	}
	args := req.GetArguments()
	wsID := strOr(args, "workspace_id")
	writeToken := strOr(args, "write_token")
	filename := strOr(args, "filename")
	b64 := strOr(args, "content_b64")
	toolName := strOr(args, "tool")
	toolVer := strOr(args, "tool_version")
	sourcePath := strOr(args, "source_path")
	sourceMtime := strOr(args, "source_mtime")
	alreadyGz, _ := args["already_gzipped"].(bool)
	origin := strOr(args, "origin")
	if origin == "" {
		origin = "local"
	}

	if err := s.resolveWriteSync(wsID, writeToken); err != nil {
		return errResult(err.Error()), nil
	}
	ws, _ := s.reg.Get(wsID)

	if filename == "" {
		return errResult("filename required"), nil
	}
	if strings.ContainsAny(filename, "/\\") || strings.Contains(filename, "..") {
		return errResult("filename must be simple (no slashes)"), nil
	}
	if !strings.HasSuffix(filename, ".jsonl") && !strings.HasSuffix(filename, ".jsonl.gz") {
		return errResult("filename must end with .jsonl or .jsonl.gz"), nil
	}

	raw, err := base64.StdEncoding.DecodeString(b64)
	if err != nil {
		return errResult("bad base64: " + err.Error()), nil
	}
	if len(raw) > maxSessionSize {
		return errResult(fmt.Sprintf("too large (%d B)", len(raw))), nil
	}

	var rawSession []byte
	if alreadyGz {
		gz, err := gzip.NewReader(strings.NewReader(string(raw)))
		if err != nil {
			return errResult("bad gzip: " + err.Error()), nil
		}
		defer gz.Close()
		rawSession, err = io.ReadAll(io.LimitReader(gz, int64(maxSessionSize)+1))
		if err != nil {
			return errResult("gunzip: " + err.Error()), nil
		}
		if len(rawSession) > maxSessionSize {
			return errResult(fmt.Sprintf("decompressed too large (>%d B)", maxSessionSize)), nil
		}
	} else {
		rawSession = raw
	}

	h := sha256.Sum256(rawSession)
	hashHex := hex.EncodeToString(h[:])

	var t time.Time
	if sourceMtime != "" {
		if parsed, err := time.Parse(time.RFC3339, sourceMtime); err == nil {
			t = parsed
		}
	}
	if t.IsZero() {
		t = time.Now().UTC()
	}
	subdir := filepath.Join(fmt.Sprintf("%04d", t.Year()), fmt.Sprintf("%02d", int(t.Month())))
	base := strings.TrimSuffix(filename, ".gz")
	target := base + ".gz"
	rel := filepath.Join(subdir, target)
	abs, err := s.paths.ResolveSession(wsID, filepath.ToSlash(rel))
	if err != nil {
		return errResult(err.Error()), nil
	}

	// Dedup
	if existing, ok := readStoredEnvelope(abs); ok && existing.SHA256 == hashHex {
		return okText(fmt.Sprintf("already uploaded: %s", filepath.ToSlash(rel))), nil
	}

	if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
		return errResult("mkdir: " + err.Error()), nil
	}

	tmp := abs + ".tmp"
	f, err := os.Create(tmp)
	if err != nil {
		return errResult("create: " + err.Error()), nil
	}
	gw := gzip.NewWriter(f)

	if toolName == "" && ws != nil {
		toolName = "sync"
	}
	envelope := Envelope{
		Type:          "envelope",
		SchemaVersion: 1,
		WorkspaceID:   wsID,
		Tool:          toolName,
		ToolVersion:   toolVer,
		SessionID:     guessSessionID(filename),
		SourcePath:    sourcePath,
		SourceSize:    len(rawSession),
		SourceMtime:   sourceMtime,
		UploadedAt:    time.Now().UTC().Format(time.RFC3339),
		SHA256:        hashHex,
		Origin:        origin,
	}
	envLine, _ := json.Marshal(envelope)
	_, _ = gw.Write(append(envLine, '\n'))
	_, _ = gw.Write(rawSession)
	if len(rawSession) > 0 && rawSession[len(rawSession)-1] != '\n' {
		_, _ = gw.Write([]byte{'\n'})
	}
	if err := gw.Close(); err != nil {
		f.Close()
		os.Remove(tmp)
		return errResult("gzip: " + err.Error()), nil
	}
	f.Close()
	if err := os.Rename(tmp, abs); err != nil {
		os.Remove(tmp)
		return errResult("rename: " + err.Error()), nil
	}
	return okText(fmt.Sprintf("uploaded: %s (sha256=%s bytes=%d)",
		filepath.ToSlash(rel), hashHex[:12], len(rawSession))), nil
}

// ── Envelope ─────────────────────────────────────────────────────────────

type Envelope struct {
	Type          string `json:"type"`
	SchemaVersion int    `json:"schema_version"`
	WorkspaceID   string `json:"workspace_id"`
	Tool          string `json:"tool"`
	ToolVersion   string `json:"tool_version,omitempty"`
	SessionID     string `json:"session_id,omitempty"`
	SourcePath    string `json:"source_path,omitempty"`
	SourceSize    int    `json:"source_size"`
	SourceMtime   string `json:"source_mtime,omitempty"`
	UploadedAt    string `json:"uploaded_at"`
	SHA256        string `json:"sha256"`
	Origin        string `json:"origin,omitempty"`
}

func readStoredEnvelope(path string) (*Envelope, bool) {
	f, err := os.Open(path)
	if err != nil {
		return nil, false
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return nil, false
	}
	defer gz.Close()
	sc := bufio.NewScanner(gz)
	sc.Buffer(make([]byte, 0, 4096), 1<<20)
	if !sc.Scan() {
		return nil, false
	}
	var env Envelope
	if err := json.Unmarshal(sc.Bytes(), &env); err != nil || env.Type != "envelope" {
		return nil, false
	}
	return &env, true
}

func guessSessionID(filename string) string {
	name := strings.TrimSuffix(strings.TrimSuffix(filename, ".gz"), ".jsonl")
	parts := strings.Split(name, "-")
	if len(parts) >= 5 {
		return strings.Join(parts[len(parts)-5:], "-")
	}
	return name
}

// ── versioning ───────────────────────────────────────────────────────────

func versionBeforeOverwrite(path string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return // doesn't exist yet, nothing to version
	}
	dir := filepath.Dir(path)
	name := filepath.Base(path)
	vdir := filepath.Join(dir, ".versions", name)
	_ = os.MkdirAll(vdir, 0o755)
	ts := time.Now().UTC().Format("2006-01-02T15-04-05Z")
	_ = os.WriteFile(filepath.Join(vdir, ts+".md"), data, 0o644)
}

func atomicWrite(path string, data []byte) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// ── grep helper ──────────────────────────────────────────────────────────

func runGrep(_ context.Context, query string, paths []string, knowledgeOnly bool, contextLines int, root string) (*mcp.CallToolResult, error) {
	if len(paths) == 0 {
		return okText("(nothing to search)"), nil
	}
	ctx2, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	var cmd *exec.Cmd
	if _, err := exec.LookPath("rg"); err == nil {
		argv := []string{"--no-heading", "-n", "-i", "--max-count=5", "--max-filesize=1M"}
		if knowledgeOnly {
			argv = append(argv, "--glob", "*.md", "--glob", "!.versions/**")
		} else {
			argv = append(argv, "-z") // search gzipped files
		}
		if contextLines > 0 {
			argv = append(argv, fmt.Sprintf("-C%d", contextLines))
		}
		argv = append(argv, query)
		argv = append(argv, paths...)
		cmd = exec.CommandContext(ctx2, "rg", argv...)
	} else {
		argv := []string{"-r", "-n", "-i"}
		if knowledgeOnly {
			argv = append(argv, "--include=*.md")
		}
		argv = append(argv, query)
		argv = append(argv, paths...)
		cmd = exec.CommandContext(ctx2, "grep", argv...)
	}
	out, _ := cmd.Output()
	if ctx2.Err() != nil {
		return errResult("search timed out"), nil
	}
	result := strings.ReplaceAll(string(out), root+"/", "")
	if len(result) == 0 {
		return okText("no matches"), nil
	}
	if len(result) > searchLimit {
		result = result[:searchLimit] + "\n...(truncated)"
	}
	return okText(result), nil
}
