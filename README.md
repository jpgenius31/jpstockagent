# Stock Agents — local Indian-stock analysis dashboard

A one-click dashboard that runs on **your own machine** (no cloud backend). A panel of eight named agents screens Indian (NSE) stocks, an LLM panel debates each pick, and every **BUY** signal is sent to your Telegram.

> **Analysis only.** No orders are ever placed and there is no broker integration. Not investment advice.

## How to run

```bash
pip install -r requirements.txt
cp .env.example .env        # then edit .env (Telegram token + chat id)
python app.py               # opens http://127.0.0.1:5000
```

Click **Start agents**. The agents animate offline → working → done, the verdict feed fills in, BUY signals plus a daily summary go to Telegram, and everything is written to `audit.db` (SQLite).

### LLM without an API key (Claude subscription)
Install Claude Code, run `claude`, and `/login` with your Claude plan (Pro/Max). The app auto-detects the `claude` CLI and uses it (`claude -p ... --output-format json --model haiku`) — no API key, no per-call billing. To use an API instead, put `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` in `.env`.

Detection order: `claude_code` → `anthropic` → `openai`. Force one with `LLM_PROVIDER=claude_code|anthropic|openai` (or `none`). If no provider works (no CLI/login/key/network), the app silently uses the **deterministic rule engine**, which always works.

### Telegram
1. Message **@BotFather** → `/newbot` → copy the bot token.
2. Message **@userinfobot** → copy your chat id.
3. Put both in `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) and send your bot one message (e.g. `/start`) so it may write to you.

Without Telegram configured the dashboard still works; signals just stay on screen.

## Modes
| Mode | What it does |
|------|--------------|
| **demo** | Loads `demo_data/*.json` — fully offline. **The bundled files are sample data** (fictional companies, synthetic prices, flagged `"sample": true`), so no real ticker is ever shown with made-up numbers. |
| **live** | Pulls NSE data via `yfinance` for the tickers in `universe.json` (large / mid / small), screens each bucket by day-change and keeps the top `SHORTLIST_PER_BUCKET`. |

To replace the sample files with a real snapshot (useful for offline demos): `python data_sources.py --snapshot`.

## The agents
Scout (screens movers) → Technician (price action, RVOL, trend) → Fundamentalist (analyst targets) → Newsdesk (headlines + sentiment) → Bull / Bear (the debate) → Judge (verdict + confidence) → Messenger (Telegram).

A stock fires a signal when `verdict == BUY` and `confidence ≥ CONFIDENCE_THRESHOLD` (default 7).

## Evidence & grounding
Each stock becomes one normalised evidence bundle (price, 52-week range, technicals, analyst data, news, `data_gaps`). A value that can't be computed is `null` **and** named in `data_gaps`.

**This feed has no raw ratios such as P/E or ROE**; fundamentals are judged from analyst consensus, targets and the 52-week range only.

Grounding rule (both engines): every figure an agent cites must exist in the evidence bundle. `scoring.verify_grounding()` checks every number in the output; if an LLM answer cites an untraceable figure it is retried once, then replaced by the deterministic engine for that stock. Click a verdict row to see each agent's reasoning, engine used and data gaps.

### Deterministic engine (fallback)
* **Bull** (0–100): high RVOL (capped), 52-week position ≥ 85, price above a rising SMA, strong day-range close, analyst upside ≥ 10%, ≥ 80% Buy, positive news, positive window return.
* **Bear** (0–100): RVOL < 1, 52-week position < 30, below SMA / downtrend, no analyst headroom, < 55% Buy, ≥ 20% below the high, high Sell %, negative news, weak close.
* **Judge**: `net = bull − bear`. BUY if `net ≥ 25` and (52-week position ≥ 60 or RVOL ≥ 3); AVOID if `net ≤ −15`; else WATCH. Confidence = `clamp(round(4 + net/15), 1, 10)`, forced ≥ 7 for BUY and ≤ 6 otherwise.

## Configuration (`.env`)
| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | – | Signal delivery |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | – | Optional API providers |
| `LLM_PROVIDER` | auto | `claude_code`, `anthropic`, `openai` or `none` |
| `CLAUDE_MODEL` / `ANTHROPIC_MODEL` / `OPENAI_MODEL` | `haiku` / `claude-haiku-5-5` / `gpt-4o-mini` | Model choice |
| `BRAND` | Stock Agents | Header name |
| `CONFIDENCE_THRESHOLD` | 7 | Minimum confidence to fire a BUY |
| `SHORTLIST_PER_BUCKET` | 4 | Live mode: stocks kept per cap bucket |
| `AGENT_DELAY` | 0.8 | Seconds of visual pacing between agents |
| `PORT` | 5000 | Local port |
| `LLM_CONCURRENCY`, `LLM_TIMEOUT`, `OPEN_BROWSER` | 3, 180, 1 | Parallel LLM calls, per-call timeout (s), auto-open browser |

## Security notes
* `.env` is read locally; secrets are never hardcoded. The Telegram token (and API keys) are scrubbed from logs, the UI, `/status` and the audit DB.
* The server binds to `127.0.0.1` only.
* The `claude` CLI is run with no stdin, in an empty temporary directory, and your `ANTHROPIC_API_KEY` is removed from its environment so it uses your subscription login. News headlines are untrusted text: they are length-limited, the model is told to treat them as data, and the model's output is only parsed as JSON — it cannot trigger trades or messages beyond the fixed signal format.
* On Windows, multi-line prompts are flattened to one line for the CLI (`.cmd` shims can't take newlines); the native `claude.exe` is recommended.

## Files
`app.py` (server, state machine, Telegram, SQLite) · `scoring.py` (deterministic agents, Judge, grounding verifier) · `llm.py` (LLM debate, provider detection, fallback) · `data_sources.py` (demo loader, yfinance adapter, evidence builder) · `dashboard.html` (UI) · `universe.json` (editable tickers) · `demo_data/` (sample bundles) · `audit.db` (created on first run: tables `runs`, `verdicts`).

## Troubleshooting
* **Live mode error** — yfinance can be rate-limited or offline; try again later or use demo mode.
* **Engine shows `deterministic`** — no `claude` login / API key worked. Run `claude` once and `/login`, or set a key.
* **No Telegram message** — check token/chat id, and that you messaged the bot first; failures are shown in the run note (token redacted).
