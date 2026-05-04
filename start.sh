#!/bin/bash
set -e

# Start Tailscale daemon in userspace mode (no kernel modules required)
tailscaled --tun=userspace-networking --outbound-http-proxy-listen=localhost:1055 --socks5-server=localhost:1080 &
TAILSCALED_PID=$!
sleep 3

# Connect to the tailnet
if [ -n "$TAILSCALE_AUTHKEY" ]; then
    tailscale up \
        --authkey="${TAILSCALE_AUTHKEY}" \
        --hostname="vant-railway" \
        --accept-routes \
        --accept-dns=false 2>&1 || echo "Tailscale up failed — continuing without tailnet"
    echo "Tailscale status:"
    tailscale status 2>&1 || true
else
    echo "No TAILSCALE_AUTHKEY set — skipping Tailscale"
fi

# Start the Flask app
exec gunicorn --bind "0.0.0.0:${PORT:-5050}" --workers 1 --timeout 120 server:app
