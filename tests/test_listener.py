"""Offline test of the Telegram listener: no network, no keys. Run: python -m tests.test_listener"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hunt.db as hdb  # noqa: E402

hdb.DB = Path(tempfile.mkdtemp()) / "t.db"
from bot import ai, council  # noqa: E402

council.DIR = Path(tempfile.mkdtemp())
import listener as L  # noqa: E402
from tests.htmlcheck import check_html  # noqa: E402

sent, jobs = [], []


def fake_reply(t):
    check_html(t)  # every reply must be valid Telegram HTML
    sent.append(t)


L.reply = fake_reply
L.ask_grok = lambda q: f"grok says about {q} <b>not a tag</b>"
L.ask_gemini = lambda q, pdf=None: f"gemini says about {q} pdf={bool(pdf)}"
L.ask_claude = lambda q, pdf=None: f"claude answers: {q[:20]}"
L.run_job = lambda args, label: jobs.append(args) or f"started {label}"
L.download = lambda fid: b"%PDF-1.4"
L.PAUSE = Path(tempfile.mkdtemp()) / "paused"


def fake_claude(system, messages, model=None, max_tokens=2000):
    c = messages if isinstance(messages, str) else str(messages[-1]["content"])
    if "OPENING" in c:
        return "First view: rising.\nQ-GROK-1: what do shippers say?\nQ-GEMINI-1: what do the numbers say?"
    if "REACTION" in c:
        return "Grok helps; Gemini helps more.\nQ-GROK: none\nQ-GEMINI: none"
    return "DECISION: Yes, demand is rising.\nAccepted from Grok and Gemini.\nCONFIDENCE: medium"


ai.claude = fake_claude
ai.grok = lambda s, u, search=True: "Shippers on X are bullish (https://x.com/1).\nPOSITION: rising"
ai.gemini = lambda s, u, pdf=None, search=True: "Port data shows +12% (https://example.com).\nPOSITION: rising"


def last():
    return sent[-1]


L.handle({"text": "/grok cohance ceo"})
assert last().startswith("🐦 <b>Grok</b>") and "cohance ceo" in last() and "&lt;b&gt;not a tag" in last()
L.handle({"text": "/gemini LPG demand"})
assert "gemini says about LPG demand" in last()
L.handle({"text": "is TAC any good?"})
assert last().startswith("🟠 <b>Claude</b>")
L.handle({"caption": "", "document": {"mime_type": "application/pdf", "file_id": "x"}})
assert "pdf=True" in last()

n = len(sent)
L.handle({"text": "/team Is LPG demand in India rising?"})
team_msgs = sent[n:]
print("\n---\n".join(team_msgs))
assert team_msgs[0].startswith("🧑‍⚖️") and any("🐦 <b>Grok</b> · answer" in m for m in team_msgs)
assert any("🟠 <b>Claude</b> · decision" in m for m in team_msgs) and "/talk" in team_msgs[-1]
L.handle({"text": "/talk"})
assert "Recent discussions" in last()
from bot import cards  # noqa: E402
key = cards.short_key(council.recent(1)[0])
L.handle({"text": f"/talk_{key}"})  # the one-tap form
assert "🔷 <b>Gemini</b> · final answer" in last()
assert f"/talk_{key}" in team_msgs[-1]

L.handle({"text": "/analyse tac"})
assert jobs[-1] == ["--symbols", "TAC"]
L.handle({"text": "/gates KALIND"})
assert jobs[-1] == ["--gates", "KALIND"]
L.handle({"text": "/results FOO"})
assert jobs[-1] == ["--results", "FOO"]
L.handle({"text": "/filings"})
assert "No evening filing report yet" in last()
L.handle({"text": "/observe"})
assert jobs[-1] == ["--evening"]
L.handle({"text": "/results"})
assert "No quarterly results" in last()
L.handle({"text": "/pause"})
assert L.PAUSE.exists()
L.handle({"text": "/resume"})
assert not L.PAUSE.exists()
L.handle({"text": "/why NOPE"})
assert "no signals" in last()
L.handle({"text": "/help"})
assert "/team" in last() and "&lt;question&gt;" in last()
L.handle({"text": "/grok"})
assert "Write the question" in last()
L.handle({"text": "/xyz"})
assert "/help" in last()
print("\nALL LISTENER TESTS PASSED")
