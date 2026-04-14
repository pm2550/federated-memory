#!/bin/bash
# Called by GitHub Actions after uploading the new binary.
set -euo pipefail

INSTALL_DIR=/opt/mcp-memory-server
BINARY_SRC=/tmp/memory-server-new

echo "=== Saving rollback point ==="
cp "$INSTALL_DIR/bin/memory-server" "$INSTALL_DIR/bin/memory-server.bak" 2>/dev/null || true

echo "=== Installing new binary ==="
chmod +x "$BINARY_SRC"
mv "$BINARY_SRC" "$INSTALL_DIR/bin/memory-server"

echo "=== Restarting service ==="
sudo systemctl restart mcp-memory

echo "=== Verifying ==="
sleep 3
if ! sudo systemctl is-active --quiet mcp-memory; then
    echo "ERROR: service failed to start — rolling back"
    mv "$INSTALL_DIR/bin/memory-server.bak" "$INSTALL_DIR/bin/memory-server"
    sudo systemctl restart mcp-memory
    exit 1
fi

echo "=== Deploy OK ==="
sudo systemctl status mcp-memory --no-pager -l
