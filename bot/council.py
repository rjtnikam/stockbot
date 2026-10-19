"""The council: Claude, Grok and Gemini discuss before any decision.

Order (each member reads everything said before its turn):
  1. Claude (chair) opens: a draft view and specific questions to Grok and to Gemini
  2. Grok answers from X (Twitter) and challenges claims
  3. Gemini answers from the web and replies to Grok
  4. Claude reacts: what changed its view, one follow-up question each
  5. Grok and Gemini give their final answers and a VOTE
  6. Claude decides (a stock: the full memo; /team: a short decision)

The rule in code: require_discussion() blocks every decision unless at least one
other member gave a real answer. Claude cannot skip the discussion.
Every discussion is saved in data/discussions/ (read it with /talk in Telegram).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import ai
from .config import DATA, PHILOSOPHY_FILE

log = logging.getLogger(__name__)
DIR = DATA / "discussions"
DIR.mkdir(parents=True, exist_ok=True)

NAME = {"claude": "Claude", "grok": "Grok", "gemini": "Gemini"}
ICON = {"claude": "🟠", "grok": "🐦", "gemini": "🔷"}
STAGE = {"opening": "opening", "answer": "answer", "reaction": "reaction", "final": "final answer",
         "decision": "decision"}
VOTE_RE = re.compile(r"VOTE\W{0,4}(PURSUE|WATCH|REJECT)\b\W*(.*)", re.I)
POSITION_RE = re.compile(r"POSITION\W{0,4}(.+)", re.I)


class NoDiscussion(Exception):
    """Raised when a decision is asked for but the discussion did not happen."""


@dataclass
class Turn:
    speaker: str
    stage: str
    text: str = ""
    ok: bool = True
    error: str = ""
    vote: str | None = None
    secs: float = 0.0


@dataclass
class Discussion:
    id: str
    kind: str                      # "stock" or "team"
    topic: str
    sym: str | None = None
    name: str | None = None
    created: str = ""
    status: str = "open"
    turns: list[Turn] = field(default_factory=list)
    decision: dict = field(default_factory=dict)

    # ---- state
    def others_spoke(self) -> list[str]:
        return sorted({t.speaker for t in self.turns if t.speaker != "claude" and t.ok and t.text.strip()})

    def absent(self) -> dict[str, str]:
        spoke = set(self.others_spoke())
        out = {}
        for t in self.turns:
            if t.speaker != "claude" and not t.ok and t.speaker not in spoke:
                out[t.speaker] = t.error
        return out

    def votes(self) -> dict[str, dict]:
        """Last vote / position of each member: {'grok': {'vote': 'WATCH', 'reason': '...'}}."""
        out: dict[str, dict] = {}
        for t in self.turns:
            if t.speaker == "claude" or not t.ok:
                continue
            m = VOTE_RE.search(t.text) if self.kind == "stock" else POSITION_RE.search(t.text)
            if m and self.kind == "stock":
                out[t.speaker] = {"vote": m.group(1).upper(), "reason": _clean_reason(m.group(2))}
            elif m:
                out[t.speaker] = {"vote": None, "reason": _clean_reason(m.group(1))}
        return out

    def transcript(self) -> str:
        parts = []
        for t in self.turns:
            who = f"{NAME[t.speaker].upper()} ({STAGE.get(t.stage, t.stage)})"
            parts.append(f"{who}:\n{t.text}" if t.ok else f"[{NAME[t.speaker]} could not answer: {t.error[:120]}]")
        return "\n\n".join(parts)

    # ---- storage
    def path(self) -> Path:
        return DIR / f"{self.id}.json"

    def save(self) -> Path:
        p = self.path()
        p.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: Path) -> "Discussion":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        d["turns"] = [Turn(**t) for t in d.get("turns", [])]
        return cls(**d)

    @classmethod
    def new(cls, kind: str, topic: str, sym: str | None = None, name: str | None = None) -> "Discussion":
        now = datetime.now()
        base = f"{now:%Y-%m-%d}_{_slug(sym) if sym else 'team-' + now.strftime('%H%M%S')}"
        did, i = base, 2
        while (DIR / f"{did}.json").exists():
            did, i = f"{base}-{i}", i + 1
        return cls(id=did, kind=kind, topic=topic, sym=sym, name=name, created=now.isoformat(timespec="seconds"))


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9&\-]", "", str(s)).upper()[:30] or "X"


def _clean_reason(s: str) -> str:
    s = re.sub(r"^[\s\-–—:*]+", "", (s or "").strip())
    return s.split("\n")[0].strip("* ")[:300]


def _short(e: Exception) -> str:
    s = str(e)
    if "429" in s or "RESOURCE_EXHAUSTED" in s or "quota" in s.lower():
        return "quota used up, error 429"
    if "503" in s or "UNAVAILABLE" in s:
        return "service busy, error 503"
    if "401" in s or "403" in s or "api key" in s.lower():
        return "API key problem"
    return s[:160]


def require_discussion(d: Discussion | None) -> None:
    """No decision without a discussion: the chair's opening plus at least one real answer from another member."""
    if d is None:
        raise NoDiscussion("no discussion took place")
    opened = any(t.speaker == "claude" and t.stage == "opening" and t.ok for t in d.turns)
    if not opened or not d.others_spoke():
        why = "; ".join(f"{NAME[k]}: {v}" for k, v in d.absent().items()) or "the chair could not open"
        raise NoDiscussion(f"the discussion could not take place ({why})")


