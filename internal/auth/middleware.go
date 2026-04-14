package auth

import (
	"context"
	"crypto/hmac"
	"log"
	"net/http"
	"strings"
)

// TokenRole distinguishes AI clients (session-bound writes) from
// trusted sync scripts (explicit workspace_id writes).
type TokenRole int

const (
	RoleUnknown TokenRole = iota
	RoleAI                // AI client: writes locked to session-bound workspace
	RoleSync              // Sync script: writes require explicit workspace_id
)

type contextKey int

const tokenRoleKey contextKey = iota

// RoleFromContext returns the token role for this request.
func RoleFromContext(ctx context.Context) TokenRole {
	if v, ok := ctx.Value(tokenRoleKey).(TokenRole); ok {
		return v
	}
	return RoleUnknown
}

// Middleware checks Bearer token on every request.
// Two tokens are supported:
//   - aiToken:   AI clients, writes are session-bound
//   - syncToken: trusted sync scripts, writes use explicit workspace_id
//
// Uses hmac.Equal to prevent timing attacks.
func Middleware(aiToken, syncToken string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw := r.Header.Get("Authorization")
		parts := strings.SplitN(raw, " ", 2)
		if len(parts) != 2 || parts[0] != "Bearer" {
			log.Printf("AUTH FAIL from %s %s (no bearer)", r.RemoteAddr, r.URL.Path)
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		tok := parts[1]

		var role TokenRole
		if hmac.Equal([]byte(tok), []byte(aiToken)) {
			role = RoleAI
		} else if syncToken != "" && hmac.Equal([]byte(tok), []byte(syncToken)) {
			role = RoleSync
		} else {
			log.Printf("AUTH FAIL from %s %s (bad token)", r.RemoteAddr, r.URL.Path)
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}

		ctx := context.WithValue(r.Context(), tokenRoleKey, role)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}
