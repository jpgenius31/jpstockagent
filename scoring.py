"""scoring.py - deterministic agents, Judge and grounding verifier.

No LLM, no network, no API key: this engine always works and is the fallback
for the LLM debate in llm.py.  Every figure quoted in a reason comes straight
from the evidence bundle, and verify_grounding() double-checks that.
"""
from __future__ import annotations

import math
import re

BUY_MIN_NET = 25
AVOID_MAX_NET = -15
MISSING = object()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _get(d, path):
    cur = d
    for key in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def num(ev, path):
    """Numeric value at a dotted path, or None when missing / not a number."""
    v = _get(ev, path)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _win(ev):
    n = num(ev, "technicals.sma_window")
    return f"{int(n)}-day SMA" if n else "SMA"


def _run_rules(rules, ev):
    fired, missing = [], []
    for label, fn in rules:
        out = fn(ev)
        if out is MISSING:
            missing.append(f"{label}: data unavailable")
        elif out:
            fired.append(out)
    fired.sort(key=lambda t: -t[0])
    return fired, missing


def _case(rules, ev):
    fired, missing = _run_rules(rules, ev)
    score = int(round(clamp(sum(p for p, _ in fired), 0, 100)))
    return {"score": score, "reasons": [t for _, t in fired] + missing, "n_fired": len(fired)}


# --------------------------------------------------------------------------
# Bull rules (max 100 points)
# --------------------------------------------------------------------------
def b_rvol(ev):
    r = num(ev, "technicals.rvol")
    if r is None:
        return MISSING
    pts = clamp((r - 1) / 3, 0, 1) * 20
    return (pts, f"RVOL {r:.1f}x vs prior average volume") if pts > 0 else None


def b_breakout(ev):
    p = num(ev, "range_52w.position_pct")
    if p is None:
        return MISSING
    return (15, f"Breakout zone, {p:.0f}% of the way up the 52-week range") if p >= 85 else None


def b_trend(ev):
    v = num(ev, "technicals.price_vs_sma_pct")
    if v is None:
        return MISSING
    if v <= 0:
        return None
    rising = _get(ev, "technicals.sma_rising") is True
    text = f"Price {v:.1f}% above its {_win(ev)}" + (" and the SMA is rising" if rising else "")
    return (15 if rising else 7, text)


def b_close(ev):
    p = num(ev, "technicals.day_range_position_pct")
    if p is None:
        return MISSING
    return (10, f"Closing near the top of the day's range ({p:.0f}%)") if p >= 70 else None


def b_upside(ev):
    u = num(ev, "analyst.upside_pct")
    if u is None:
        return MISSING
    return (10, f"Analyst mean target implies {u:.1f}% upside") if u >= 10 else None


def b_buy(ev):
    b = num(ev, "analyst.buy_pct")
    if b is None:
        return MISSING
    return (10, f"{b:.0f}% of analysts rate it Buy") if b >= 80 else None


def b_news(ev):
    pos, neg = num(ev, "news.positive"), num(ev, "news.negative")
    if pos is None or neg is None:
        return MISSING
    if pos > neg:
        return (10, f"News tone positive ({int(pos)} positive vs {int(neg)} negative headlines)")
    return None


def b_return(ev):
    r = num(ev, "technicals.window_return_pct")
    if r is None:
        return MISSING
    days = num(ev, "technicals.window_days")
    span = f"the {int(days)}-day window" if days else "the window"
    return (10, f"Up {r:.1f}% over {span}") if r > 0 else None


BULL_RULES = [
    ("RVOL", b_rvol), ("52-week position", b_breakout), ("SMA trend", b_trend),
    ("Day-range close", b_close), ("Analyst upside", b_upside), ("Analyst buy %", b_buy),
    ("News sentiment", b_news), ("Window return", b_return),
]


# --------------------------------------------------------------------------
# Bear rules (max 100 points)
# --------------------------------------------------------------------------
def r_rvol(ev):
    r = num(ev, "technicals.rvol")
    if r is None:
        return MISSING
    return (12, f"Thin participation: RVOL {r:.1f}x") if r < 1 else None


def r_low52(ev):
    p = num(ev, "range_52w.position_pct")
    if p is None:
        return MISSING
    return (15, f"Near the 52-week low ({p:.0f}% of range)") if p < 30 else None


def r_trend(ev):
    v = num(ev, "technicals.price_vs_sma_pct")
    trend = _get(ev, "technicals.trend")
    if v is None and trend is None:
        return MISSING
    if v is not None and v < 0:
        return (15, f"Price {abs(v):.1f}% below its {_win(ev)}")
    if trend == "down":
        return (15, "Trend is down")
    return None


def r_upside(ev):
    u = num(ev, "analyst.upside_pct")
    if u is None:
        return MISSING
    return (10, f"No analyst headroom (mean target {u:+.1f}% from price)") if u <= 0 else None


def r_conviction(ev):
    b = num(ev, "analyst.buy_pct")
    if b is None:
        return MISSING
    return (10, f"Low analyst conviction ({b:.0f}% Buy)") if b < 55 else None


