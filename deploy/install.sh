#!/bin/bash
# First-time server setup. Run as a user with sudo.
set -euo pipefail

INSTALL_DIR=/opt/mcp-memory-server
MEMORY_DIR=/data/memory
BINARY=bin/memory-server-linux-amd64   # built by GitHub Actions and uploaded here

echo "=== Creating mcp system user ==="
id mcp &>/dev/null || useradd -r -s /bin/false -d "$INSTALL_DIR" mcp

echo "=== Creating directories ==="
mkdir -p "$INSTALL_DIR/bin"
mkdir -p "$MEMORY_DIR"/{projects,user,feedback}
chown -R mcp:mcp "$MEMORY_DIR"
chown -R mcp:mcp "$INSTALL_DIR"

echo "=== Installing binary ==="
cp "$BINARY" "$INSTALL_DIR/bin/memory-server"
chmod +x "$INSTALL_DIR/bin/memory-server"

echo "=== Installing systemd service ==="
cp deploy/mcp-memory.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable mcp-memory

echo "=== Installing nginx ==="
apt-get install -y nginx
cp deploy/nginx.conf /etc/nginx/sites-available/mcp-memory
ln -sf /etc/nginx/sites-available/mcp-memory /etc/nginx/sites-enabled/mcp-memory
rm -f /etc/nginx/sites-enabled/default

echo "=== Generating self-signed TLS cert (10 years) ==="
mkdir -p /etc/ssl/mcp
openssl req -x509 -newkey rsa:4096 \
    -keyout /etc/ssl/mcp/key.pem \
    -out    /etc/ssl/mcp/cert.pem \
    -days 3650 -nodes \
    -subj "/CN=mcp-memory-server"
chmod 600 /etc/ssl/mcp/key.pem

nginx -t && systemctl enable --now nginx

echo "=== Installing fail2ban ==="
apt-get install -y fail2ban
cp deploy/fail2ban-filter.conf /etc/fail2ban/filter.d/mcp-auth.conf
cp deploy/fail2ban-jail.conf   /etc/fail2ban/jail.d/mcp.conf
systemctl enable --now fail2ban

echo "=== iptables: rate-limit new connections on port 7443 ==="
iptables -I INPUT -p tcp --dport 7443 -m state --state NEW \
    -m hashlimit \
    --hashlimit-above 20/min \
    --hashlimit-burst 30 \
    --hashlimit-mode srcip \
    --hashlimit-name mcp_new \
    -j DROP

echo "=== Sudoers: allow deploy user to restart service without password ==="
echo "deploy-user ALL=(ALL) NOPASSWD: /bin/systemctl restart mcp-memory, /bin/systemctl status mcp-memory" \
    > /etc/sudoers.d/mcp-deploy
chmod 440 /etc/sudoers.d/mcp-deploy

echo ""
echo "=== DONE ==="
echo "Next: create $INSTALL_DIR/.env with:"
echo "  MCP_TOKEN=<long-random-token>"
echo "  MEMORY_ROOT=/data/memory"
echo "  PORT=8000"
echo ""
echo "Then: sudo systemctl start mcp-memory"
