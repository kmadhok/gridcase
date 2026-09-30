"""The daily digest: the standing questions, answered once a day for the UTC day before.

Each card runs one governed digest check (gridcase/tools/digest.py) at the digest's as_of and
writes one sentence from a fixed template whose numbers come straight from that check's
receipt, so the verifier can match every figure. No model is called. The period compared is the
last P days against the P days before, P = min(7, full days of record // 2); with less history
the comparison cards say so. The digest and its receipts are stored (table `digests`, ledger
case 'digest-<day>'), and rebuilding a day gives the same digest.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import duckdb

from gridcase.explain.explainer import DRIVER_WORDS
from gridcase.explain.schema import Claim, Conclusion
from gridcase.explain.verifier import verify
from gridcase.ledger.schema import Receipt
from gridcase.memory.cases import get_case
from gridcase.semantic.kpis import load_kpis
from gridcase.tools.base import ToolError, run_tool

log = logging.getLogger("gridcase")

DAY = timedelta(days=1)
MAX_PERIOD_DAYS = 7
HEADLINE_SHARE_POINTS = 0.03  # a category gaining 3 points of volume share
HEADLINE_MOVER_RATIO = 3.0  # a market at 3 times its usual volume
HEADLINE_SWING = 0.30  # an outcome moving 30 cents
TOTAL_KPIS = ["volume_usd", "active_traders", "markets_traded", "new_traders"]
SMALL = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"]
Window = tuple[datetime, datetime]


# ------------------------------------------------------------------ formatting


def usd(v: float) -> str:
    if abs(v) >= 1e6:
        return f"${v / 1e6:.1f}M"
    if abs(v) >= 1e4:
        return f"${v / 1e3:.0f}k"
    return f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"


def pct(share: float) -> str:
    return f"{100 * share:.0f}%"


def cents(price: float) -> str:
    return f"{100 * price:.0f}¢"


def weekday(day: str) -> str:
    return date.fromisoformat(day).strftime("%A")


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


# ------------------------------------------------------------------ periods


def _first_full_day(conn: duckdb.DuckDBPyConnection, as_of: datetime) -> datetime | None:
    row = conn.execute("SELECT min(ts) FROM trades_as_of(?)", [as_of]).fetchone()
    first: datetime | None = row[0] if row else None
    if first is None:
        return None
    day = datetime.combine(first.date(), time())
    return day if day == first else day + DAY


def periods(
    conn: duckdb.DuckDBPyConnection, day: date, as_of: datetime
) -> tuple[int, Window, Window | None, Window]:
    """(P, current, previous, yesterday) for the digest of `day` (covering up to its 00:00)."""
    midnight = datetime.combine(day, time())
    first = _first_full_day(conn, as_of)
    full_days = max(0, (midnight - first).days) if first else 0
    p = min(MAX_PERIOD_DAYS, full_days // 2)
    current = (midnight - max(p, 1) * DAY, midnight)
    previous = (midnight - 2 * p * DAY, midnight - p * DAY) if p else None
    return p, current, previous, (midnight - DAY, midnight)


def period_words(p: int) -> tuple[str, str]:
    if p == 7:
        return "this week", "last week"
    if p <= 1:
        return "yesterday", "the day before"
    n = SMALL[p].lower()  # in words: a numeral here would cite nothing
    return f"in the last {n} days", f"the {n} days before"


# ------------------------------------------------------------------ sentences


def _claim(text: str, r: Receipt | None) -> dict[str, Any]:
    return {
        "text": text,
        "receipt_ids": [r.receipt_id] if r else [],
        "tier": "governed" if r else "inferred",
    }


def totals_sentence(o: dict[str, Any], cur: str, prev: str) -> str:
    items = {i["kpi"]: i for i in o["items"]}
    vol, traders = items["volume_usd"], items["active_traders"]
    who = f"by {_plural(int(traders['now']), 'active trader')}"
    if vol["pct_change"] is None:
        return f"{usd(vol['now'])} was traded {cur}, {who}."
    more = "more" if vol["pct_change"] > 0 else "less"
    return (
        f"{usd(vol['now'])} was traded {cur}, {abs(vol['pct_change']):.0f}% {more} than "
        f"{prev}, {who}."
    )


def share_sentence(o: dict[str, Any], cur: str) -> str:
    if o["insufficient"]:
        return "There is not enough history yet to compare category shares between periods."
    segs = {s["segment"]: s for s in o["segments"]}
    g, lo = segs[o["top_gainer"]], segs[o["top_loser"]]
    if o["gain"] < 0.005:
        top = o["segments"][0]
        return (
            f"Category shares held steady {cur}: {top['segment']} kept the most, {pct(top['now'])}."
        )
    return (
        f"{g['segment']} rose from {pct(g['before'])} to {pct(g['now'])} of all volume {cur}. "
        f"{lo['segment']} gave up the most, falling from {pct(lo['before'])} to {pct(lo['now'])}."
    )


def movers_sentence(o: dict[str, Any]) -> str:
    if o["insufficient"]:
        return "There is not enough history yet to say which markets are unusually busy."
    if not o["markets"] or o["markets"][0]["change"] <= 0:
        return "No market traded much more than usual yesterday."
    m = o["markets"][0]
    if m["ratio"] is None:
        return (
            f"{m['market']} came from nowhere yesterday: {usd(m['now'])} traded, with no trading "
            "on the days before."
        )
    return (
        f"{m['market']} traded {m['ratio']:.1f} times its usual daily volume yesterday: "
        f"{usd(m['now'])} against about {usd(m['usual'])}."
    )


def swings_sentence(o: dict[str, Any]) -> str:
    if not o["swings"]:
        return "No event market's odds moved much yesterday."
    s = o["swings"][0]
    return (
        f"{s['market']} ({s['outcome']}) went from {cents(s['open'])} to {cents(s['close'])} "
        "yesterday, the biggest odds move of any event market."
    )


def whales_sentence(o: dict[str, Any], cur: str) -> str:
    if not o["days"]:
        return "There was no trading to measure yet."
    if len(o["days"]) == 1:
        return f"Whales placed {pct(o['peak_value'])} of yesterday's volume."
    return (
        f"Whales placed {pct(o['peak_value'])} of {weekday(o['peak_day'])}'s volume, the "
        f"highest share {cur}; the average day was {pct(o['mean'])}."
    )


def retention_sentence(o: dict[str, Any]) -> str:
    if not o["days"]:
        return "There are no new traders to follow up yet."
    last = o["latest"]
    text = (
        f"{pct(last['rate'])} of the {_plural(last['new'], 'new trader')} on "
        f"{weekday(last['day'])} came back the next day."
    )
    if len(o["days"]) > 1:
        text += f" Across these days it was {pct(o['mean_rate'])}."
    return text


def rhythm_sentence(o: dict[str, Any]) -> str:
    if "peak" not in o:
        return "There was no trading to measure yet."
    p, q = o["peak"], o["quiet"]
    return (
        f"Money peaks on {p['weekday']}s around {p['hour']:02d}:00 UTC and is quietest on "
        f"{q['weekday']}s around {q['hour']:02d}:00 UTC."
    )


def concentration_sentence(o: dict[str, Any], cur: str) -> str:
    if not o["markets"]:
        return "There was no trading to measure yet."
    n = o["markets_for_half"]
    few = "a single market" if n == 1 else f"just {n:,} markets"
    return f"Half of all volume went to {few}, out of {o['markets']:,} that traded {cur}."


def kind_sentence(o: dict[str, Any], cur: str) -> str:
    if not o["days"]:
        return "There was no trading to measure yet."
    return (
        f'{pct(o["mean"])} of the money went to short recurring crypto rounds ("Up or Down" '
        f"markets) {cur}; the rest was on real events."
    )


def alarms_sentence(cases: list[dict[str, Any]]) -> str:
    n = len(cases)
    if not n:
        return "No watched number went outside its usual range in the last day."
    word = SMALL[n] if n < len(SMALL) else "Many"
    return f"{word} investigation{'s' if n != 1 else ''} opened automatically in the last day."


# ------------------------------------------------------------------ headline


def headline(
    cards: dict[str, dict[str, Any]], cur: str, fallback: dict[str, Any]
) -> dict[str, Any]:
    """The first card, in page order, whose story clears its bar; else the totals sentence."""
    shift, mover, swing = cards["share_shift"], cards["movers"], cards["odds_swings"]
    o = shift["data"]
    if not o["insufficient"] and o["gain"] >= HEADLINE_SHARE_POINTS:
        pts = round(100 * o["gain"])
        text = (
            f"{o['top_gainer']} took {pts} more point{'s' if pts != 1 else ''} of the money {cur}."
        )
        return {**shift["sentence"], "text": text, "card": "share_shift"}
    o = mover["data"]
    if o["markets"] and (o["markets"][0]["ratio"] or 0) >= HEADLINE_MOVER_RATIO:
        m = o["markets"][0]
        text = f"{m['market']} traded {m['ratio']:.1f} times its usual volume yesterday."
        return {**mover["sentence"], "text": text, "card": "movers"}
    o = swing["data"]
    if o["swings"] and abs(o["swings"][0]["move"]) >= HEADLINE_SWING:
        s = o["swings"][0]
        text = (
            f"Traders changed their minds on {s['market']}: {s['outcome']} went from "
            f"{cents(s['open'])} to {cents(s['close'])} yesterday."
        )
        return {**swing["sentence"], "text": text, "card": "odds_swings"}
    return {**fallback, "card": "totals"}


# ------------------------------------------------------------------ build


CARDS: list[tuple[str, str, str, str]] = [
    # id, section, question, note (how it is worked out)
    (
        "share_shift",
        "What changed",
        "Which category gained share of volume?",
        "Each category's share of volume, this period against the one before",
    ),
    (
        "movers",
        "What changed",
        "Which markets came out of nowhere yesterday?",
        "Each market's volume yesterday against its own daily average",
    ),
    (
        "odds_swings",
        "What traders believe",
        "Whose odds moved the most yesterday?",
        "First against last price of the day; event markets, 20 or more trades",
    ),
    (
        "whales",
        "Who is trading",
        "Are whales or regular traders driving the money?",
        "Share of volume from trades of $1,000 or more, by day",
    ),
    (
        "retention",
        "Who is trading",
        "Do new traders come back the next day?",
        "Wallets on their first day in our record that traded again the next day",
    ),
    (
        "rhythm",
        "How the market behaves",
        "When is Polymarket busiest?",
        "Average volume by weekday and hour, UTC",
    ),
    (
        "concentration",
        "How the market behaves",
        "How concentrated is the money?",
        "Markets ranked by volume until they make up half of it",
    ),
    (
        "kind",
        "How the market behaves",
        "How much is 5-minute crypto betting, and how much is real forecasting?",
        "Share of volume in recurring Up or Down markets, by day",
    ),
    (
        "alarms",
        "What the watch caught",
        "What set off an alarm in the last day?",
        "Investigations the hourly watch opened",
    ),
]


def _alarm_cases(
    conn: duckdb.DuckDBPyConnection, lo: datetime, hi: datetime
) -> list[dict[str, Any]]:
    kpis, _ = load_kpis()
    rows = conn.execute(
        "SELECT case_id FROM cases WHERE as_of >= ? AND as_of <= ? ORDER BY as_of, case_id",
        [lo, hi],
    ).fetchall()
    out = []
    for (cid,) in rows:
        c = get_case(conn, cid)
        out.append(
            {
                "case_id": c.case_id,
                "kpi": c.metric,
                "kpi_label": kpis[c.metric].label if c.metric in kpis else c.metric,
                "segment": c.entity,
                "as_of": c.as_of.isoformat(),
                "explanation": DRIVER_WORDS.get(c.top_hypothesis or "", None),
            }
        )
    return out


def build_digest(conn: duckdb.DuckDBPyConnection, day: date, as_of: datetime) -> dict[str, Any]:
    p, current, previous, yesterday = periods(conn, day, as_of)
    cur, prev = period_words(p)
    case_id = f"digest-{day.isoformat()}"
    receipts: dict[str, Receipt] = {}

    def check(name: str, inputs: dict[str, Any]) -> Receipt | None:
        try:
            r = run_tool(conn, name, inputs, as_of, case_id=case_id)
        except ToolError as e:
            log.warning("digest %s: %s failed: %s", day, name, e)
            return None
        receipts[r.receipt_id] = r
        return r

    first = _first_full_day(conn, as_of)
    follow_lo = max(current[1] - 8 * DAY, first or current[0])
    runs: dict[str, tuple[str, dict[str, Any], Callable[[dict[str, Any]], str]]] = {
        "share_shift": (
            "digest.share_shift",
            {"current": current, "previous": previous},
            lambda o: share_sentence(o, cur),
        ),
        "movers": ("digest.movers", {"window": yesterday}, movers_sentence),
        "odds_swings": ("digest.odds_swings", {"window": yesterday}, swings_sentence),
        "whales": (
            "digest.by_day",
            {"kpi": "whale_share", "window": current},
            lambda o: whales_sentence(o, cur),
        ),
        "retention": ("digest.retention", {"window": (follow_lo, current[1])}, retention_sentence),
        "rhythm": ("digest.rhythm", {"window": current}, rhythm_sentence),
        "concentration": (
            "digest.concentration",
            {"window": current},
            lambda o: concentration_sentence(o, cur),
        ),
        "kind": (
            "digest.by_day",
            {"kpi": "recurring_share", "window": current},
            lambda o: kind_sentence(o, cur),
        ),
    }

    cards: dict[str, dict[str, Any]] = {}
    for cid, section, question, note in CARDS:
        card: dict[str, Any] = {"id": cid, "section": section, "question": question, "note": note}
        if cid == "alarms":
            cases = _alarm_cases(conn, yesterday[0], as_of)
            card |= {
                "data": {"cases": cases},
                "insufficient": False,
                "sentence": _claim(alarms_sentence(cases), None),
            }
        else:
            tool_name, inputs, sentence = runs[cid]
            r = check(tool_name, inputs)
            if r is None:
                card |= {
                    "data": {},
                    "insufficient": True,
                    "sentence": _claim("This question could not be answered today.", None),
                }
            else:
                card |= {
                    "data": r.output,
                    "insufficient": bool(r.output.get("insufficient")),
                    "sentence": _claim(sentence(r.output), r),
                }
        cards[cid] = card

    tr = check("digest.totals", {"kpis": TOTAL_KPIS, "current": current, "previous": previous})
    totals: dict[str, Any] = {
        "items": tr.output["items"] if tr else [],
        "sentence": _claim(totals_sentence(tr.output, cur, prev), tr) if tr else None,
    }
    cov = check("data.coverage", {"window": ((previous or current)[0], current[1])})
    head = headline(cards, cur, totals["sentence"] or _claim("", None)) if tr else None

    _verify_all(cards, totals, head, receipts)
    digest = {
        "day": day.isoformat(),
        "as_of": as_of.isoformat(),
        "period_days": p,
        "period_words": {"current": cur, "previous": prev},
        "windows": {
            "current": [t.isoformat() for t in current],
            "previous": [t.isoformat() for t in previous] if previous else None,
            "yesterday": [t.isoformat() for t in yesterday],
        },
        "headline": head,
        "totals": totals,
        "cards": list(cards.values()),
        "coverage": cov.output if cov else None,
        "receipts": [r.model_dump(mode="json") for r in receipts.values()],
    }
    digest = dict(json.loads(json.dumps(digest)))
    conn.execute(
        "INSERT OR REPLACE INTO digests VALUES (?, ?, ?, ?)",
        [day, as_of, datetime.now(UTC).replace(tzinfo=None), json.dumps(digest)],
    )
    return digest


def _verify_all(
    cards: dict[str, dict[str, Any]],
    totals: dict[str, Any],
    head: dict[str, Any] | None,
    receipts: dict[str, Receipt],
) -> None:
    """Every sentence must pass the verifier; a template that states a number its receipt does
    not hold is a bug, so the sentence is replaced rather than shown."""
    sentences = [c["sentence"] for c in cards.values()] + [
        s for s in (totals["sentence"], head) if s is not None
    ]
    for s in sentences:
        claim = Claim(text=s["text"], receipt_ids=s["receipt_ids"], tier=s["tier"])
        _, dropped = verify(
            Conclusion(case_id="digest", top_hypothesis=None, ranked=[], claims=[claim]),
            receipts,
            drop_uncited_numbers=True,
        )
        if dropped:
            log.error("digest sentence failed verification: %s", dropped[0])
            s.update(
                text="This answer could not be checked today.", receipt_ids=[], tier="inferred"
            )


# ------------------------------------------------------------------ read back


def get_digest(conn: duckdb.DuckDBPyConnection, day: date) -> dict[str, Any] | None:
    row = conn.execute("SELECT digest_json FROM digests WHERE day = ?", [day]).fetchone()
    return None if row is None else dict(json.loads(row[0]))


def latest_digest(conn: duckdb.DuckDBPyConnection) -> dict[str, Any] | None:
    row = conn.execute("SELECT digest_json FROM digests ORDER BY day DESC LIMIT 1").fetchone()
    return None if row is None else dict(json.loads(row[0]))


def list_digests(conn: duckdb.DuckDBPyConnection) -> list[dict[str, str]]:
    rows = conn.execute("SELECT day FROM digests ORDER BY day DESC").fetchall()
    return [{"day": d.isoformat()} for (d,) in rows]


def due_day(conn: duckdb.DuckDBPyConnection, wall: datetime, settle: timedelta) -> date | None:
    """The day whose digest is due at `wall`: today's, once `settle` has passed midnight so the
    last trades of yesterday are collected, if it has not been built yet."""
    if wall - datetime.combine(wall.date(), time()) < settle:
        return None
    day = wall.date()
    row = conn.execute("SELECT count(*) FROM digests WHERE day = ?", [day]).fetchone()
    return None if row and row[0] else day
