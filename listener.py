"""Talk to the bots from Telegram. Runs all the time (systemd: stockbot-listener).

Only YOUR chat (TELEGRAM_CHAT_ID) is answered; every other sender is ignored.

    /team <question>     the council: Claude opens, Grok and Gemini answer and challenge, Claude decides (live)
    /grok <question>     X (Twitter) search by Grok alone
    /gemini <question>   Google search by Gemini alone (send a PDF with this caption and Gemini reads it)
    /claude <question>   Claude answers from the bot's own data + your philosophy
    plain text           same as /claude (it remembers the last few messages)
    /talk [SYM]          read the bots' discussions
    /results [SYM]       quarterly results
    /analyse SYM  /gates SYM  /why SYM  /filings SYM  /board  /pause  /resume  /status  /help
"""
from __future__ import annotations

import base64
import collections
import json
import logging
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import requests

from bot import ai, telegram
from bot.config import DATA, PHILOSOPHY_FILE, REPORTS, env, load_config
from bot.lock import PAUSE, browser_busy
from bot.telegram import esc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout),
                              logging.FileHandler(DATA / "listener.log", encoding="utf-8")])
log = logging.getLogger("listener")
ROOT = Path(__file__).resolve().parent
OFFSET = DATA / "tg_offset"
MAX_Q = 4000
HISTORY: collections.deque = collections.deque(maxlen=10)  # Claude chat memory (this process only)
POOL = ThreadPoolExecutor(max_workers=3)

HELP = """🤖 <b>Talk to the bots</b>

<b>The council</b> (they talk to each other)
/team &lt;question&gt; — Claude opens, Grok (X) and Gemini (web) answer and challenge each other, then Claude decides. You see every turn live.
/talk — recent discussions · /talk SYMBOL — read one

<b>One bot</b>
/grok &lt;question&gt; — Grok searches X (Twitter)
/gemini &lt;question&gt; — Gemini searches Google (send a PDF with /gemini as caption: Gemini reads it)
/claude &lt;question&gt; — Claude answers from the bot's data + your philosophy
Plain text = /claude (it remembers the last few messages; /new to forget)

<b>Stocks</b>
/analyse SYMBOL — full study now: research, council, memo (Opus tokens)
/results — results filed recently · /results SYMBOL — one company
/gates SYMBOL — quality checklist (free)
/why SYMBOL — why the scan picked it (free)
/filings — today's important filings (the evening report) · /filings SYMBOL — one company
/observe — make the evening filing report now
/board — today's scan
/access — proof: what the bot read on Screener and MarketSmith today · /access SYMBOL — one company

<b>Control</b>
/pause · /resume — stop / start the 6 am run and the evening report
/status"""


# ------------------------------------------------------------------ telegram plumbing
def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/{method}"


def reply(text: str) -> None:
    """Send a formatted (HTML) message; callers escape any AI or user text with esc()."""
    telegram.send_html(text or "(empty answer)")


def download(file_id: str, max_mb: int = 20) -> bytes | None:
    r = requests.get(_api("getFile"), params={"file_id": file_id}, timeout=30).json()
    path = (r.get("result") or {}).get("file_path")
    if not path or (r["result"].get("file_size") or 0) > max_mb * 1024 * 1024:
        return None
    return requests.get(f"https://api.telegram.org/file/bot{env('TELEGRAM_BOT_TOKEN')}/{path}", timeout=120).content


# ------------------------------------------------------------------ single bots
ANSWER_RULE = ("You help Rajat, an investor in Indian small and mid-cap stocks. Answer directly and concisely. "
               "Give the source link for every fact. If you cannot find something, say so; never guess numbers. "
               "Plain text, no markdown.")


def ask_grok(q: str) -> str:
    return ai.grok(ANSWER_RULE + " Search X (Twitter) posts.", q)


def ask_gemini(q: str, pdf: bytes | None = None) -> str:
    return ai.gemini(ANSWER_RULE, q, pdf=pdf)


