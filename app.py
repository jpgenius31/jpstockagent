"""app.py - local dashboard server, agent pipeline, Telegram sender, SQLite audit.

Analysis only.  This program never places orders and never talks to a broker.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, Response, jsonify, request, send_file

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "audit.db")
IST = timezone(timedelta(hours=5, minutes=30))
DISCLAIMER = "— Analysis only. No trade was placed. Not investment advice."


# --------------------------------------------------------------------------
# tiny .env loader (no dependency) - real environment variables win
# --------------------------------------------------------------------------
def load_env(path=None):
    path = path or os.path.join(BASE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            elif " #" in v:
                v = v.split(" #", 1)[0].strip()
            if k and k not in os.environ:
                os.environ[k] = v


load_env()

import data_sources  # noqa: E402
import llm  # noqa: E402
import scoring  # noqa: E402


def cfg_int(name, default):
    try:
        return int(float((os.environ.get(name) or "").strip() or default))
    except ValueError:
        return default


def cfg_float(name, default):
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


# --------------------------------------------------------------------------
# secret scrubbing - tokens never reach logs, UI or the audit DB
# --------------------------------------------------------------------------
_SECRET_ENVS = ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
_PATTERNS = [re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"), re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),
             re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")]


def scrub(text):
    text = str(text)
    for name in _SECRET_ENVS:
        val = (os.environ.get(name) or "").strip()
        if len(val) >= 8:
            text = text.replace(val, "***")
    for pat in _PATTERNS:
        text = pat.sub("***", text)
    return text


# --------------------------------------------------------------------------
# SQLite audit
# --------------------------------------------------------------------------
DBLOCK = threading.Lock()


def db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with DBLOCK, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS runs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, mode TEXT, engine TEXT,
          status TEXT, universe INTEGER, shortlisted INTEGER, buys INTEGER, messages_sent INTEGER, note TEXT);
        CREATE TABLE IF NOT EXISTS verdicts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, created_at TEXT, symbol TEXT, name TEXT,
          cap TEXT, verdict TEXT, confidence INTEGER, winner TEXT, rationale TEXT, catalyst TEXT,
          bull_score INTEGER, bear_score INTEGER, net INTEGER, price REAL, day_change REAL, engine TEXT,
          fallback_reason TEXT, signal_fired INTEGER, telegram_status TEXT, grounding_flags TEXT,
          evidence_json TEXT, scores_json TEXT);
        """)


def now_str():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def db_run_start(mode):
    with DBLOCK, db() as c:
        cur = c.execute("INSERT INTO runs(started_at, mode, status) VALUES(?,?,?)", (now_str(), mode, "running"))
        return cur.lastrowid


def db_run_finish(run_id, **kw):
    kw["finished_at"] = now_str()
    sets = ", ".join(f"{k}=?" for k in kw)
    with DBLOCK, db() as c:
        c.execute(f"UPDATE runs SET {sets} WHERE id=?", (*[scrub(v) if isinstance(v, str) else v for v in kw.values()], run_id))


