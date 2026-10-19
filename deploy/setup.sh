#!/usr/bin/env bash
# One-shot server setup for Ubuntu on AWS Lightsail. Run from inside ~/stockbot:
#   bash deploy/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
# never stop on Ubuntu's pink "restart services?" screen
APT="sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get"

echo "==> System packages"
$APT update -y
$APT install -y python3 python3-venv python3-pip unzip

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo
  echo "STOP: this bot needs Python 3.9 or newer; this server has $(python3 -V 2>&1)."
  echo "Tell Claude: 'setup says it needs Python 3.9' and paste: $(grep PRETTY /etc/os-release)"
  exit 1
fi

echo "==> Python environment"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "==> Headless Chrome + its system libraries"
sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a .venv/bin/playwright install-deps chromium
.venv/bin/playwright install chromium

echo "==> Secrets file"
[ -f .env ] || cp .env.example .env
chmod 600 .env
[ -f session.json ] && chmod 600 session.json || true

echo "==> Daily 6:00 IST schedule (Mon-Fri)"
sed "s#__APP_DIR__#${APP_DIR}#g; s#__USER__#$(whoami)#g" deploy/stockbot.service | sudo tee /etc/systemd/system/stockbot.service >/dev/null
sudo cp deploy/stockbot.timer /etc/systemd/system/stockbot.timer
sudo systemctl daemon-reload
sudo systemctl enable --now stockbot.timer

echo "==> Telegram listener (always on)"
sed "s#__APP_DIR__#${APP_DIR}#g; s#__USER__#$(whoami)#g" deploy/stockbot-listener.service | sudo tee /etc/systemd/system/stockbot-listener.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now stockbot-listener

echo
systemctl list-timers stockbot.timer --no-pager || true
echo
echo "Done. Next: Step 4 of the guide (nano .env), then run:  .venv/bin/python doctor.py"
