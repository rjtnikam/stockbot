"""Sends memos, charts and alerts to your Telegram."""
from __future__ import annotations

import html as _html
import logging
import re

import requests

from .config import env

log = logging.getLogger(__name__)


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/{method}"


def _safe(e: Exception) -> str:
    """Error text without the bot token (it appears inside request URLs)."""
    tok = env("TELEGRAM_BOT_TOKEN") or "<none>"
    return str(e).replace(tok, "<token>")


def enabled() -> bool:
    return bool(env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"))


def send(text: str) -> None:
    if not enabled():
        print(text)
        return
    for i in range(0, len(text), 3900):  # Telegram limit is 4096 chars
        try:
            requests.post(_api("sendMessage"), data={"chat_id": env("TELEGRAM_CHAT_ID"),
                          "text": text[i:i + 3900], "disable_web_page_preview": True}, timeout=30)
        except Exception as e:
            log.warning("telegram send failed: %s", _safe(e))


def send_file(path: str, caption: str = "", photo: bool = False) -> None:
    if not enabled() or not path:
        return
    method, field = ("sendPhoto", "photo") if photo else ("sendDocument", "document")
    try:
        with open(path, "rb") as f:
            requests.post(_api(method), data={"chat_id": env("TELEGRAM_CHAT_ID"), "caption": caption[:1000]},
                          files={field: f}, timeout=60)
    except Exception as e:
        log.warning("telegram file failed: %s", _safe(e))


# ------------------------------------------------------------------ clean (HTML) messages
LIMIT = 3800
_TAG = re.compile(r"</?(b|i|u|s|a|code|pre|blockquote)(\s[^>]*)?>", re.I)


def esc(x) -> str:
    """Escape text for Telegram HTML (company names, AI text, numbers)."""
    return _html.escape("" if x is None else str(x), quote=False)


def link(url: str | None, text: str) -> str:
    if not url or not str(url).startswith("http"):
        return esc(text)
    return f'<a href="{_html.escape(str(url), quote=True)}">{esc(text)}</a>'


def plain(text: str) -> str:
    """HTML message -> plain text (fallback when Telegram rejects the HTML)."""
    return _html.unescape(_TAG.sub("", text or ""))


def _chunks(text: str, limit: int = LIMIT) -> list[str]:
    """Split on blank lines so a tag never spans two messages (our HTML never spans paragraphs)."""
    out, cur = [], ""
    for block in (text or "").split("\n\n"):
        while len(block) > limit:  # one huge block: split on lines, then hard-cut as plain text
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > limit // 3 else limit
            piece, block = block[:cut], block[cut:].lstrip("\n")
            if cur:
                out.append(cur)
                cur = ""
            out.append(piece if piece.count("<") == piece.count(">") else esc(plain(piece)))
        if cur and len(cur) + len(block) + 2 > limit:
            out.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        out.append(cur)
    return [c for c in out if c.strip()]


def send_html(text: str) -> None:
    """Formatted message (bold, links, tables in <pre>). Falls back to plain text if Telegram refuses it."""
    if not enabled():
        print(plain(text))
        return
    for chunk in _chunks(text):
        try:
            r = requests.post(_api("sendMessage"), data={"chat_id": env("TELEGRAM_CHAT_ID"), "text": chunk,
                              "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=30)
            if r.status_code != 200:
                log.warning("telegram HTML refused (%s); sending plain text", r.text[:200])
                requests.post(_api("sendMessage"), data={"chat_id": env("TELEGRAM_CHAT_ID"), "text": plain(chunk)[:4000],
                              "disable_web_page_preview": True}, timeout=30)
        except Exception as e:
            log.warning("telegram send failed: %s", _safe(e))
