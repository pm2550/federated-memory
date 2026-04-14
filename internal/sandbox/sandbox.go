package sandbox

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strings"
)

// safeChars: allow letters, digits, dash, underscore, dot, slash only.
var safeChars = regexp.MustCompile(`^[a-zA-Z0-9_\-./]+$`)

// Validate resolves rel against root and ensures the result stays inside root.
// Returns the absolute path on success.
// Pass rel="" to get root itself (for list_files default).
func Validate(root, rel string) (string, error) {
	if rel == "" || rel == "/" || rel == "." {
		return root, nil
	}

	// Block null bytes and control chars
	if strings.ContainsAny(rel, "\x00\n\r\t") {
		return "", fmt.Errorf("path contains invalid characters")
	}

	if !safeChars.MatchString(rel) {
		return "", fmt.Errorf("path contains disallowed characters (allowed: a-z A-Z 0-9 - _ . /)")
	}

	// Block explicit traversal attempts even before filepath.Clean
	if strings.Contains(rel, "..") {
		return "", fmt.Errorf("path traversal not allowed")
	}

	abs := filepath.Clean(filepath.Join(root, rel))

	// Ensure result is strictly under root
	if abs != root && !strings.HasPrefix(abs, root+string(filepath.Separator)) {
		return "", fmt.Errorf("path escapes memory root")
	}

	return abs, nil
}
