package main

import (
	"fmt"
	"log"
	"net/http"

	"github.com/mark3labs/mcp-go/server"

	"memory-server/internal/auth"
	"memory-server/internal/config"
	"memory-server/internal/federation"
	"memory-server/internal/tools"
)

func main() {
	cfg := config.Load()

	s := server.NewMCPServer("memory-server", "2.0.0",
		server.WithToolCapabilities(true),
	)

	// Federation layer: workspace-aware knowledge + session archives.
	fed, err := federation.NewService(cfg.MemoryRoot)
	if err != nil {
		log.Fatalf("federation init: %v", err)
	}
	fed.Register(s)

	// Raw filesystem tools (backward compatibility; scoped to memory root
	// by the sandbox package). Prefer federation.* tools for new clients.
	tools.Register(s, cfg.MemoryRoot)

	streamable := server.NewStreamableHTTPServer(s,
		server.WithEndpointPath("/mcp"),
	)

	mux := http.NewServeMux()
	mux.Handle("/mcp", streamable)
	mux.Handle("/mcp/", streamable)

	handler := auth.Middleware(cfg.AIToken, cfg.SyncToken, mux)

	addr := fmt.Sprintf("127.0.0.1:%d", cfg.Port)
	log.Printf("memory-server v2 listening on %s (memory=%s)", addr, cfg.MemoryRoot)

	if err := http.ListenAndServe(addr, handler); err != nil {
		log.Fatalf("server error: %v", err)
	}
}
