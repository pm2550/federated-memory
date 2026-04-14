package federation

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// Identity is the full workspace descriptor stored on disk.
type Identity struct {
	ID          string    `json:"id"`
	Machine     string    `json:"machine"`
	Tool        string    `json:"tool,omitempty"`        // e.g. claude-code, codex-cli
	Project     string    `json:"project,omitempty"`     // from cwd
	RepoName    string    `json:"repo_name,omitempty"`   // human-readable display name
	CwdHint     string    `json:"cwd_hint,omitempty"`
	Owner       string    `json:"owner,omitempty"`
	WriteToken  string    `json:"write_token,omitempty"` // per-workspace, for sync script
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`
}

func generateWriteToken() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// Registry is a file-backed list of known workspaces.
type Registry struct {
	mu    sync.Mutex
	paths *Paths
}

func NewRegistry(p *Paths) *Registry {
	return &Registry{paths: p}
}

func (r *Registry) Ensure() error {
	for _, d := range []string{r.paths.MetaDir, r.paths.WorkspacesDir} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return err
		}
	}
	if _, err := os.Stat(r.paths.WorkspacesJSON); os.IsNotExist(err) {
		if err := os.WriteFile(r.paths.WorkspacesJSON, []byte("[]\n"), 0o644); err != nil {
			return err
		}
	}
	return nil
}

func (r *Registry) List() ([]Identity, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.loadLocked()
}

func (r *Registry) loadLocked() ([]Identity, error) {
	data, err := os.ReadFile(r.paths.WorkspacesJSON)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var list []Identity
	if len(data) == 0 {
		return nil, nil
	}
	if err := json.Unmarshal(data, &list); err != nil {
		return nil, fmt.Errorf("decode workspaces.json: %w", err)
	}
	return list, nil
}

func (r *Registry) saveLocked(list []Identity) error {
	sort.SliceStable(list, func(i, j int) bool {
		return list[i].UpdatedAt.After(list[j].UpdatedAt)
	})
	data, err := json.MarshalIndent(list, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	tmp := r.paths.WorkspacesJSON + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, r.paths.WorkspacesJSON)
}

// Upsert inserts or updates identity. Generates write_token on first insert.
// Accepts either (machine + repo_name) or (machine + tool + project) for backward compat.
func (r *Registry) Upsert(in Identity) (*Identity, error) {
	if in.Machine == "" {
		return nil, fmt.Errorf("machine is required")
	}
	// Compute ID: prefer repo_name if given, else tool-project
	var id string
	if in.RepoName != "" {
		id = WorkspaceID(in.Machine, in.RepoName)
	} else if in.Tool != "" && in.Project != "" {
		id = WorkspaceID(in.Machine, in.Tool, in.Project)
		if in.RepoName == "" {
			in.RepoName = in.Tool + "/" + in.Project // display label
		}
	} else if in.Tool != "" {
		id = WorkspaceID(in.Machine, in.Tool)
		if in.RepoName == "" {
			in.RepoName = in.Tool
		}
	} else {
		return nil, fmt.Errorf("need repo_name, or tool+project")
	}
	in.ID = id

	r.mu.Lock()
	defer r.mu.Unlock()

	list, err := r.loadLocked()
	if err != nil {
		return nil, err
	}

	now := time.Now().UTC()
	found := false
	for i := range list {
		if list[i].ID == id {
			if list[i].CreatedAt.IsZero() {
				list[i].CreatedAt = now
			}
			// Preserve write_token from first registration.
			// If the existing workspace has no write_token (migrated from v2),
			// generate one now.
			if in.WriteToken == "" {
				in.WriteToken = list[i].WriteToken
			}
			if in.WriteToken == "" {
				in.WriteToken = generateWriteToken()
			}
			list[i].Machine = in.Machine
			if in.RepoName != "" {
				list[i].RepoName = in.RepoName
			}
			if in.Tool != "" {
				list[i].Tool = in.Tool
			}
			if in.Project != "" {
				list[i].Project = in.Project
			}
			if in.Owner != "" {
				list[i].Owner = in.Owner
			}
			if in.CwdHint != "" {
				list[i].CwdHint = in.CwdHint
			}
			if in.WriteToken != "" {
				list[i].WriteToken = in.WriteToken
			}
			list[i].UpdatedAt = now
			in = list[i]
			found = true
			break
		}
	}
	if !found {
		in.CreatedAt = now
		in.UpdatedAt = now
		if in.WriteToken == "" {
			in.WriteToken = generateWriteToken()
		}
		list = append(list, in)
	}

	dir := filepath.Join(r.paths.WorkspacesDir, id)
	for _, sub := range []string{
		dir,
		filepath.Join(dir, "knowledge"),
		filepath.Join(dir, "knowledge", ".versions"),
		filepath.Join(dir, "sessions"),
	} {
		if err := os.MkdirAll(sub, 0o755); err != nil {
			return nil, err
		}
	}

	idJSON, err := json.MarshalIndent(in, "", "  ")
	if err != nil {
		return nil, err
	}
	idJSON = append(idJSON, '\n')
	if err := os.WriteFile(filepath.Join(dir, "identity.json"), idJSON, 0o644); err != nil {
		return nil, err
	}

	if err := r.saveLocked(list); err != nil {
		return nil, err
	}

	return &in, nil
}

func (r *Registry) Get(id string) (*Identity, error) {
	if err := ValidateID(id); err != nil {
		return nil, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	list, err := r.loadLocked()
	if err != nil {
		return nil, err
	}
	for i := range list {
		if list[i].ID == id {
			return &list[i], nil
		}
	}
	return nil, fmt.Errorf("workspace not registered: %s", id)
}

// ValidateWriteToken checks that the given token matches the workspace.
func (r *Registry) ValidateWriteToken(wsID, token string) error {
	ws, err := r.Get(wsID)
	if err != nil {
		return err
	}
	if ws.WriteToken == "" {
		return fmt.Errorf("workspace has no write_token (re-register to generate one)")
	}
	if ws.WriteToken != token {
		return fmt.Errorf("write_token mismatch for workspace %s", wsID)
	}
	return nil
}

// FindByLabel looks up a workspace by "machine/repo_name" or "machine/tool/project" label.
func (r *Registry) FindByLabel(label string) (*Identity, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	list, err := r.loadLocked()
	if err != nil {
		return nil, err
	}
	// Try exact match on "machine/repo_name"
	for i := range list {
		if list[i].Machine+"/"+list[i].RepoName == label {
			return &list[i], nil
		}
	}
	// Try "machine/tool/project" format (v2 compat)
	for i := range list {
		if list[i].Tool != "" && list[i].Project != "" {
			if list[i].Machine+"/"+list[i].Tool+"/"+list[i].Project == label {
				return &list[i], nil
			}
		}
	}
	// Try slug match
	for i := range list {
		if Slug(list[i].Machine)+"/"+Slug(list[i].RepoName) == label {
			return &list[i], nil
		}
	}
	return nil, fmt.Errorf("workspace not found by label: %s", label)
}
