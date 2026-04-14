package config

import (
	"log"
	"os"
	"strconv"
)

type Config struct {
	AIToken    string // For AI clients (session-bound writes)
	SyncToken  string // For sync scripts (explicit workspace writes)
	MemoryRoot string
	Port       int
}

func Load() *Config {
	aiToken := os.Getenv("MCP_TOKEN")
	if aiToken == "" {
		log.Fatal("MCP_TOKEN environment variable is required")
	}

	syncToken := os.Getenv("SYNC_TOKEN")
	if syncToken == "" {
		log.Printf("WARNING: SYNC_TOKEN not set; sync scripts will not be able to authenticate")
	}

	memoryRoot := os.Getenv("MEMORY_ROOT")
	if memoryRoot == "" {
		memoryRoot = "/data/memory"
	}

	port := 8000
	if p := os.Getenv("PORT"); p != "" {
		var err error
		port, err = strconv.Atoi(p)
		if err != nil {
			log.Fatalf("Invalid PORT: %v", err)
		}
	}

	return &Config{
		AIToken:    aiToken,
		SyncToken:  syncToken,
		MemoryRoot: memoryRoot,
		Port:       port,
	}
}