def _symbols_in(q: str) -> list[str]:
    words = {w.upper() for w in re.findall(r"[A-Za-z0-9&\-]{3,20}", q)}
    return [w for w in sorted(words) if list(REPORTS.glob(f"*_{w}.md")) or _deep(w)][:3]


def _deep(sym: str) -> dict | None:
    from hunt import db
    return db.doc_get(f"deep:{sym.upper()}", max_age_days=60)


def _latest(pattern: str) -> Path | None:
    files = sorted(REPORTS.glob(pattern))
    return files[-1] if files else None


def bot_context(q: str) -> str:
    parts = []
    board = _latest("*_hunt_board.txt")
    if board:
        parts.append(f"## LATEST SCAN ({board.name[:10]})\n{telegram.plain(board.read_text(encoding='utf-8'))[:5000]}")
    for s in _symbols_in(q):
        memo = _latest(f"*_{s}.md")
        if memo:
            parts.append(f"## LATEST MEMO ON {s} ({memo.name[:10]})\n{memo.read_text(encoding='utf-8')[:25000]}")
        d = _deep(s)
        if d and d.get("gate"):
            parts.append(f"## QUALITY GATES {s}\n{json.dumps(d['gate'], default=str)[:3000]}")
    memos = sorted({p.name[11:-3] for p in REPORTS.glob("*.md")})
    parts.append("## STOCKS WITH MEMOS\n" + ", ".join(memos[-80:]))
    return "\n\n".join(parts)


def ask_claude(q: str, pdf: bytes | None = None) -> str:
    phil = PHILOSOPHY_FILE.read_text(encoding="utf-8") if PHILOSOPHY_FILE.exists() else ""
    system = (f"# RAJAT'S INVESTING PHILOSOPHY\n{phil[:30000]}\n\n# YOUR JOB\nYou are the chat side of Rajat's research "
              f"bot. Answer from the BOT DATA below; say plainly when the data does not cover it. Be a sparring "
              f"partner, not a promoter. Plain text, no markdown. Short sentences, active voice (ASD-STE100).\n\n"
              f"# BOT DATA\n{bot_context(q)}")
    content: list = []
    if pdf:
        content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                      "data": base64.b64encode(pdf).decode()}})
    content.append({"type": "text", "text": q})
    text = ai.claude(system, list(HISTORY) + [{"role": "user", "content": content}], max_tokens=3000)
    HISTORY.append({"role": "user", "content": q})
    HISTORY.append({"role": "assistant", "content": text})
    return text


# ------------------------------------------------------------------ the council, live
def team(q: str) -> None:
    from bot import cards, council
    reply("🧑‍⚖️ <b>The council meets.</b> Claude opens, Grok and Gemini answer and challenge, then Claude "
          "decides. Each turn arrives here (2–5 min).")

    def live(d, turn):
        reply(cards.turn_message(turn))

    brief = "BOT DATA (use it if relevant):\n" + bot_context(q)[:8000]
    d = council.run("team", topic=q, brief=brief, facts=f"RAJAT'S QUESTION: {q}\n", cfg=load_config(), on_turn=live)
    try:
        council.decide_team(d, on_turn=live)
    except council.NoDiscussion as e:
        reply(f"⚪ <b>No decision.</b> Claude does not decide alone, and {esc(e)}.")
    reply(f"📁 Saved. Read it again: {cards.cmd('talk', cards.short_key(d))}")


def talk(arg: str) -> str:
    from bot import cards, council
    if not arg:
        return cards.discussion_list(council.recent(10))
    key = arg.strip()
    path = council.DIR / f"{key}.json"
    if path.exists():
        return cards.discussion_message(council.Discussion.load(path))
    by_key = [d for d in council.recent(60) if cards.short_key(d).upper() == key.upper()]
    if by_key:
        return cards.discussion_message(by_key[0])
    found = council.recent(1, sym=key)
    if not found:
        return f"No discussion found for {esc(key)}. Send /talk to see the list."
    return cards.discussion_message(found[0])


