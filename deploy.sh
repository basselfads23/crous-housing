#!/usr/bin/env bash
# ==============================================================================
# CROUS Watcher - One-Step Ubuntu VPS Setup Script
# ==============================================================================
set -e

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CURRENT_USER="$(whoami)"
SERVICE_NAME="crous-watcher"

echo "=================================================="
echo "🚀 Setting up CROUS Watcher on Ubuntu"
echo "Directory: ${APP_DIR}"
echo "User:      ${CURRENT_USER}"
echo "=================================================="

# 1. Update package list and install Python 3 & pip if not present
echo "📦 Checking system dependencies..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv

# 2. Set up Python virtual environment
echo "🐍 Setting up virtual environment..."
if [ ! -d "${APP_DIR}/venv" ]; then
    python3 -m venv "${APP_DIR}/venv"
fi

"${APP_DIR}/venv/bin/pip" install --upgrade pip
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo "🎭 Installing Playwright Chromium browser & dependencies..."
"${APP_DIR}/venv/bin/playwright" install --with-deps chromium

# 3. Check for .env file
if [ ! -f "${APP_DIR}/.env" ]; then
    echo "⚠️  No .env file found!"
    if [ -f "${APP_DIR}/.env.example" ]; then
        cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
        echo "Created .env from .env.example. Please edit .env with your Telegram bot token."
    fi
fi

# 4. Generate systemd service file dynamically
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
echo "⚙️  Configuring systemd service at ${SERVICE_FILE}..."

sudo tee "${SERVICE_FILE}" > /dev/null <<EOF
[Unit]
Description=CROUS Housing Watcher Daemon (24/7)
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/crous_watcher.py
Restart=always
RestartSec=10
EnvironmentFile=${APP_DIR}/.env

# Safety limits
MemoryMax=500M
CPUQuota=50%

# Logging
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 5. Reload systemd, enable and start service
echo "🔄 Reloading systemd and enabling service..."
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"

echo ""
echo "=================================================="
echo "✅ CROUS Watcher is now running 24/7!"
echo "=================================================="
echo "Useful commands:"
echo "  Check status:  sudo systemctl status ${SERVICE_NAME}"
echo "  View live logs: journalctl -u ${SERVICE_NAME} -f"
echo "  Restart:       sudo systemctl restart ${SERVICE_NAME}"
echo "  Stop:          sudo systemctl stop ${SERVICE_NAME}"
echo "=================================================="
