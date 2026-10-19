"""Health check: tells you, in plain words, what works and what to fix next.

    .venv/bin/python doctor.py            # full check (sends a hello to your Telegram)
    .venv/bin/python doctor.py --quick    # skip the browser and network checks

It never prints your keys: only their first few characters, so you can spot a swap.
"""
from __future__ import annotations

import importlib
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS: list[tuple[str, str, str]] = []  # (status, what, fix)


def ok(what):
    RESULTS.append(("OK", what, ""))
    print(f"[ OK ] {what}")


def warn(what, fix):
    RESULTS.append(("WARN", what, fix))
    print(f"[WARN] {what}\n       -> {fix}")


def fail(what, fix):
    RESULTS.append(("FAIL", what, fix))
    print(f"[FAIL] {what}\n       -> {fix}")


def mask(v: str) -> str:
    return f"{v[:6]}... ({len(v)} characters)"


def check_system():
    v = sys.version_info
    (ok if v >= (3, 9) else lambda w: fail(w, "Python 3.9+ needed: tell Claude 'doctor says old Python'"))(
        f"Python {v.major}.{v.minor}")
    if not (ROOT / "run_daily.py").exists():
        fail("not inside the stockbot folder", "type: cd ~/stockbot   then run doctor again")
    mem = swap = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                mem = int(line.split()[1]) / 1048576
            if line.startswith("SwapTotal"):
                swap = int(line.split()[1]) / 1048576
        if mem + swap >= 1.9:
            ok(f"memory {mem:.1f} GB + swap {swap:.1f} GB")
        else:
            warn(f"memory {mem:.1f} GB + swap {swap:.1f} GB is tight for the invisible Chrome",
                 "add 2 GB of swap (guide Step 2e)")
    except OSError:
        pass
    free = shutil.disk_usage(ROOT).free / 1e9
    (ok(f"disk space free: {free:.1f} GB") if free >= 2 else
     warn(f"only {free:.1f} GB disk free", "delete old files, or increase the Lightsail disk"))


def check_packages():
    missing = []
    for mod in ("anthropic", "openai", "google.genai", "playwright", "pandas", "yfinance", "bs4", "dotenv",
                "yaml", "matplotlib", "lxml", "requests"):
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(mod)
    if missing:
        fail(f"Python packages missing: {', '.join(missing)}",
             "run doctor with .venv/bin/python (not plain python); if still failing: bash deploy/setup.sh")
    else:
        ok("all Python packages installed")


EXPECT = {
    "ANTHROPIC_API_KEY": (r"^sk-ant-", "should start with sk-ant-"),
    "GEMINI_API_KEY": (r"^(AIza|AQ\.)", "should start with AIza or AQ."),
    "XAI_API_KEY": (r"^xai-", "should start with xai-"),
    "TELEGRAM_BOT_TOKEN": (r"^\d{6,}:[\w-]{20,}$", "should look like 7123456789:AAH..."),
    "TELEGRAM_CHAT_ID": (r"^-?\d{5,}$", "should be a number like 512345678"),
}


def _perm_ok(p: Path) -> bool:
    return not (p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def check_env() -> dict:
    env_file = ROOT / ".env"
    if not env_file.exists():
        fail(".env file missing", "run: cp .env.example .env   then guide Step 4")
        return {}
    from dotenv import dotenv_values
    vals = {k: (v or "").strip() for k, v in dotenv_values(env_file).items()}
    for key, (rx, hint) in EXPECT.items():
        v = vals.get(key, "")
        if not v:
            fail(f"{key} is empty", "nano .env and paste it (guide Step 4)")
        elif v.startswith(("'", '"')) or " " in v:
            fail(f"{key} has quotes or spaces", "nano .env: the value goes right after = with no quotes or spaces")
        elif not re.search(rx, v):
            fail(f"{key} looks wrong: {mask(v)}", f"it {hint}; did two keys get swapped?")
        else:
            ok(f"{key} present: {mask(v)}")
    if not _perm_ok(env_file):
        warn(".env can be read by other users", "run: chmod 600 .env")
    return vals


def check_session():
    s = ROOT / "session.json"
    if not s.exists():
        warn("session.json missing (the one-time login)", "do guide Step 5 on your laptop")
        return False
    ok("session.json present")
    if not _perm_ok(s):
        warn("session.json can be read by other users", "run: chmod 600 session.json")
    return True


def check_telegram(vals: dict):
    import requests
    tok, chat = vals.get("TELEGRAM_BOT_TOKEN"), vals.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          data={"chat_id": chat, "text": "Stockbot doctor: hello! Telegram works."}, timeout=20)
        d = r.json()
    except Exception as e:
        fail(f"Telegram unreachable: {str(e).replace(tok, '<token>')[:160]}", "check the server's internet, then retry")
        return
    if d.get("ok"):
        ok("Telegram: hello message sent (check your phone)")
    elif "chat not found" in str(d.get("description", "")).lower():
        fail("Telegram: chat not found", "open YOUR bot in Telegram, press Start, send hi; re-check TELEGRAM_CHAT_ID")
    elif r.status_code == 401:
        fail("Telegram: token rejected", "re-copy the token from @BotFather into .env")
    else:
        fail(f"Telegram: {d.get('description')}", "re-check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")