def r_from_high(ev):
    ph = num(ev, "range_52w.pct_from_high")
    if ph is None:
        return MISSING
    return (10, f"{abs(ph):.1f}% below the 52-week high") if ph <= -20 else None


def r_sell(ev):
    s = num(ev, "analyst.sell_pct")
    if s is None:
        return MISSING
    return (8, f"{s:.0f}% of analysts rate it Sell") if s >= 20 else None


def r_news(ev):
    pos, neg = num(ev, "news.positive"), num(ev, "news.negative")
    if pos is None or neg is None:
        return MISSING
    if neg > pos:
        return (10, f"News tone negative ({int(neg)} negative vs {int(pos)} positive headlines)")
    return None


def r_close(ev):
    p = num(ev, "technicals.day_range_position_pct")
    if p is None:
        return MISSING
    return (10, f"Weak close in the day's range ({p:.0f}%)") if p <= 30 else None


BEAR_RULES = [
    ("RVOL", r_rvol), ("52-week position", r_low52), ("SMA trend", r_trend),
    ("Analyst upside", r_upside), ("Analyst buy %", r_conviction),
    ("Distance from 52-week high", r_from_high), ("Analyst sell %", r_sell),
    ("News sentiment", r_news), ("Day-range close", r_close),
]


def bull_case(ev):
    return _case(BULL_RULES, ev)


def bear_case(ev):
    return _case(BEAR_RULES, ev)


# --------------------------------------------------------------------------
# Panel specialists (Technician / Fundamentalist / Newsdesk): 0-100 scores
# --------------------------------------------------------------------------
def _specialist(parts, missing):
    score = int(round(clamp(50 + sum(d for d, _ in parts), 0, 100)))
    top = sorted(parts, key=lambda t: -abs(t[0]))[:3]
    return {"score": score, "reasons": [t for _, t in top] + missing}


def technician(ev):
    parts, missing = [], []
    r = num(ev, "technicals.rvol")
    if r is None:
        missing.append("RVOL: data unavailable")
    else:
        parts.append((clamp((r - 1) / 3, -0.3, 1) * 20, f"RVOL {r:.1f}x"))
    v = num(ev, "technicals.price_vs_sma_pct")
    if v is None:
        missing.append("SMA trend: data unavailable")
    else:
        parts.append((clamp(v / 5, -1, 1) * 15, f"Price {v:+.1f}% vs its {_win(ev)}"))
    trend = _get(ev, "technicals.trend")
    if trend in ("up", "down"):
        parts.append((10 if trend == "up" else -10, f"Trend is {trend}"))
    p = num(ev, "technicals.day_range_position_pct")
    if p is None:
        missing.append("Day-range close: data unavailable")
    else:
        parts.append(((p - 50) / 50 * 10, f"Close at {p:.0f}% of the day's range"))
    w = num(ev, "technicals.window_return_pct")
    if w is None:
        missing.append("Window return: data unavailable")
    else:
        parts.append((clamp(w / 10, -1, 1) * 10, f"{w:+.1f}% over the window"))
    return _specialist(parts, missing)


def fundamentalist(ev):
    parts, missing = [], []
    u = num(ev, "analyst.upside_pct")
    if u is None:
        missing.append("Analyst upside: data unavailable")
    else:
        parts.append((clamp(u / 30, -1, 1) * 25, f"Mean target {u:+.1f}% from price"))
    b = num(ev, "analyst.buy_pct")
    if b is None:
        missing.append("Analyst buy %: data unavailable")
    else:
        parts.append(((b - 50) / 50 * 15, f"{b:.0f}% of analysts rate it Buy"))
    c = _get(ev, "analyst.consensus")
    cmap = {"strong_buy": 10, "buy": 5, "hold": 0, "underperform": -8, "sell": -8, "strong_sell": -12}
    if isinstance(c, str) and c in cmap:
        parts.append((cmap[c], f"Consensus: {c.replace('_', ' ')}"))
    elif c is None:
        missing.append("Consensus: data unavailable")
    return _specialist(parts, missing)


def newsdesk(ev):
    total = num(ev, "news.total")
    pos, neg, neu = num(ev, "news.positive"), num(ev, "news.negative"), num(ev, "news.neutral")
    if total is None or pos is None or neg is None:
        return {"score": 50, "reasons": ["News: data unavailable"]}
    if total == 0:
        return {"score": 50, "reasons": ["No recent headlines"]}
    score = int(round(clamp(50 + (pos - neg) / total * 50, 0, 100)))
    neu_txt = f", {int(neu)} neutral" if neu is not None else ""
    return {"score": score, "reasons": [f"{int(pos)} positive, {int(neg)} negative{neu_txt} headlines"]}


