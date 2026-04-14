.PHONY: build build-linux test run

build:
	go build -o bin/memory-server ./cmd/server

# Cross-compile for the server (linux amd64)
build-linux:
	GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" \
		-o bin/memory-server-linux-amd64 ./cmd/server

test:
	go test ./...

# Local dev run (creates ./testdata/memory if missing)
run:
	mkdir -p testdata/memory/{projects,user,feedback}
	MCP_TOKEN=dev-token \
	MEMORY_ROOT=./testdata/memory \
	PORT=8000 \
	go run ./cmd/server
