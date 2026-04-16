#!/bin/sh
# Ensure state/log files exist before starting (prevents Docker creating them as directories)
touch /app/btc-futures/state.json
touch /app/btc-futures/state.json.bak
touch /app/btc-futures/bot.log

exec python bot/main.py "$@"