# --------------------------------------------------------------------------
# Judge
# --------------------------------------------------------------------------
def judge(ev, bull, bear):
    b, r = bull["score"], bear["score"]
    net = b - r
    pos = num(ev, "range_52w.position_pct")
    rvol = num(ev, "technicals.rvol")
    leadership = (pos is not None and pos >= 60) or (rvol is not None and rvol >= 3)

    if net >= BUY_MIN_NET and leadership:
        verdict = "BUY"
    elif net <= AVOID_MAX_NET:
        verdict = "AVOID"
    else:
        verdict = "WATCH"

    conf = int(clamp(round(4 + net / 15), 1, 10))
    conf = max(conf, 7) if verdict == "BUY" else min(conf, 6)
    winner = "Bear" if r > b else "Bull"

    bull_top = [t for t in bull["reasons"] if "unavailable" not in t][:2]
    bear_top = [t for t in bear["reasons"] if "unavailable" not in t][:2]
    if verdict == "BUY":
        rationale = f"Bull {b} vs Bear {r} (net {net:+d}) with price leadership confirmed. " + "; ".join(bull_top) + "."
    elif verdict == "AVOID":
        rationale = f"Bear {r} vs Bull {b} (net {net:+d}); risk outweighs reward. " + ("; ".join(bear_top) + "." if bear_top else "")
    elif net >= BUY_MIN_NET:
        rationale = f"Bull {b} vs Bear {r} but no breakout or volume confirmation yet. " + (bull_top[0] + "." if bull_top else "")
    else:
        lead = (bull_top if winner == "Bull" else bear_top)
        rationale = f"Mixed picture: Bull {b} vs Bear {r} (net {net:+d}). " + (lead[0] + "." if lead else "No strong signal in the data.")

    win_reasons = [t for t in (bull if winner == "Bull" else bear)["reasons"] if "unavailable" not in t]
    catalyst = win_reasons[0] if win_reasons else "data unavailable"
    return {
        "winner": winner, "verdict": verdict, "confidence": conf,
        "rationale": rationale.strip(), "key_catalyst": catalyst,
        "bull_score": b, "bear_score": r, "net": net,
    }


def evaluate(evidence):
    """evaluate(evidence) -> {scores:{agent:{score,reasons}}, verdict:{...}}"""
    ev = evidence or {}
    bull, bear = bull_case(ev), bear_case(ev)
    scores = {
        "bull": {"score": bull["score"], "reasons": bull["reasons"]},
        "bear": {"score": bear["score"], "reasons": bear["reasons"]},
        "technician": technician(ev),
        "fundamentalist": fundamentalist(ev),
        "newsdesk": newsdesk(ev),
    }
    res = {"scores": scores, "verdict": judge(ev, bull, bear), "engine": "deterministic"}
    res["grounding"] = grounding_report(res, ev)
    return res


# --------------------------------------------------------------------------
# Grounding verifier: every number in the output must trace to the evidence
# --------------------------------------------------------------------------
_STRIP = [re.compile(r"\b52[- ]?(?:week|wk)s?\b", re.I), re.compile(r"\d+\s*/\s*10\b")]
_NUM_RE = re.compile(r"(?<![\w.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def numbers_in(text):
    t = str(text)
    for pat in _STRIP:
        t = pat.sub(" ", t)
    out = []
    for m in _NUM_RE.finditer(t):
        try:
            out.append((m.group(0), float(m.group(0).replace(",", ""))))
        except ValueError:
            pass
    return out


def evidence_numbers(ev):
    pool = []

    def walk(x, key=""):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            pool.append(float(x))
        elif isinstance(x, dict):
            for k, v in x.items():
                if k in ("pulled_at", "notes", "data_gaps"):
                    continue
                walk(v, k)
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
        elif isinstance(x, str) and key in ("title", "name", "symbol", "publisher"):
            pool.extend(n for _, n in numbers_in(x))

    walk(ev)
    return pool


def _matches(n, pool):
    a = abs(n)
    for e in pool:
        e = abs(e)
        if abs(a - e) <= 0.06:
            return True
        if a == round(e) or a == math.floor(e + 0.5):
            return True
    return False


def verify_grounding(texts, evidence, extra=()):
    """Return the list of numeric tokens in `texts` that are not traceable to
    `evidence` (or to `extra`, the panel's own scores).  Empty list = grounded."""
    pool = evidence_numbers(evidence or {})
    pool += [float(x) for x in extra if isinstance(x, (int, float)) and not isinstance(x, bool)]
    flags = []
    for text in texts:
        for token, n in numbers_in(text or ""):
            if not _matches(n, pool) and token not in flags:
                flags.append(token)
    return flags


def collect_texts(res):
    texts = []
    for s in res.get("scores", {}).values():
        texts += list(s.get("reasons", []))
    v = res.get("verdict", {})
    texts += [v.get("rationale", ""), v.get("key_catalyst", "")]
    return texts


def grounding_report(res, evidence):
    v = res.get("verdict", {})
    extra = [s.get("score") for s in res.get("scores", {}).values()]
    extra += [v.get(k) for k in ("bull_score", "bear_score", "net", "confidence")]
    flags = verify_grounding(collect_texts(res), evidence, extra)
    return {"ok": not flags, "flags": flags}
