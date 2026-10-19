"""Offline test of the council, the clean card and the memo file. No network, no API keys.

Run: python -m tests.test_council
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hunt.db as hdb  # noqa: E402

hdb.DB = Path(tempfile.mkdtemp()) / "test.db"
import bot.store as bstore  # noqa: E402

bstore.DB = hdb.DB
from bot import ai, cards, council, fundamentals, prices, screener, telegram  # noqa: E402
from bot.config import load_config  # noqa: E402

council.DIR = Path(tempfile.mkdtemp())  # never touch real discussions
from tests.htmlcheck import check_html  # noqa: E402
from tests.test_offline import fake_html, fake_prices  # noqa: E402

CALLS: list[str] = []
MODE = {"members_fail": False}


def _content(messages) -> str:
    if isinstance(messages, str):
        return messages
    last = messages[-1]["content"]
    return last if isinstance(last, str) else " ".join(p.get("text", "") for p in last if isinstance(p, dict))


def fake_claude(system, messages, model=None, max_tokens=2000):
    c = _content(messages)
    if "Write your OPENING" in c:
        CALLS.append("claude:opening")
        return ("DRAFT: WATCH\nFOR: sales +27% YoY; ROCE 24%.\nAGAINST: P/E rich; margins flat at 15%.\n"
                "Q-GROK-1: Do customers talk about new orders?\nQ-GROK-2: Any promoter selling chatter?\n"
                "Q-GEMINI-1: Is the new plant on time?\nQ-GEMINI-2: Any competitor adding capacity?")
    if "Write your REACTION" in c:
        CALLS.append("claude:reaction")
        return "GROK's order chatter is weak evidence. GEMINI's plant update changes my view.\nQ-GROK: none\nQ-GEMINI: When does the plant reach 50% use?"
    if "DOSSIER (JSON)" in c:
        CALLS.append("claude:thesis")
        assert "THE DISCUSSION" in c and "GROK (answer)" in c and "GEMINI (answer)" in c, "decision without the transcript"
        return ("## Verdict\nWATCH, conviction 3/5. Good business, price ahead.\n## 1. The story\nA <precision> maker & more.\n"
                "| rule | status |\n|---|---|\n| CFO/PAT | pass |\n## 9. The discussion\nGrok: WATCH. Gemini: PURSUE.\n"
                'CARD_JSON: {"verdict": "WATCH", "conviction": 3, "stage": "Inflection", "price_vs_story": "price ahead of story", '
                '"what_it_does": "Precision parts for auto & rail", "story": "New plant doubled capacity. Sales grew 27% in the latest quarter.", '
                '"where_it_stands": "Turning point. The price expects 49% growth a year; the business did 0%.", '
                '"main_risk": "The price already expects much more growth than delivered.", '
                '"watch_next": ["Q3 result around 14 Feb: margin above 15%?", "Plant use above 50%"], '
                '"decision_reason": "Gemini showed the plant is on time, but the price is ahead.", '
                '"discussion": {"accepted": [{"from": "GEMINI", "point": "plant on time"}], '
                '"rejected": [{"from": "GROK", "point": "order chatter", "why": "anonymous accounts"}], "overruled_majority": false}}')
    if "Write the DECISION" in c:
        CALLS.append("claude:decision")
        return "DECISION: Wait for the Q3 result.\nACCEPTED from Grok: chatter is retail. ACCEPTED from Gemini: plant on time.\nCONFIDENCE: medium"
    raise AssertionError("unexpected Claude prompt: " + c[:120])


def fake_grok(system, user, search=True):
    if MODE["members_fail"]:
        raise RuntimeError("429 RESOURCE_EXHAUSTED")
    assert "DISCUSSION SO FAR" in user and "CLAUDE (opening)" in user, "Grok must see the chair's opening"
    if "FINAL turn" in user:
        CALLS.append("grok:final")
        return "No more evidence on X.\nVOTE: WATCH — X chatter is mostly retail tips (https://x.com/a/status/1)."
    CALLS.append("grok:answer")
    return "On X, two dealers mention new orders (https://x.com/b/status/2). No promoter selling talk found."


def fake_gemini(system, user, pdf=None, search=True):
    if MODE["members_fail"]:
        raise RuntimeError("503 UNAVAILABLE")
    assert "GROK (answer)" in user, "Gemini must see Grok's answer (they talk to each other)"
    if "FINAL turn" in user:
        CALLS.append("gemini:final")
        return "The plant reaches 50% use by March per the company (https://example.com/ppt).\nPOSITION: n/a\nVOTE: PURSUE — capacity is real and on time."
    CALLS.append("gemini:answer")
    return "The new plant started on time (https://example.com/news). GROK's dealer posts: I found no confirmation."


ai.claude, ai.grok, ai.gemini = fake_claude, fake_grok, fake_gemini


class FakePage:
    url = ""


class FakeBr:
    page = FakePage()

    def goto(self, url, wait="domcontentloaded"):
        return "<html></html>"


def research_fakes():
    from bot import research_gemini, research_grok, valuepickr
    research_grok.x_research = lambda name, sym, days: {"mention_level": "low", "mention_trend": "rising",
                                                        "who_is_talking": "serious small-cap investors",
                                                        "bull_narratives": [{"point": "new plant", "url": "https://x.com/c/1"}]}
    research_gemini.web_research = lambda name, sym, days: {"story_now": "Capacity doubled.", "recent_news": []}
    research_gemini.digest_valuepickr = lambda name, vp: {"found": False}
    valuepickr.fetch = lambda *a, **k: {"found": False}


def analyse(cfg):
    import run_daily
    company = screener.parse_company_html(fake_html())
    company.update(url="test", basis="consolidated", codes={"nse": "TESTP"})
    numbers = fundamentals.build(company, cfg)
    hunt = {"company": company, "numbers": numbers, "screener_slug": "TESTP", "score": 9.0, "setups": ["Earnings inflection"],
            "stage_hint": "Inflection", "components": [{"label": "numbers: revenue jump", "points": 2.0, "url": None}],
            "red_flags": [], "deep_check": {"gate": {"status": "pass", "passed": 12, "total": 13}}, "documents": None}
    prices.stock_history = lambda s, y=6, codes=None: fake_prices(0.0012, 3)
    return run_daily.analyse_stock(FakeBr(), "TESTP", "hunt", cfg, fake_prices(0.0004, 2), {}, False, hunt=hunt)


def main():
    cfg = load_config()
    research_fakes()

    # 1. full stock flow: the council talks, then Claude decides with the transcript
    r = analyse(cfg)
    order = [c for c in CALLS if c != "claude:fix"]
    print("turn order:", order)
    assert order == ["claude:opening", "grok:answer", "gemini:answer", "claude:reaction", "grok:final",
                     "gemini:final", "claude:thesis"], order
    d = r["discussion"]
    assert [t.stage for t in d.turns][-1] == "decision" and d.status == "decided"
    assert d.votes() == {"grok": {"vote": "WATCH", "reason": "X chatter is mostly retail tips (https://x.com/a/status/1)."},
                         "gemini": {"vote": "PURSUE", "reason": "capacity is real and on time."}}, d.votes()
    assert r["card"]["verdict"] == "WATCH" and r["card"]["discussion_checked"] is True
    card = cards.stock_card(r["name"], r["sym"], r["numbers"], r["card"], d, stage=r["stage"]["stage"], sources=r["sources"])
    print("\n" + telegram.plain(card))
    check_html(card)
    for must in ("🟡 <b>WATCH</b> · conviction 3/5", "📖 <b>The story</b>", "<pre>Latest quarter (Jun 2026)",
                 "🐦 Grok: <b>WATCH</b>", "🔷 Gemini: <b>PURSUE</b>", "🟠 Claude decided: <b>WATCH</b>",
                 "👀 <b>Watch next</b>", "/talk_TESTP", "Precision parts for auto &amp; rail"):
        assert must in card, must
    assert "hunt" not in card.lower() and "DRY" not in card and "**" not in card, "internal words leaked into the card"
    assert "🔐 <b>Data read</b>: Screener ✅" in card and "MarketSmith ❌" in card, "the card must prove what was read"
    assert "Data sources" in Path(r["html"]).read_text(encoding="utf-8")
    html_file = Path(r["html"]).read_text(encoding="utf-8")
    assert "The discussion" in html_file and "🔷 Gemini · final answer" in html_file
    assert "&lt;precision&gt;" in html_file and "<table>" in html_file, "memo markdown must become safe HTML"
    folded = cards.discussion_folded(d)
    print("\n" + folded[:900])
    check_html(folded)
    assert folded.count("<blockquote expandable>") == 7 and "🐦 Grok WATCH" in folded and "Claude WATCH" in folded
    assert len(telegram._chunks(folded)) <= 2, "the morning discussion must stay short: max 2 messages"
    talk = cards.discussion_message(d)
    check_html(talk)
    assert "🐦 <b>Grok</b> · final answer" in talk

    # 2. the rule: no discussion -> no decision (Claude never decides alone)
    CALLS.clear()
    MODE["members_fail"] = True
    r2 = analyse(cfg)
    print("\nno-discussion calls:", CALLS)
    assert "claude:thesis" not in CALLS, "Claude decided without a discussion"
    assert r2["card"]["verdict"] == "NO DECISION"
    card2 = cards.stock_card(r2["name"], r2["sym"], r2["numbers"], r2["card"], r2["discussion"])
    check_html(card2)
    check_html(cards.discussion_folded(r2["discussion"]))
    assert "⚪ <b>NO DECISION</b>" in card2 and "absent (quota used up, error 429)" in card2, card2
    MODE["members_fail"] = False

    # 3. /team: the council on a free question, every turn live, then Claude's decision
    CALLS.clear()
    live = []
    dt = council.run("team", topic="Is LPG shipping demand in India rising?", brief="", facts="RAJAT'S QUESTION: ...\n",
                     cfg=cfg, on_turn=lambda d, t: live.append(t.speaker + ":" + t.stage))
    council.decide_team(dt, on_turn=lambda d, t: live.append(t.speaker + ":" + t.stage))
    print("team live turns:", live)
    assert live == ["claude:opening", "grok:answer", "gemini:answer", "claude:reaction", "grok:final", "gemini:final",
                    "claude:decision"], live
    assert dt.decision["headline"].startswith("Wait for the Q3 result")
    assert council.recent(5)[0].id == dt.id and dt.path().exists()
    lst = cards.discussion_list(council.recent(5))
    check_html(lst)
    try:
        council.require_discussion(council.Discussion.new("team", "x"))
        raise AssertionError("require_discussion must refuse an empty discussion")
    except council.NoDiscussion:
        pass

    # 4. dry card (no AI) is clean too
    dry = cards.stock_card("Test & Co", "TST", r["numbers"], {}, None, dry=True)
    check_html(dry)
    assert "🧪" in dry and "Test &amp; Co" in dry
    print("\nALL COUNCIL TESTS PASSED")


if __name__ == "__main__":
    main()
