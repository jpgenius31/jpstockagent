"""data_sources.py - demo loader, yfinance adapter and the evidence builder.

Every stock becomes ONE normalised evidence bundle.  A value that cannot be
computed is null AND its dotted path is listed in data_gaps.

NOTE: this feed has no raw fundamental ratios (P/E, ROE, debt/equity ...).
Fundamentals are judged from analyst consensus, targets and the 52-week range.
"""
from __future__ import annotations

import glob
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DEMO_DIR = os.path.join(BASE, "demo_data")
IST = timezone(timedelta(hours=5, minutes=30))
CAP_BUCKETS = ("large", "mid", "small")
SMA_WINDOW = 10
NOTE_NO_RATIOS = ("This feed has no raw fundamental ratios (P/E, ROE, debt/equity ...); fundamentals are "
                  "judged from analyst consensus, price targets and the 52-week range only.")


def now_ist():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------
# small numeric helpers
# --------------------------------------------------------------------------
def _f(x):
    try:
        if x is None or isinstance(x, bool):
            return None
        v = float(x)
        return None if v != v or v in (float("inf"), float("-inf")) else v
    except (TypeError, ValueError):
        return None


def _r(x, nd=2):
    v = _f(x)
    return None if v is None else round(v, nd)


def _pct(a, b):
    a, b = _f(a), _f(b)
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1) * 100


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# --------------------------------------------------------------------------
# news sentiment (simple, transparent keyword lexicon)
# --------------------------------------------------------------------------
_POS = {"surge", "surges", "jumps", "jump", "rally", "rallies", "gain", "gains", "beat", "beats", "record",
        "upgrade", "upgrades", "upgraded", "profit", "growth", "wins", "win", "order", "orders", "bags",
        "strong", "bullish", "rises", "rise", "soars", "outperform", "expansion", "approval", "dividend",
        "buyback", "breakout", "boost", "boosts", "higher"}
_NEG = {"fall", "falls", "plunge", "plunges", "drop", "drops", "miss", "misses", "downgrade", "downgrades",
        "downgraded", "loss", "losses", "probe", "fraud", "weak", "bearish", "slump", "cut", "cuts", "decline",
        "declines", "penalty", "ban", "default", "lawsuit", "resigns", "resign", "selloff", "slides", "lower",
        "warning", "concern", "concerns"}


def _sentiment(title):
    words = re.findall(r"[a-z]+", str(title).lower())
    p, n = sum(w in _POS for w in words), sum(w in _NEG for w in words)
    return "positive" if p > n else "negative" if n > p else "neutral"


def _news_items(raw):
    if raw is None:
        return None
    items = []
    for n in raw:
        if not isinstance(n, dict):
            continue
        c = n.get("content") if isinstance(n.get("content"), dict) else n
        title = c.get("title") or n.get("title")
        if not title:
            continue
        prov = c.get("provider")
        pub = prov.get("displayName") if isinstance(prov, dict) else None
        pub = pub or c.get("publisher") or n.get("publisher")
        items.append({"title": str(title).strip()[:160], "publisher": pub, "sentiment": _sentiment(title)})
    return items


# --------------------------------------------------------------------------
# evidence builder
# --------------------------------------------------------------------------
def collect_gaps(bundle):
    gaps = []

    def walk(x, path):
        if isinstance(x, dict):
            for k, v in x.items():
                if not path and k in ("data_gaps", "notes", "pulled_at", "source", "sample"):
                    continue
                if path == "news" and k == "recent":
                    continue
                walk(v, f"{path}.{k}" if path else k)
        elif x is None:
            gaps.append(path)

    walk(bundle, "")
    return gaps


