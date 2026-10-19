"""One place for the three AIs. The council, the listener and the reports all call these.

    claude(system, messages)   Anthropic (model from .env; the chair and the decider)
    grok(system, user)         xAI Grok with live X (Twitter) search
    gemini(system, user)       Google Gemini with live Google search (can also read a PDF)

Tests replace these functions, so no test ever needs an API key.
"""
from __future__ import annotations

import logging

from .config import env

log = logging.getLogger(__name__)


def chat_model() -> str:
    return env("CLAUDE_CHAT_MODEL", "claude-sonnet-5-5")


def thesis_model() -> str:
    return env("CLAUDE_MODEL", "claude-opus-5-5")


def claude(system: str, messages: list[dict] | str, model: str | None = None, max_tokens: int = 2000) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    msg = client.messages.create(model=model or chat_model(), max_tokens=max_tokens, system=system,
                                 messages=messages)
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


def grok(system: str, user: str, search: bool = True) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=env("XAI_API_KEY"), base_url="https://api.x.ai/v1")
    kw = {"tools": [{"type": "x_search"}]} if search else {}
    resp = client.responses.create(model=env("GROK_MODEL", "grok-4.3"),
                                   input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                                   **kw)
    text = (getattr(resp, "output_text", "") or "").strip()
    cites = [str(c) for c in (getattr(resp, "citations", None) or [])][:8]
    if cites and "http" not in text:
        text += "\n\nSources:\n" + "\n".join(cites)
    return text


def gemini(system: str, user: str, pdf: bytes | None = None, search: bool = True) -> str:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=env("GEMINI_API_KEY"))  # keep a reference: the SDK closes unreferenced clients
    parts: list = [types.Part.from_bytes(data=pdf, mime_type="application/pdf")] if pdf else []
    parts.append(user)
    config = types.GenerateContentConfig(
        system_instruction=system or None, temperature=0.2,
        tools=[types.Tool(google_search=types.GoogleSearch())] if search else None)
    resp = client.models.generate_content(model=env("GEMINI_MODEL", "gemini-3.5-flash"), contents=parts, config=config)
    text = (resp.text or "").strip()
    links = []
    try:
        for ch in (resp.candidates[0].grounding_metadata.grounding_chunks or []):
            if ch.web:
                links.append(f"{ch.web.title}: {ch.web.uri}")
    except Exception:
        pass
    if links and "http" not in text:
        text += "\n\nSources:\n" + "\n".join(links[:8])
    return text
