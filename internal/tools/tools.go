package tools

import (
	"context"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"

	"memory-server/internal/sandbox"
)

const maxFileSize = 1 * 1024 * 1024 // 1 MB

type toolSet struct{ root string }

// Register adds all tools to the MCP server.
func Register(s *server.MCPServer, memoryRoot string) {
	t := &toolSet{root: memoryRoot}

	s.AddTool(mcp.NewTool("list_files",
		mcp.WithDescription("List files and directories under the memory root"),
		mcp.WithString("path", mcp.Description("Relative path to list (default: root)")),
	), t.listFiles)

	s.AddTool(mcp.NewTool("read_file",
		mcp.WithDescription("Read a memory file (max 1 MB)"),
		mcp.WithString("path", mcp.Required(), mcp.Description("Relative path to file")),
	), t.readFile)

	s.AddTool(mcp.NewTool("write_file",
		mcp.WithDescription("Atomically write or overwrite a memory file"),
		mcp.WithString("path", mcp.Required(), mcp.Description("Relative path")),
		mcp.WithString("content", mcp.Required(), mcp.Description("File content")),
	), t.writeFile)

	s.AddTool(mcp.NewTool("delete_file",
		mcp.WithDescription("Delete a memory file (directories not allowed)"),
		mcp.WithString("path", mcp.Required(), mcp.Description("Relative path to file")),
	), t.deleteFile)

	s.AddTool(mcp.NewTool("search",
		mcp.WithDescription("Search file contents (ripgrep if available, else grep). Returns matching lines with file:line prefix."),
		mcp.WithString("query", mcp.Required(), mcp.Description("Search pattern (regex)")),
		mcp.WithString("path", mcp.Description("Subdirectory to search in (default: all)")),
	), t.search)
}

// ── list_files ──────────────────────────────────────────────────────────────

func (t *toolSet) listFiles(_ context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args := req.GetArguments()
	rel, _ := args["path"].(string)
	abs, err := sandbox.Validate(t.root, rel)
	if err != nil {
		return mcp.NewToolResultError(err.Error()), nil
	}

	var lines []string
	_ = filepath.WalkDir(abs, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		display, _ := filepath.Rel(t.root, path)
		if d.IsDir() {
			lines = append(lines, "[dir]  "+display)
		} else {
			info, _ := d.Info()
			lines = append(lines, fmt.Sprintf("[file] %s  (%d B)", display, info.Size()))
		}
		return nil
	})

	if len(lines) == 0 {
		return mcp.NewToolResultText("(empty)"), nil
	}
	return mcp.NewToolResultText(strings.Join(lines, "\n")), nil
}

// ── read_file ────────────────────────────────────────────────────────────────

func (t *toolSet) readFile(_ context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args := req.GetArguments()
	rel, _ := args["path"].(string)
	abs, err := sandbox.Validate(t.root, rel)
	if err != nil {
		return mcp.NewToolResultError(err.Error()), nil
	}

	info, err := os.Stat(abs)
	if err != nil {
		return mcp.NewToolResultError(fmt.Sprintf("not found: %s", rel)), nil
	}
	if info.IsDir() {
		return mcp.NewToolResultError("path is a directory — use list_files"), nil
	}
	if info.Size() > maxFileSize {
		return mcp.NewToolResultError(fmt.Sprintf("file too large (%d B, max 1 MB)", info.Size())), nil
	}

	data, err := os.ReadFile(abs)
	if err != nil {
		return mcp.NewToolResultError(fmt.Sprintf("read error: %v", err)), nil
	}
	return mcp.NewToolResultText(string(data)), nil
}

// ── write_file ───────────────────────────────────────────────────────────────

func (t *toolSet) writeFile(_ context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args := req.GetArguments()
	rel, _ := args["path"].(string)
	content, _ := args["content"].(string)

	if len(content) > maxFileSize {
		return mcp.NewToolResultError("content too large (max 1 MB)"), nil
	}

	abs, err := sandbox.Validate(t.root, rel)
	if err != nil {
		return mcp.NewToolResultError(err.Error()), nil
	}

	if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
		return mcp.NewToolResultError(fmt.Sprintf("mkdir error: %v", err)), nil
	}

	// Atomic write: write to temp then rename
	tmp := abs + ".tmp"
	if err := os.WriteFile(tmp, []byte(content), 0o644); err != nil {
		return mcp.NewToolResultError(fmt.Sprintf("write error: %v", err)), nil
	}
	if err := os.Rename(tmp, abs); err != nil {
		_ = os.Remove(tmp)
		return mcp.NewToolResultError(fmt.Sprintf("rename error: %v", err)), nil
	}

	display, _ := filepath.Rel(t.root, abs)
	return mcp.NewToolResultText(fmt.Sprintf("written: %s", display)), nil
}

// ── delete_file ──────────────────────────────────────────────────────────────

func (t *toolSet) deleteFile(_ context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args := req.GetArguments()
	rel, _ := args["path"].(string)
	abs, err := sandbox.Validate(t.root, rel)
	if err != nil {
		return mcp.NewToolResultError(err.Error()), nil
	}

	info, err := os.Stat(abs)
	if err != nil {
		return mcp.NewToolResultError(fmt.Sprintf("not found: %s", rel)), nil
	}
	if info.IsDir() {
		return mcp.NewToolResultError("cannot delete directories"), nil
	}

	if err := os.Remove(abs); err != nil {
		return mcp.NewToolResultError(fmt.Sprintf("delete error: %v", err)), nil
	}
	return mcp.NewToolResultText(fmt.Sprintf("deleted: %s", rel)), nil
}

// ── search ───────────────────────────────────────────────────────────────────

func (t *toolSet) search(_ context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args := req.GetArguments()
	query, _ := args["query"].(string)
	rel, _ := args["path"].(string)

	if query == "" {
		return mcp.NewToolResultError("query is required"), nil
	}

	searchPath, err := sandbox.Validate(t.root, rel)
	if err != nil {
		return mcp.NewToolResultError(err.Error()), nil
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	var cmd *exec.Cmd
	if _, err := exec.LookPath("rg"); err == nil {
		cmd = exec.CommandContext(ctx, "rg",
			"--no-heading", "-n", "-i",
			"--max-count=5", "--max-filesize=1M",
			query, searchPath,
		)
	} else {
		cmd = exec.CommandContext(ctx, "grep",
			"-r", "-n", "-i",
			"--include=*.md", "--include=*.txt",
			query, searchPath,
		)
	}

	out, err := cmd.Output()
	if ctx.Err() != nil {
		return mcp.NewToolResultError("search timed out"), nil
	}
	if err != nil {
		// exit 1 = no matches (not an error)
		return mcp.NewToolResultText("no matches found"), nil
	}

	result := strings.ReplaceAll(string(out), t.root+"/", "")
	if len(result) > 5000 {
		result = result[:5000] + "\n… (truncated)"
	}
	return mcp.NewToolResultText(result), nil
}