# ------------------------------------------------------------------ bot jobs
def run_job(args: list[str], label: str) -> str:
    busy = browser_busy()
    subprocess.Popen([sys.executable, "run_daily.py", *args, "--now"], cwd=ROOT,
                     stdout=open(DATA / "listener_jobs.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
    return (f"⏳ {esc(label)}: queued. Another job uses the browser now; it starts when that job ends." if busy
            else f"⏳ {esc(label)}: started. The result comes here in a few minutes.")


def why(sym: str) -> str:
    from hunt import runner, scoring
    d = _deep(sym)
    rows = scoring.score_all(load_config(), date.today(), {sym}, confirm={sym: d} if d else {})
    r = next((x for x in rows if x["slug"] == sym), None)
    if not r:
        return f"{esc(sym)}: the scan has no signals for it in the last 2 years."
    lines = [f"🔎 <b>Why the scan picked {esc(r['name'])}</b> ({esc(sym)})" + (" · <b>removed</b>" if r["excluded"] else "")]
    lines += [f"{c['points']:+.1f}  {esc(runner.plain_label(c['label']))}" for c in r["components"][:12]]
    if r.get("gate"):
        lines.append(f"Quality checks: {esc(r['gate'])}" + (f" — {esc('; '.join(r['gate_reasons']))}" if r.get("gate_reasons") else ""))
    if r["kill_reasons"]:
        lines.append("Removed because: " + esc("; ".join(r["kill_reasons"])))
    return "\n".join(lines)


def filings_of(sym: str) -> str:
    from hunt import db
    rows = db.filings_for(sym, (date.today() - timedelta(days=180)).isoformat())
    if not rows:
        return f"{esc(sym)}: no filings stored in the last 180 days."
    out = [f"📄 <b>Filings — {esc(sym)}</b> (last 180 days)"]
    for r in sorted(rows, key=lambda x: x["dt"], reverse=True)[:12]:
        tags = ", ".join(json.loads(r["tags"] or "[]")) or "no tag"
        out.append(f"{esc(r['dt'])} · {esc(tags)}\n  {esc(r['headline'][:160])}\n  " + telegram.link(r["url"], "PDF"))
    return "\n".join(out)


def results_cmd(arg: str) -> str:
    from hunt import db, results
    if not arg:
        return results.list_recent(3)
    sym = arg.split()[0].upper()
    r = db.latest_result(sym)
    if r and (r.get("data") or {}).get("q") and r["filed"] >= (date.today() - timedelta(days=10)).isoformat():
        return results.block(r)
    return run_job(["--results", sym], f"latest result of {sym}")


def status() -> str:
    board = _latest("*_hunt_board.txt")
    return (f"⚙️ <b>Status</b>\n6 am run: {'⏸ PAUSED' if PAUSE.exists() else '▶️ active'}\n"
            f"Browser: {'busy (a job is running)' if browser_busy() else 'free'}\n"
            f"Latest scan: {esc(board.name[:10]) if board else 'none'}\n"
            f"Chat model: {esc(ai.chat_model())} · memo model: {esc(ai.thesis_model())}")


# ------------------------------------------------------------------ dispatch
def handle(msg: dict) -> None:
    text = (msg.get("text") or msg.get("caption") or "").strip()
    doc = msg.get("document")
    # '/talk ACGL' and the one-tap form '/talk_ACGL' mean the same
    m = re.match(r"^/([A-Za-z]+)(?:_([A-Za-z0-9_]+))?(?:@\w+)?\s*(.*)$", text, re.S)
    cmd, arg = (m.group(1).lower(), (m.group(2) or m.group(3)).strip()) if m else ("claude", text)
    arg = arg[:MAX_Q]
    sym = arg.split()[0].upper() if arg else ""
    pdf = None
    if doc:
        if doc.get("mime_type") != "application/pdf":
            return reply("I read only PDF files.")
        pdf = download(doc["file_id"])
        if pdf is None:
            return reply("The PDF is too large (Telegram bots can download max 20 MB).")
        if not m:
            cmd = "gemini"
        arg = arg or "Summarise this document for an investor: business, numbers, guidance, red flags."
    try:
        if cmd in ("start", "help"):
            return reply(HELP)
        if cmd == "new":
            HISTORY.clear()
            return reply("🧹 Claude chat memory cleared.")
        if cmd in ("grok", "gemini", "claude", "team") and not arg:
            return reply(f"Write the question after /{cmd}.")
        if cmd == "grok":
            reply("⏳ Grok is searching X…")
            return reply("🐦 <b>Grok</b>\n" + esc(ask_grok(arg)))
        if cmd == "gemini":
            reply("⏳ Gemini is " + ("reading the PDF…" if pdf else "searching Google…"))
            return reply("🔷 <b>Gemini</b>\n" + esc(ask_gemini(arg, pdf)))
        if cmd == "claude":
            return reply("🟠 <b>Claude</b>\n" + esc(ask_claude(arg, pdf=pdf)))
        if cmd == "team":
            return team(arg)
        if cmd == "talk":
            return reply(talk(arg))
        if cmd == "access":
            from bot import cards
            return reply(cards.access_message(sym or None))
        if cmd in ("results", "result"):
            return reply(results_cmd(arg))
        if cmd in ("analyse", "analyze"):
            return reply(run_job(["--symbols", sym], f"full study of {sym}") if sym else "Use: /analyse SYMBOL")
        if cmd == "gates":
            if not sym:
                return reply("Use: /gates SYMBOL")
            d = _deep(sym)
            if d and d.get("gate") and d["gate"].get("checks"):
                from hunt import gates
                return reply(esc(gates.report(sym, sym, d["gate"])) + "\n<i>(from the last check, max 60 days old)</i>")
            return reply(run_job(["--gates", sym], f"quality check of {sym}"))
        if cmd == "why":
            return reply(why(sym) if sym else "Use: /why SYMBOL")
        if cmd == "filings":
            if sym:
                return reply(filings_of(sym))
            f = _latest("*_filings.txt")
            return reply(f.read_text(encoding="utf-8") if f else "No evening filing report yet. Send /observe to make one now.")
        if cmd == "observe":
            return reply(run_job(["--evening"], "evening filing report"))
        if cmd == "board":
            b = _latest("*_hunt_board.txt")
            return reply(b.read_text(encoding="utf-8") if b else "No scan yet.")
        if cmd == "pause":
            PAUSE.touch()
            return reply("⏸ The 6 am run and the evening filing report are paused. Send /resume to start them again.")
        if cmd == "resume":
            PAUSE.unlink(missing_ok=True)
            return reply("▶️ The 6 am run and the evening filing report are active again.")
        if cmd == "status":
            return reply(status())
        return reply(f"I do not know /{esc(cmd)}. Send /help.")
    except Exception as e:
        log.exception("command %s failed", cmd)
        reply(f"❌ /{esc(cmd)} failed: {esc(telegram._safe(e)[:300])}")


def main() -> None:
    me = str(env("TELEGRAM_CHAT_ID") or "")
    if not (env("TELEGRAM_BOT_TOKEN") and me):
        sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be in .env")
    offset = int(OFFSET.read_text()) if OFFSET.exists() else 0
    log.info("listener started")
    while True:
        try:
            r = requests.get(_api("getUpdates"), params={"offset": offset, "timeout": 50,
                                                          "allowed_updates": json.dumps(["message"])}, timeout=70).json()
            if not r.get("ok"):
                log.warning("getUpdates: %s", str(r)[:200])
                time.sleep(15)
                continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                OFFSET.write_text(str(offset))
                msg = u.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != me:
                    log.warning("ignored a message from chat %s", (msg.get("chat") or {}).get("id"))
                    continue
                POOL.submit(handle, msg)
        except Exception as e:
            log.warning("poll error: %s", telegram._safe(e))
            time.sleep(10)


if __name__ == "__main__":
    main()