def _key_problem(name: str, e: Exception, where: str):
    msg = str(e)
    if re.search(r"401|403|invalid|authenticat|permission|API key not valid|incorrect api key", msg, re.I):
        fail(f"{name} key rejected: {msg[:120]}", f"re-copy it from {where} into .env (nano .env)")
    elif re.search(r"credit|billing|quota|429|insufficient", msg, re.I):
        fail(f"{name} key works but has no credit/quota: {msg[:120]}", f"add credit at {where}")
    else:
        warn(f"couldn't reach {name}: {msg[:120]}", "check the server's internet and run doctor again in a minute")


def check_ai_keys(vals: dict):
    """Listing models is free: proves each key works without spending credit."""
    if vals.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            anthropic.Anthropic(api_key=vals["ANTHROPIC_API_KEY"]).models.list(limit=1)
            ok("Anthropic key accepted")
        except Exception as e:
            _key_problem("Anthropic", e, "console.anthropic.com")
    if vals.get("GEMINI_API_KEY"):
        try:
            from google import genai
            client = genai.Client(api_key=vals["GEMINI_API_KEY"])  # keep a reference: the list is fetched lazily
            next(iter(client.models.list()))
            ok("Gemini key accepted")
        except Exception as e:
            _key_problem("Gemini", e, "aistudio.google.com")
    if vals.get("XAI_API_KEY"):
        try:
            from openai import OpenAI
            OpenAI(api_key=vals["XAI_API_KEY"], base_url="https://api.x.ai/v1").models.list()
            ok("xAI key accepted")
        except Exception as e:
            _key_problem("xAI", e, "console.x.ai")


def check_browser(has_session: bool):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        ok("invisible Chrome starts")
    except Exception as e:
        fail(f"invisible Chrome won't start: {str(e)[:160]}",
             "run: .venv/bin/playwright install chromium   (and add swap if memory is low)")
        return
    if not has_session:
        return
    try:
        sys.path.insert(0, str(ROOT))
        from bot.session import browser_session, screener_logged_in
        with browser_session((1, 2)) as br:
            (ok("Screener: logged in as you") if screener_logged_in(br) else
             fail("Screener: NOT logged in", "redo guide Step 5 (login_local.py + send session.json)"))
    except Exception as e:
        fail(f"Screener check failed: {str(e)[:160]}", "redo guide Step 5")


def check_timer():
    try:
        en = subprocess.run(["systemctl", "is-enabled", "stockbot.timer"], capture_output=True, text=True).stdout.strip()
    except FileNotFoundError:
        return
    if en == "enabled":
        nxt = subprocess.run(["systemctl", "list-timers", "stockbot.timer", "--no-pager", "--no-legend"],
                             capture_output=True, text=True).stdout.strip()
        ok("6 am schedule is ON" + (f" (next: {' '.join(nxt.split()[:4])})" if nxt else ""))
    else:
        warn("6 am schedule is OFF", "run: sudo systemctl enable --now stockbot.timer")


def main():
    quick = "--quick" in sys.argv
    os.chdir(ROOT)
    print("Stockbot doctor\n")
    check_system()
    check_packages()
    vals = check_env()
    has_session = check_session()
    if not quick:
        check_telegram(vals)
        check_ai_keys(vals)
        check_browser(has_session)
    check_timer()
    n_fail = sum(1 for s, *_ in RESULTS if s == "FAIL")
    n_warn = sum(1 for s, *_ in RESULTS if s == "WARN")
    print()
    if n_fail == 0 and n_warn == 0:
        print("ALL GOOD. Next: the tests in guide Step 6.")
    else:
        first = next((r for r in RESULTS if r[0] == "FAIL"), None) or next(r for r in RESULTS if r[0] == "WARN")
        print(f"{n_fail} to fix, {n_warn} warnings. Fix this first: {first[1]} -> {first[2]}")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
