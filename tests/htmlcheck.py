"""Checks a Telegram HTML message the way Telegram does: few tags, balanced in every chunk, '<' escaped."""
import re

from bot import telegram

ALLOWED = {"b", "i", "u", "s", "a", "code", "pre", "blockquote"}
TAG = re.compile(r"<(/?)([a-zA-Z]+)[^>]*>")


def check_html(msg: str) -> None:
    for chunk in telegram._chunks(msg):
        stack = []
        for m in TAG.finditer(chunk):
            closing, tag = m.group(1) == "/", m.group(2).lower()
            assert tag in ALLOWED, f"tag <{tag}> not allowed in Telegram: {chunk[:200]}"
            if closing:
                assert stack and stack[-1] == tag, f"unbalanced </{tag}> in: {chunk[:300]}"
                stack.pop()
            else:
                stack.append(tag)
        assert not stack, f"unclosed {stack} in: {chunk[:300]}"
        assert "<" not in TAG.sub("", chunk), "raw '<' must be escaped"
