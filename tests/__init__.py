"""Tests never touch real data or send real messages: temp data folder, no Telegram, no API keys."""
import os
import tempfile

os.environ["STOCKBOT_DATA"] = tempfile.mkdtemp(prefix="stockbot_test_")
for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY"):
    os.environ[_k] = ""  # an empty value stops .env from filling it in