def build_evidence(meta, rows, info, news_items, recs=None, pulled_at=None, source="yfinance"):
    """meta: {symbol,name,cap_segment,sector}; rows: [{date,open,high,low,close,volume}] oldest->newest;
    info: yfinance-style .info dict; news_items: list (or None if unavailable); recs: optional
    {strongBuy,buy,hold,sell,strongSell} counts."""
    rows = [r for r in (rows or []) if _f(r.get("close")) is not None]
    info = info or {}
    last = rows[-1] if rows else {}
    prev = rows[-2] if len(rows) >= 2 else {}

    live = _f(info.get("currentPrice"))
    if live is None:
        live = _f(info.get("regularMarketPrice"))
    if live is None:
        live = _f(last.get("close"))
    prev_close = _f(prev.get("close"))
    if prev_close is None:
        prev_close = _f(info.get("regularMarketPreviousClose")) or _f(info.get("previousClose"))
    price = {
        "live": _r(live), "day_open": _r(last.get("open")), "day_high": _r(last.get("high")),
        "day_low": _r(last.get("low")), "prev_close": _r(prev_close),
        "day_change_pct": _r(_pct(live, prev_close)),
        "volume": int(_f(last.get("volume"))) if _f(last.get("volume")) is not None else None,
    }

    hi, lo = _f(info.get("fiftyTwoWeekHigh")), _f(info.get("fiftyTwoWeekLow"))
    pos = None
    if live is not None and hi is not None and lo is not None and hi > lo:
        pos = clamp01((live - lo) / (hi - lo)) * 100
    range_52w = {"high": _r(hi), "low": _r(lo), "pct_from_high": _r(_pct(live, hi)),
                 "position_pct": _r(pos)}

    closes = [_f(r.get("close")) for r in rows]
    highs = [_f(r.get("high")) for r in rows if _f(r.get("high")) is not None]
    lows = [_f(r.get("low")) for r in rows if _f(r.get("low")) is not None]
    vols = [_f(r.get("volume")) for r in rows]
    rvol = None
    if len(vols) >= 2 and vols[-1] is not None:
        prior = [v for v in vols[:-1][-20:] if v is not None and v > 0]
        if prior:
            rvol = vols[-1] / (sum(prior) / len(prior))
    n = min(SMA_WINDOW, len(closes))
    sma_now = _mean(closes[-n:]) if n >= 3 else None
    sma_prev = _mean(closes[-n - 5:-5]) if sma_now is not None and len(closes) >= n + 5 else None
    rising = None if sma_now is None or sma_prev is None else sma_now > sma_prev
    pvs = _pct(live, sma_now)
    if pvs is None:
        trend = None
    elif pvs > 1 and rising is not False:
        trend = "up"
    elif pvs < -1 and rising is not True:
        trend = "down"
    else:
        trend = "sideways"
    day_pos = None
    dh, dl = _f(last.get("high")), _f(last.get("low"))
    if live is not None and dh is not None and dl is not None and dh > dl:
        day_pos = clamp01((live - dl) / (dh - dl)) * 100
    technicals = {
        "rvol": _r(rvol), "price_vs_sma_pct": _r(pvs), "sma_window": n if sma_now is not None else None,
        "sma_rising": rising,
        "window_return_pct": _r(_pct(closes[-1], closes[0])) if len(closes) >= 2 else None,
        "window_days": len(rows) if rows else None,
        "swing_high": _r(max(highs)) if highs else None, "swing_low": _r(min(lows)) if lows else None,
        "day_range_position_pct": _r(day_pos), "trend": trend,
    }

    buy = hold = sell = None
    if recs:
        vals = {k: _f(recs.get(k)) or 0 for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
        tot = sum(vals.values())
        if tot > 0:
            buy = (vals["strongBuy"] + vals["buy"]) / tot * 100
            hold = vals["hold"] / tot * 100
            sell = (vals["sell"] + vals["strongSell"]) / tot * 100
    cons = info.get("recommendationKey")
    cons = None if not cons or str(cons).lower() == "none" else str(cons)
    tmean = _f(info.get("targetMeanPrice"))
    nan = _f(info.get("numberOfAnalystOpinions"))
    analyst = {
        "consensus": cons, "num_analysts": int(nan) if nan is not None else None,
        "buy_pct": _r(buy, 1), "hold_pct": _r(hold, 1), "sell_pct": _r(sell, 1),
        "target_mean": _r(tmean), "target_low": _r(info.get("targetLowPrice")),
        "target_high": _r(info.get("targetHighPrice")), "upside_pct": _r(_pct(tmean, live)),
    }

    if news_items is None:
        news = {"total": None, "positive": None, "negative": None, "neutral": None, "recent": []}
    else:
        news = {"total": len(news_items),
                "positive": sum(i["sentiment"] == "positive" for i in news_items),
                "negative": sum(i["sentiment"] == "negative" for i in news_items),
                "neutral": sum(i["sentiment"] == "neutral" for i in news_items),
                "recent": news_items[:5]}

    bundle = {
        "symbol": meta.get("symbol"), "name": meta.get("name") or meta.get("symbol"),
        "cap_segment": meta.get("cap_segment"), "sector": meta.get("sector") or info.get("sector"),
        "price": price, "range_52w": range_52w, "technicals": technicals, "analyst": analyst,
        "news": news, "data_gaps": [], "pulled_at": pulled_at or now_ist(), "source": source,
        "notes": NOTE_NO_RATIOS,
    }
    bundle["data_gaps"] = collect_gaps(bundle)
    return bundle


def clamp01(x):
    return max(0.0, min(1.0, x))


# --------------------------------------------------------------------------
# demo mode
# --------------------------------------------------------------------------
def load_demo(log=print, directory=None):
    bundles = []
    for path in sorted(glob.glob(os.path.join(directory or DEMO_DIR, "*.json"))):
        if os.path.basename(path).startswith("_"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            for b in (raw if isinstance(raw, list) else [raw]):
                if isinstance(b, dict) and b.get("symbol") and isinstance(b.get("price"), dict):
                    b.setdefault("data_gaps", collect_gaps(b))
                    bundles.append(b)
                else:
                    log(f"Skipped invalid demo bundle in {os.path.basename(path)}")
        except Exception as e:
            log(f"Skipped {os.path.basename(path)}: {type(e).__name__}")
    return bundles


# --------------------------------------------------------------------------
# live mode (NSE via yfinance)
# --------------------------------------------------------------------------
def load_universe(path=None):
    path = path or os.path.join(BASE, "universe.json")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    out = {}
    for cap in CAP_BUCKETS:
        items = []
        for it in raw.get(cap, []):
            if isinstance(it, str):
                it = {"symbol": it}
            sym = str(it.get("symbol", "")).strip().upper()
            sym = sym[:-3] if sym.endswith(".NS") else sym
            if sym:
                items.append({"symbol": sym, "name": it.get("name") or sym, "sector": it.get("sector")})
        out[cap] = items
    return out


def _df_rows(df):
    rows = []
    if df is None or getattr(df, "empty", True):
        return rows
    for idx, r in df.iterrows():
        close = _f(r.get("Close"))
        if close is None:
            continue
        rows.append({"date": str(idx)[:10], "open": _f(r.get("Open")), "high": _f(r.get("High")),
                     "low": _f(r.get("Low")), "close": close, "volume": _f(r.get("Volume"))})
    return rows


def _recs(ticker):
    df = ticker.recommendations
    if df is None or getattr(df, "empty", True):
        return None
    row = df.iloc[0]
    return {k: row[k] for k in ("strongBuy", "buy", "hold", "sell", "strongSell") if k in df.columns}


def load_live(universe, shortlist_n=4, log=print, workers=6):
    """Screen every bucket by day-change, keep the top `shortlist_n`, build evidence bundles.
    Returns (bundles, scanned_count)."""
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError("yfinance is not installed - run: pip install -r requirements.txt")

    tasks = [(cap, it) for cap in CAP_BUCKETS for it in universe.get(cap, [])]
    if not tasks:
        raise RuntimeError("universe.json has no tickers")
    log(f"Screening {len(tasks)} NSE tickers by day change ...")

    def history(task):
        try:
            df = yf.Ticker(task[1]["symbol"] + ".NS").history(period="1mo", interval="1d", auto_adjust=False)
            return task, _df_rows(df)
        except Exception:
            return task, []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        hist = list(ex.map(history, tasks))
    usable = [(t, rows) for t, rows in hist if len(rows) >= 2]
    if not usable:
        raise RuntimeError("no price data came back from yfinance (offline or rate-limited?)")
    log(f"Price history received for {len(usable)}/{len(tasks)} tickers")

    picks = []
    for cap in CAP_BUCKETS:
        ranked = []
        for (c, it), rows in usable:
            if c == cap:
                ranked.append((_pct(rows[-1]["close"], rows[-2]["close"]) or -1e9, c, it, rows))
        ranked.sort(key=lambda t: -t[0])
        picks += ranked[:shortlist_n]

    def detail(p):
        _, cap, it, rows = p
        t = yf.Ticker(it["symbol"] + ".NS")
        info, news, recs = {}, None, None
        try:
            info = t.info or {}
        except Exception:
            pass
        try:
            news = _news_items(t.news or [])
        except Exception:
            pass
        try:
            recs = _recs(t)
        except Exception:
            pass
        meta = {"symbol": it["symbol"], "name": it["name"], "cap_segment": cap, "sector": it.get("sector")}
        return build_evidence(meta, rows, info, news, recs, source="yfinance")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        bundles = list(ex.map(detail, picks))
    log(f"Shortlisted {len(bundles)} stocks for the debate")
    return bundles, len(usable)


# --------------------------------------------------------------------------
# CLI: python data_sources.py --snapshot   (refresh demo_data with real live data)
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Evidence-bundle utilities")
    ap.add_argument("--snapshot", action="store_true",
                    help="pull live NSE data and save the shortlisted bundles into demo_data/ (replaces existing)")
    ap.add_argument("--per-bucket", type=int, default=3)
    args = ap.parse_args()
    if not args.snapshot:
        ap.print_help()
        raise SystemExit(0)
    got, scanned = load_live(load_universe(), args.per_bucket)
    for old in glob.glob(os.path.join(DEMO_DIR, "*.json")):
        os.remove(old)
    os.makedirs(DEMO_DIR, exist_ok=True)
    for b in got:
        b["source"] = "yfinance-snapshot"
        with open(os.path.join(DEMO_DIR, f"{b['symbol']}.json"), "w", encoding="utf-8") as f:
            json.dump(b, f, indent=2, ensure_ascii=False)
    print(f"Saved {len(got)} real bundles (scanned {scanned}) to {DEMO_DIR}")