def db_verdict(run_id, ev, row, res):
    with DBLOCK, db() as c:
        cur = c.execute(
            """INSERT INTO verdicts(run_id, created_at, symbol, name, cap, verdict, confidence, winner, rationale,
               catalyst, bull_score, bear_score, net, price, day_change, engine, fallback_reason, signal_fired,
               telegram_status, grounding_flags, evidence_json, scores_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, now_str(), row["symbol"], row["name"], row["cap"], row["verdict"], row["confidence"],
             row["winner"], scrub(row["why"]), scrub(row["catalyst"]), row["bull_score"], row["bear_score"],
             row["net"], row["price"], row["day_change"], row["engine"], scrub(row["fallback_reason"] or ""),
             int(row["fired"]), "pending" if row["fired"] else "n/a", json.dumps(row["grounding_flags"]),
             scrub(json.dumps(ev, ensure_ascii=False)), scrub(json.dumps(res.get("scores", {}), ensure_ascii=False))))
        return cur.lastrowid


def db_verdict_tg(vid, status):
    with DBLOCK, db() as c:
        c.execute("UPDATE verdicts SET telegram_status=? WHERE id=?", (scrub(status), vid))


# --------------------------------------------------------------------------
# shared state (polled by the page)
# --------------------------------------------------------------------------
AGENT_DEFS = [
    ("scout", "Scout", "Screens the stock universe for movers", ("Scanned", "Shortlisted")),
    ("technician", "Technician", "Reads price action, RVOL & trend", ("Analyzed", "Avg RVOL")),
    ("fundamentalist", "Fundamentalist", "Weighs valuation & analyst targets", ("Covered", "Avg upside")),
    ("newsdesk", "Newsdesk", "Pulls live news & scores sentiment", ("Headlines", "Net tone")),
    ("bull", "Bull", "Argues the case to buy", ("Cases", "Avg score")),
    ("bear", "Bear", "Argues the case against", ("Cases", "Avg score")),
    ("judge", "Judge", "Weighs the debate, issues verdict + confidence", ("Verdicts", "Buy")),
    ("messenger", "Messenger", "Sends signals to Telegram", ("Sent", "Engine")),
]

LOCK = threading.RLock()


def tg_configured():
    return bool((os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip() and (os.environ.get("TELEGRAM_CHAT_ID") or "").strip())


def new_state(mode="demo"):
    return {
        "state": "idle", "mode": mode, "run_id": None, "started_at": None, "finished_at": None, "error": None,
        "agents": [{"id": i, "name": n, "role": r, "icon": i, "status": "offline",
                    "stats": [{"label": l, "value": "–"} for l in labels]} for i, n, r, labels in AGENT_DEFS],
        "kpis": {"universe": 0, "in_debate": 0, "buy_signals": 0, "top_pick": None},
        "verdicts": [], "footer": None,
        "telegram": {"configured": tg_configured(), "sent": 0, "failed": 0, "note": ""},
        "log": [],
    }


S = new_state()


def log(msg):
    line = f"{datetime.now(IST):%H:%M:%S} {scrub(msg)}"
    print(line, flush=True)
    with LOCK:
        S["log"].append(line)
        del S["log"][:-80]


def set_agent(aid, status=None, v1=None, v2=None):
    with LOCK:
        a = next(x for x in S["agents"] if x["id"] == aid)
        if status:
            a["status"] = status
        if v1 is not None:
            a["stats"][0]["value"] = str(v1)
        if v2 is not None:
            a["stats"][1]["value"] = str(v2)


def avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def fmt(x, spec=".1f", suffix=""):
    return "–" if x is None else f"{x:{spec}}{suffix}"


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def tg_send(text):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        return False, "not configured"
    base = (os.environ.get("TELEGRAM_API_BASE") or "https://api.telegram.org").rstrip("/")
    try:
        r = requests.post(f"{base}/bot{token}/sendMessage", timeout=15,
                          json={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                                "disable_web_page_preview": True})
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 200 and body.get("ok"):
            return True, "sent"
        return False, scrub(f"HTTP {r.status_code}: {body.get('description', 'request failed')}")
    except Exception as e:
        return False, scrub(f"{type(e).__name__}: {e}")


def esc(x):
    return html.escape(str(x), quote=False)


def money(p):
    return "data unavailable" if p is None else f"₹{p:,.2f}"


def pct(c):
    return "data unavailable" if c is None else f"{c:+.2f}%"


def fmt_buy(r):
    cap = str(r["cap"] or "").capitalize()
    return (f"🟢 BUY SIGNAL — {esc(r['symbol'])} ({esc(cap)} cap)\n\n"
            f"Verdict: BUY | Confidence: {r['confidence']}/10\n\n"
            f"Winner: {esc(r['winner'])}\n\n"
            f"Why: {esc(r['why'])}\n\n"
            f"Key catalyst: {esc(r['catalyst'])}\n\n"
            f"Live price: {esc(money(r['price']))} | Day change: {esc(pct(r['day_change']))}\n\n"
            f"{DISCLAIMER}")


def fmt_summary(rows, fired, engine_label):
    date = datetime.now(IST).strftime("%d %b %Y")
    lines = [f"📊 Daily summary — {date} (IST)", "",
             f"Analyzed: {len(rows)} stocks | Engine: {esc(engine_label)}", ""]
    if fired:
        lines.append(f"BUY signals fired ({len(fired)}):")
        for r in fired:
            lines.append(f"• {esc(r['symbol'])} — {r['confidence']}/10 — {esc(money(r['price']))} ({esc(pct(r['day_change']))})")
    else:
        lines.append("Signals: no BUY signals fired")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# pipeline
# --------------------------------------------------------------------------
def safe_eval(engine, ev):
    try:
        return engine.evaluate(ev)
    except Exception as e:
        log(f"Engine error on {ev.get('symbol')}: {type(e).__name__}")
    try:
        res = scoring.evaluate(ev)
        res["engine"], res["engine_kind"], res["fallback_reason"] = "deterministic", "deterministic", "engine error"
        return res
    except Exception:
        zero = {"score": 0, "reasons": ["data unavailable"]}
        return {"scores": {k: dict(zero) for k in ("bull", "bear", "technician", "fundamentalist", "newsdesk")},
                "verdict": {"winner": "Bull", "verdict": "WATCH", "confidence": 1, "rationale": "data unavailable",
                            "key_catalyst": "data unavailable", "bull_score": 0, "bear_score": 0, "net": 0},
                "engine": "deterministic", "engine_kind": "deterministic", "fallback_reason": "evaluation failed",
                "grounding": {"ok": True, "flags": []}}


def make_row(ev, res, threshold):
    v = res["verdict"]
    price = ev.get("price") or {}
    return {
        "symbol": ev.get("symbol"), "name": ev.get("name"), "cap": ev.get("cap_segment"), "sector": ev.get("sector"),
        "verdict": v["verdict"], "confidence": v["confidence"], "winner": v["winner"], "why": v["rationale"],
        "catalyst": v["key_catalyst"], "price": price.get("live"), "day_change": price.get("day_change_pct"),
        "bull_score": v["bull_score"], "bear_score": v["bear_score"], "net": v["net"],
        "agent_points": res.get("scores", {}), "engine": res.get("engine"),
        "fallback_reason": res.get("fallback_reason"),
        "grounding_flags": (res.get("grounding") or {}).get("flags", []),
        "data_gaps": ev.get("data_gaps", []),
        "fired": v["verdict"] == "BUY" and v["confidence"] >= threshold, "sent": None,
    }


def cycle(mode):
    run_id = db_run_start(mode)
    with LOCK:
        S["run_id"] = run_id
    delay = cfg_float("AGENT_DELAY", 0.8)
    threshold = cfg_int("CONFIDENCE_THRESHOLD", 7)
    shortlist_n = cfg_int("SHORTLIST_PER_BUCKET", 4)
    engine = llm.Engine(log=log)
    messages_sent = 0
    try:
        log(f"Run #{run_id} started · mode={mode} · LLM providers: {', '.join(engine.candidates) or 'none (deterministic engine)'}")

        # 1) Scout - build the universe / load evidence
        set_agent("scout", "working")
        if mode == "live":
            bundles, scanned = data_sources.load_live(data_sources.load_universe(), shortlist_n, log=log)
        else:
            bundles = data_sources.load_demo(log=log)
            scanned = len(bundles)
        if not bundles:
            raise RuntimeError("No evidence bundles available (demo_data is empty?)")
        time.sleep(delay)
        with LOCK:
            S["kpis"]["universe"], S["kpis"]["in_debate"] = scanned, len(bundles)
        set_agent("scout", "done", scanned, len(bundles))

        # 2) Technician / Fundamentalist / Newsdesk - read the evidence
        set_agent("technician", "working"); time.sleep(delay)
        rv = [(b.get("technicals") or {}).get("rvol") for b in bundles]
        set_agent("technician", "done", len(bundles), fmt(avg(rv), ".1f", "x"))

        set_agent("fundamentalist", "working"); time.sleep(delay)
        ups = [(b.get("analyst") or {}).get("upside_pct") for b in bundles]
        covered = sum(1 for b in bundles if (b.get("analyst") or {}).get("target_mean") is not None)
        set_agent("fundamentalist", "done", covered, fmt(avg(ups), "+.1f", "%"))

        set_agent("newsdesk", "working"); time.sleep(delay)
        news = [b.get("news") or {} for b in bundles]
        heads = sum(n.get("total") or 0 for n in news)
        tone = sum((n.get("positive") or 0) - (n.get("negative") or 0) for n in news)
        set_agent("newsdesk", "done", heads, f"{tone:+d}")

        # 3) Debate: Bull -> Bear -> Judge
        set_agent("bull", "working"); time.sleep(delay * 0.5)
        set_agent("bear", "working"); time.sleep(delay * 0.5)
        set_agent("judge", "working")
        rows = []
        workers = max(1, cfg_int("LLM_CONCURRENCY", 3)) if engine.has_llm else 1
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(safe_eval, engine, b): b for b in bundles}
            for fut in as_completed(futs):
                ev, res = futs[fut], fut.result()
                row = make_row(ev, res, threshold)
                row["_vid"] = db_verdict(run_id, ev, row, res)
                rows.append(row)
                bulls = [r["bull_score"] for r in rows]
                bears = [r["bear_score"] for r in rows]
                buys = [r for r in rows if r["verdict"] == "BUY"]
                top = max(buys or rows, key=lambda r: (r["verdict"] == "BUY", r["confidence"], r["net"]))
                with LOCK:
                    S["verdicts"].insert(0, {k: v for k, v in row.items() if not k.startswith("_")})
                    S["kpis"]["buy_signals"] = sum(1 for r in rows if r["fired"])
                    S["kpis"]["top_pick"] = {"symbol": top["symbol"], "confidence": top["confidence"]} if top["verdict"] == "BUY" else None
                set_agent("bull", None, len(rows), fmt(avg(bulls), ".0f"))
                set_agent("bear", None, len(rows), fmt(avg(bears), ".0f"))
                set_agent("judge", None, len(rows), len(buys))
        order = {"BUY": 0, "WATCH": 1, "AVOID": 2}
        rows.sort(key=lambda r: (order[r["verdict"]], -r["confidence"], -r["net"]))
        with LOCK:
            S["verdicts"] = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
        for aid in ("bull", "bear", "judge"):
            time.sleep(delay * 0.4)
            set_agent(aid, "done")

        # 4) Messenger -> Telegram
        set_agent("messenger", "working")
        fired = [r for r in rows if r["fired"]]
        engine_label = engine.label()
        failed = 0
        if tg_configured():
            for r in fired:
                ok, why = tg_send(fmt_buy(r))
                r["sent"] = ok
                db_verdict_tg(r["_vid"], "sent" if ok else f"failed: {why}")
                messages_sent += ok
                failed += (not ok)
                if not ok:
                    log(f"Telegram send failed for {r['symbol']}: {why}")
                with LOCK:
                    for v in S["verdicts"]:
                        if v["symbol"] == r["symbol"]:
                            v["sent"] = ok
                time.sleep(1.1)
            ok, why = tg_send(fmt_summary(rows, fired, engine_label))
            messages_sent += ok
            failed += (not ok)
            if not ok:
                log(f"Telegram summary failed: {why}")
            note = f"{messages_sent} message(s) sent" + (f", {failed} failed" if failed else "")
        else:
            note = "Telegram not configured - signals shown here only (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env)"
            log(note)
            for r in fired:
                db_verdict_tg(r["_vid"], "skipped: not configured")
        with LOCK:
            S["telegram"].update({"configured": tg_configured(), "sent": messages_sent, "failed": failed, "note": note})
        time.sleep(delay * 0.4)
        set_agent("messenger", "done", messages_sent, engine.short_label())

        pulled = max((b.get("pulled_at") or "" for b in bundles), default="") or data_sources.now_ist()
        sample = any(str(b.get("source", "")).startswith("demo") for b in bundles)
        with LOCK:
            S["footer"] = {"n_stocks": len(bundles), "pulled_at": pulled, "engine": engine_label,
                           "mode": mode, "sample": sample}
            S["state"], S["finished_at"] = "done", now_str()
        db_run_finish(run_id, engine=engine_label, status="done", universe=scanned, shortlisted=len(bundles),
                      buys=len(fired), messages_sent=messages_sent, note=note)
        log(f"Run #{run_id} finished · {len(rows)} verdicts · {len(fired)} BUY signal(s) · engine: {engine_label}")
    except Exception as e:
        msg = scrub(f"{type(e).__name__}: {e}")
        print(scrub(traceback.format_exc()), flush=True)
        log(f"Run failed: {msg}")
        with LOCK:
            for a in S["agents"]:
                if a["status"] == "working":
                    a["status"] = "offline"
            S["state"], S["error"], S["finished_at"] = "error", msg, now_str()
        db_run_finish(run_id, status="error", note=msg, engine=engine.label())


# --------------------------------------------------------------------------
# Flask
# --------------------------------------------------------------------------
app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)


@app.get("/")
def index():
    return send_file(os.path.join(BASE, "dashboard.html"), max_age=0)


@app.get("/config")
def config():
    forced = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    providers = llm.detect_providers(forced)
    return jsonify({
        "brand": (os.environ.get("BRAND") or "").strip() or "Stock Agents",
        "n_agents": len(AGENT_DEFS), "modes": ["demo", "live"],
        "confidence_threshold": cfg_int("CONFIDENCE_THRESHOLD", 7),
        "shortlist_per_bucket": cfg_int("SHORTLIST_PER_BUCKET", 4),
        "agent_delay": cfg_float("AGENT_DELAY", 0.8),
        "telegram_configured": tg_configured(),
        "llm_providers": providers, "llm_forced": forced or None,
        "engine_hint": (f"{providers[0]} ({llm.model_for(providers[0])})" if providers else "deterministic"),
    })


@app.get("/status")
def status():
    with LOCK:
        S["telegram"]["configured"] = tg_configured()
        body = json.dumps(S)
    return Response(scrub(body), mimetype="application/json", headers={"Cache-Control": "no-store"})


@app.post("/start")
def start():
    global S
    data = request.get_json(silent=True) or {}
    mode = data.get("mode") if data.get("mode") in ("demo", "live") else "demo"
    with LOCK:
        if S["state"] == "running":
            return jsonify({"ok": False, "error": "A run is already in progress"}), 409
        S = new_state(mode)
        S["state"], S["started_at"] = "running", now_str()
    threading.Thread(target=cycle, args=(mode,), daemon=True).start()
    return jsonify({"ok": True, "mode": mode})


if __name__ == "__main__":
    init_db()
    port = cfg_int("PORT", 5000)
    url = f"http://127.0.0.1:{port}"
    print(f"Stock Agents dashboard: {url}   (Ctrl+C to stop)")
    if (os.environ.get("OPEN_BROWSER") or "1").strip() != "0":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
