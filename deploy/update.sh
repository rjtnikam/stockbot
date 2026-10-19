#!/usr/bin/env bash
# Run this after you unzip a new version:   bash ~/stockbot/deploy/update.sh
# Safe to run more than once. It never changes your keys, logins, watchlist or philosophy.
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"

echo "==> 1/4 Settings"
touch .env
grep -q '^CLAUDE_FAST_MODEL=' .env || echo 'CLAUDE_FAST_MODEL=claude-haiku-4-5-20251001' >> .env
grep -q '^CLAUDE_CHAT_MODEL=' .env || echo 'CLAUDE_CHAT_MODEL=claude-sonnet-5-5' >> .env
chmod 600 .env
if [ -f session.json ]; then chmod 600 session.json; fi
sed -i 's/thesis_top: 4 /thesis_top: 2 /; s/max_stocks_per_run: 5 /max_stocks_per_run: 3 /' config.yaml

echo "==> 2/4 Python packages"
.venv/bin/pip install -q -r requirements.txt

echo "==> 3/4 Self-test (no internet, no AI cost, no Telegram messages)"
mkdir -p data
for t in test_offline test_hunt test_council test_results test_listener test_access test_observer; do
  if .venv/bin/python -m "tests.$t" > "data/selftest_$t.log" 2>&1; then
    echo "   ok    $t"
  else
    echo "   FAIL  $t  (details: data/selftest_$t.log)"
    exit 1
  fi
done

echo "==> 4/4 Services: Telegram listener (restart) and the evening filing observer (21:30, Mon-Sat)"
sed "s#__APP_DIR__#${APP_DIR}#g; s#__USER__#$(whoami)#g" deploy/stockbot-evening.service \
  | sudo tee /etc/systemd/system/stockbot-evening.service >/dev/null
sudo cp deploy/stockbot-evening.timer /etc/systemd/system/stockbot-evening.timer
sed "s#__APP_DIR__#${APP_DIR}#g; s#__USER__#$(whoami)#g" deploy/stockbot-listener.service \
  | sudo tee /etc/systemd/system/stockbot-listener.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable stockbot-listener >/dev/null 2>&1
sudo systemctl restart stockbot-listener
sudo systemctl enable --now stockbot-evening.timer >/dev/null 2>&1
sleep 3
echo "   listener: $(systemctl is-active stockbot-listener)"
echo "   6 am run: $(systemctl is-enabled stockbot.timer 2>/dev/null || echo 'not installed')"
echo "   evening filings (21:30): $(systemctl is-enabled stockbot-evening.timer 2>/dev/null || echo 'not installed')"
echo "DONE. In Telegram, send /help to your bot."
