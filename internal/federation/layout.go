// Package federation implements the federated memory filesystem layout.
//
// Layout:
//   /data/memory/
//     INDEX.md, README.md
//     _meta/workspaces.json
//     workspaces/<id>/
//       identity.json
//       knowledge/*.md
//       sessions/YYYY/MM/*.jsonl.gz
package federation

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strings"
)

// Sanitize a free-form label into a safe slug (a-z0-9-).
var slugRe = regexp.MustCompile(`[^a-z0-9]+`)

func Slug(s string) string {
	s = strings.ToLower(strings.TrimSpace(s))
	s = slugRe.ReplaceAllString(s, "-")
	s = strings.Trim(s, "-")
	if s == "" {
		return "x"
	}
	if len(s) > 48 {
		s = s[:48]
	}
	return s
}

// WorkspaceID = slug(machine)-slug(a)-slug(b)  (2 or 3 segments)
// For backward compat with v2: machine-tool-project still works.
// For v3: machine-repoName also works (repoName can contain dashes).
func WorkspaceID(parts ...string) string {
	slugs := make([]string, 0, len(parts))
	for _, p := range parts {
		s := Slug(p)
		if s != "" && s != "x" {
			slugs = append(slugs, s)
		}
	}
	if len(slugs) == 0 {
		return "x"
	}
	return strings.Join(slugs, "-")
}

// Validate a workspace id supplied by a client request (not generated locally).
var validIDRe = regexp.MustCompile(`^[a-z0-9]+(-[a-z0-9]+)*$`)

func ValidateID(id string) error {
	if len(id) == 0 || len(id) > 200 {
		return fmt.Errorf("invalid workspace id length")
	}
	if !validIDRe.MatchString(id) {
		return fmt.Errorf("invalid workspace id format (expected slug-style lowercase)")
	}
	return nil
}

// Paths holds filesystem locations derived from the memory root.
type Paths struct {
	Root           string
	MetaDir        string
	WorkspacesDir  string
	WorkspacesJSON string
	GlobalIndex    string
	GlobalReadme   string
}

func NewPaths(root string) *Paths {
	return &Paths{
		Root:           root,
		MetaDir:        filepath.Join(root, "_meta"),
		WorkspacesDir:  filepath.Join(root, "workspaces"),
		WorkspacesJSON: filepath.Join(root, "_meta", "workspaces.json"),
		GlobalIndex:    filepath.Join(root, "INDEX.md"),
		GlobalReadme:   filepath.Join(root, "README.md"),
	}
}

// WorkspaceDir returns the root dir of a workspace after validating id.
func (p *Paths) WorkspaceDir(id string) (string, error) {
	if err := ValidateID(id); err != nil {
		return "", err
	}
	return filepath.Join(p.WorkspacesDir, id), nil
}

func (p *Paths) IdentityJSON(id string) (string, error) {
	d, err := p.WorkspaceDir(id)
	if err != nil {
		return "", err
	}
	return filepath.Join(d, "identity.json"), nil
}

func (p *Paths) KnowledgeDir(id string) (string, error) {
	d, err := p.WorkspaceDir(id)
	if err != nil {
		return "", err
	}
	return filepath.Join(d, "knowledge"), nil
}

func (p *Paths) SessionsDir(id string) (string, error) {
	d, err := p.WorkspaceDir(id)
	if err != nil {
		return "", err
	}
	return filepath.Join(d, "sessions"), nil
}

// ResolveKnowledge validates a relative path and returns absolute path under
// the workspace's knowledge/ dir. Rejects traversal and disallowed chars.
func (p *Paths) ResolveKnowledge(id, rel string) (string, error) {
	base, err := p.KnowledgeDir(id)
	if err != nil {
		return "", err
	}
	return safeJoin(base, rel)
}

// ResolveSession validates a relative path and returns absolute path under
// the workspace's sessions/ dir.
func (p *Paths) ResolveSession(id, rel string) (string, error) {
	base, err := p.SessionsDir(id)
	if err != nil {
		return "", err
	}
	return safeJoin(base, rel)
}

// safeJoin is the federation-local path sandbox (same semantics as sandbox pkg
// but duplicated to avoid import cycles and to keep rules in one place).
var safePathRe = regexp.MustCompile(`^[a-zA-Z0-9_\-./]+$`)

func safeJoin(base, rel string) (string, error) {
	if rel == "" {
		return base, nil
	}
	if strings.ContainsAny(rel, "\x00\n\r\t") {
		return "", fmt.Errorf("path contains invalid characters")
	}
	if !safePathRe.MatchString(rel) {
		return "", fmt.Errorf("path contains disallowed characters")
	}
	if strings.Contains(rel, "..") {
		return "", fmt.Errorf("path traversal not allowed")
	}
	abs := filepath.Clean(filepath.Join(base, rel))
	if abs != base && !strings.HasPrefix(abs, base+string(filepath.Separator)) {
		return "", fmt.Errorf("path escapes workspace boundary")
	}
	return abs, nil
}