def recent(n: int = 10, sym: str | None = None) -> list[Discussion]:
    files = sorted(DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for f in files:
        try:
            d = Discussion.load(f)
        except Exception:
            continue
        if sym and (d.sym or "").upper() != sym.upper():
            continue
        out.append(d)
        if len(out) >= n:
            break
    return out


# ------------------------------------------------------------------ prompts
def _philosophy() -> str:
    try:
        return PHILOSOPHY_FILE.read_text(encoding="utf-8")[:12000]
    except Exception:
        return ""


STYLE = "Write plain text (no markdown, no tables). Short sentences, active voice."
INVESTOR = ("The investor is Rajat: Indian small and mid caps; he wants a business story that is changing and "
            "numbers that confirm it; he hates promotion, dilution, weak cash flow and governance risk.")


def chair_system() -> str:
    return (f"# RAJAT'S INVESTING PHILOSOPHY\n{_philosophy()}\n\n# YOUR ROLE\nYou are CLAUDE, the chair of a short "
            "discussion with two members: GROK searches X (Twitter) live; GEMINI searches Google, news, exchange "
            "filings and company documents live. You decide at the end, but only after you hear them. "
            "Be a sparring partner, not a promoter. " + STYLE)


MEMBER_SYSTEM = {
    "grok": ("You are GROK, a member of an investment discussion. You search X (Twitter) live. The other members are "
             "CLAUDE (the chair; it decides) and GEMINI (it searches the web). " + INVESTOR + "\nRules:\n"
             "- First answer the questions addressed to GROK. Give evidence: post links, dates, who said it.\n"
             "- If X has no evidence, say 'No evidence on X'. Never invent posts, numbers or links.\n"
             "- Then challenge any claim in the discussion that X evidence contradicts, and name who made it.\n"
             "- Max {words} words. " + STYLE),
    "gemini": ("You are GEMINI, a member of an investment discussion. You search Google, news, exchange filings and "
               "company documents live. The other members are CLAUDE (the chair; it decides) and GROK (it searches "
               "X/Twitter). " + INVESTOR + "\nRules:\n"
               "- First answer the questions addressed to GEMINI. Give evidence: source links and dates.\n"
               "- If you find no evidence, say 'No evidence found'. Never invent facts, numbers or links.\n"
               "- Then reply to GROK's points: say where web evidence agrees or disagrees.\n"
               "- Max {words} words. " + STYLE),
}

OPEN = {
    "stock": """RESEARCH BRIEF ON {name}
{brief}

Write your OPENING (max 180 words).
Line 1: DRAFT: PURSUE or WATCH or REJECT
Then the 2 strongest points FOR and the 2 strongest points AGAINST, each with a number from the brief.
Then ask exactly 2 questions to GROK and 2 to GEMINI. Ask what could change your decision and what their
search can answer (GROK: what investors, customers, employees or insiders say on X; GEMINI: news, orders,
capacity, regulation, competitors, management history). Use this format:
Q-GROK-1: ...
Q-GROK-2: ...
Q-GEMINI-1: ...
Q-GEMINI-2: ...""",
    "team": """RAJAT'S QUESTION:
{topic}
{brief}
Write your OPENING (max 160 words): your first view in 2-4 sentences, and what you are not sure about.
Then ask exactly 2 questions to GROK and 2 to GEMINI that their live search can answer. Use this format:
Q-GROK-1: ...
Q-GROK-2: ...
Q-GEMINI-1: ...
Q-GEMINI-2: ...""",
}

MEMBER_TASK = """{facts}
DISCUSSION SO FAR:
{transcript}

Your turn, {member}. Answer the questions addressed to {member}, then challenge or confirm the others."""

REACT_TASK = """{facts}
DISCUSSION SO FAR:
{transcript}

Write your REACTION (max 150 words). Say which points from GROK and from GEMINI change your view and which do
not, and why. Name the member each time. Then ask at most one follow-up question to each:
Q-GROK: ... (or "Q-GROK: none")
Q-GEMINI: ... (or "Q-GEMINI: none")"""

FINAL_TASK = {
    "stock": """{facts}
DISCUSSION SO FAR:
{transcript}

Your FINAL turn, {member}. Answer the follow-up addressed to {member} in max 100 words (skip it if none).
Then end with exactly one line:
VOTE: PURSUE or WATCH or REJECT — one reason""",
    "team": """DISCUSSION SO FAR:
{transcript}

Your FINAL turn, {member}. Answer the follow-up addressed to {member} in max 100 words (skip it if none).
Then end with exactly one line:
POSITION: your answer to Rajat's question in one sentence""",
}

TEAM_DECIDE = """RAJAT'S QUESTION: {topic}

DISCUSSION SO FAR:
{transcript}

Write the DECISION (max 220 words). Line 1: DECISION: your answer in one sentence.
Then say what you ACCEPTED from GROK and from GEMINI, and what you REJECTED and why. Name the member each time.
If you go against both members, give the exact reason. Last line: CONFIDENCE: low, medium or high."""


# ------------------------------------------------------------------ the discussion
def run(kind: str, topic: str, brief: str, facts: str = "", sym: str | None = None, name: str | None = None,
        cfg: dict | None = None, on_turn=None) -> Discussion:
    """Run the discussion. on_turn(discussion, turn) is called after every turn (live Telegram)."""
    c = (cfg or {}).get("council") or {}
    words = int(c.get("member_words", 200))
    rounds = int(c.get("rounds", 2))
    d = Discussion.new(kind, topic, sym, name)
    chair = chair_system()

    def say(speaker: str, stage: str, fn) -> Turn:
        t0 = time.time()
        try:
            text = (fn() or "").strip()
            if not text:
                raise RuntimeError("empty answer")
            turn = Turn(speaker, stage, text)
        except Exception as e:
            log.warning("council: %s (%s) failed: %s", speaker, stage, e)
            turn = Turn(speaker, stage, ok=False, error=_short(e))
        turn.secs = round(time.time() - t0, 1)
        if turn.ok and speaker != "claude":
            m = VOTE_RE.search(turn.text) if kind == "stock" else None
            turn.vote = m.group(1).upper() if m else None
        d.turns.append(turn)
        d.save()
        if on_turn:
            try:
                on_turn(d, turn)
            except Exception as e:
                log.warning("council live callback failed: %s", e)
        return turn

    def member(who: str, final: bool) -> str:
        task = (FINAL_TASK[kind] if final else MEMBER_TASK).format(
            facts=facts, transcript=d.transcript(), member=who.upper())
        system = MEMBER_SYSTEM[who].format(words=words if not final else min(words, 150))
        return ai.grok(system, task) if who == "grok" else ai.gemini(system, task)

    opening = say("claude", "opening", lambda: ai.claude(
        chair, OPEN[kind].format(name=name or topic, brief=brief, topic=topic), max_tokens=900))
    if not opening.ok:
        d.status = "chair failed"
        d.save()
        return d
    answered = [w for w in ("grok", "gemini") if say(w, "answer", lambda w=w: member(w, False)).ok]
    if not answered:
        d.status = "members absent"
        d.save()
        return d
    if rounds >= 2:
        react = say("claude", "reaction", lambda: ai.claude(
            chair, REACT_TASK.format(facts=facts, transcript=d.transcript()), max_tokens=700))
        if react.ok:
            for w in answered:
                say(w, "final", lambda w=w: member(w, True))
    d.status = "discussed"
    d.save()
    return d


def decide_team(d: Discussion, on_turn=None) -> Turn:
    """Claude's decision on a /team question. Refuses without a discussion."""
    require_discussion(d)
    text = ai.claude(chair_system(), TEAM_DECIDE.format(topic=d.topic, transcript=d.transcript()), max_tokens=900)
    missing = [NAME[m] for m in d.others_spoke() if NAME[m].lower() not in text.lower()]
    if missing:  # the decision must answer every member who spoke
        text = ai.claude(chair_system(), TEAM_DECIDE.format(topic=d.topic, transcript=d.transcript()) +
                         f"\n\nYour last draft ignored {', '.join(missing)}. Address every member by name.",
                         max_tokens=900)
    turn = Turn("claude", "decision", text)
    d.turns.append(turn)
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    d.decision = {"text": text, "headline": re.sub(r"^\W*DECISION\W*", "", first, flags=re.I)[:300]}
    d.status = "decided"
    d.save()
    if on_turn:
        on_turn(d, turn)
    return turn


def record_stock_decision(d: Discussion, card: dict) -> None:
    """After the thesis: store Claude's decision as the last turn of the stock discussion."""
    v, c = card.get("verdict") or "NO DECISION", card.get("conviction")
    disc = card.get("discussion") or {}
    lines = [f"DECISION: {v}" + (f" (conviction {c}/5)" if c else ""), card.get("decision_reason") or ""]
    for a in disc.get("accepted") or []:
        lines.append(f"ACCEPTED from {str(a.get('from', '')).upper()}: {a.get('point', '')}")
    for r in disc.get("rejected") or []:
        lines.append(f"REJECTED from {str(r.get('from', '')).upper()}: {r.get('point', '')} — {r.get('why', '')}")
    if disc.get("overruled_majority"):
        lines.append("Claude went against the majority of the votes (reason above).")
    d.turns.append(Turn("claude", "decision", "\n".join(x for x in lines if x)))
    d.decision = {"verdict": v, "conviction": c, "reason": card.get("decision_reason"), "discussion": disc}
    d.status = "decided"
    d.save()
