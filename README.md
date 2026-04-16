# Vibe-Trading BTC Futures Bot

Automated trading bot for BTC-USDT perpetual swap on OKX, powered by a multi-timeframe AI agent for signal generation.

**How it works:** runs every 2 hours, analyzes confluence across 15m / 1H / 4H / 1D, calls the Gemini agent for deep analysis, then places limit orders with algo TP/SL — fully unattended.

**Safety features:** circuit breaker (max daily loss %), danger detection (5 conditions), startup reconciliation, dry-run mode, Telegram command interface.

> Full design documentation: [`btc-futures/BOT.md`](btc-futures/BOT.md)

---

## Requirements

- OKX account with API key (Futures trading enabled)
- Gemini API key (or another supported LLM provider)
- Telegram bot token + chat ID (for alerts and commands)
- Docker (recommended) or Python 3.11+

---

## Setup

```bash
git clone https://github.com/h1dr0nn/vibe-trading.git
cd vibe-trading

# Copy and fill in your credentials
cp btc-futures/.env.example .env
# Edit .env — set OKX_*, GEMINI_API_KEY, TELEGRAM_* variables
```

---

## Run with Docker (recommended)

```bash
# Start bot
docker compose -f docker-compose.bot.yml up -d --build

# Watch logs live
docker compose -f docker-compose.bot.yml logs -f btc-bot

# Stop
docker compose -f docker-compose.bot.yml stop btc-bot

# Restart after editing .env
docker compose -f docker-compose.bot.yml restart btc-bot
```

---

## Run locally

```bash
cd btc-futures

pip install -r ../agent/requirements.txt
pip install -r requirements.txt

# Dry-run one cycle (no real orders)
python bot/main.py --dry-run --once

# Live one cycle
python bot/main.py --once

# Run continuously (2h scheduler)
python bot/main.py
```

---

## Key .env variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DRY_RUN` | `true` | **Always start with true** — no real orders placed |
| `OKX_DEMO_MODE` | `false` | Use OKX paper trading endpoint |
| `OKX_API_KEY` | — | OKX API key |
| `OKX_SECRET_KEY` | — | OKX secret key |
| `OKX_API_PASSPHRASE` | — | OKX passphrase |
| `GEMINI_API_KEY` | — | Gemini (or other LLM) API key |
| `BOT_SYMBOL` | `BTC-USDT-SWAP` | Trading pair |
| `BOT_INTERVAL_HOURS` | `2` | Cycle interval in hours |
| `RISK_PCT` | `1.0` | % of balance risked per trade |
| `LEVERAGE` | `5` | Futures leverage |
| `MIN_CONFIDENCE` | `60` | Minimum signal confidence to open a trade |
| `MAX_DAILY_LOSS_PCT` | `3.0` | Circuit breaker threshold |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | — | Your Telegram chat ID |

See [`btc-futures/.env.example`](btc-futures/.env.example) for the full list.

---

## Telegram commands

| Command | Description |
|---------|-------------|
| `/status` | Balance, position, PnL |
| `/close` | Close position (2-step confirm) |
| `/pause` / `/resume` | Pause / resume bot cycles |
| `/analyze` | Trigger analysis immediately |
| `/pnl` | Today's realized PnL |
| `/dryrun on\|off` | Toggle dry-run mode |
| `/config` | Show current config |
| `/help` | List all commands |

---

## Project structure

```
Vibe-Trading/
├── agent/                      # AI agent core (signal generation)
│   ├── cli.py
│   └── src/
│       ├── agent/
│       ├── tools/
│       └── skills/
│
├── btc-futures/                # Bot
│   ├── bot/
│   │   ├── main.py             #   Entrypoint + main loop
│   │   ├── scheduler.py        #   APScheduler 2h cron
│   │   ├── okx_private.py      #   OKX authenticated REST API
│   │   ├── okx_errors.py       #   Error classification + retry
│   │   ├── state.py            #   Atomic state.json load/save
│   │   ├── circuit_breaker.py  #   Daily loss limit
│   │   ├── reconciler.py       #   Startup OKX <-> state sync
│   │   ├── order_manager.py    #   Place / close orders
│   │   ├── pending_order.py    #   Pending order lifecycle
│   │   ├── position_guard.py   #   Danger detection (5 conditions)
│   │   ├── telegram_bot.py     #   Commands + notifications
│   │   └── report.py           #   Message formatting
│   ├── commands/               #   Multi-TF analysis + agent bridge
│   ├── .env.example
│   ├── requirements.txt
│   └── BOT.md                  #   Full design document
│
├── Dockerfile.bot              # Bot container (build from repo root)
├── docker-compose.bot.yml
└── LICENSE
```

---

## Disclaimer

The BTC Futures Auto-Bot can execute real trades on OKX when `DRY_RUN=false`. Futures trading involves significant risk of loss, including loss of your entire capital. Always start with `DRY_RUN=true` and paper trading (`OKX_DEMO_MODE=true`) before enabling live trading. Past performance does not guarantee future results. Use at your own risk.

---

MIT License
