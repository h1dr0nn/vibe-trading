"""Telegram integration: send messages and handle commands.

Commands handled:
  /status   — current state, balance, PnL
  /analyze  — run analysis without placing order
  /close    — close position (2-step confirm)
  /close confirm — execute close
  /pause    — pause bot (no new orders)
  /resume   — resume bot
  /pnl      — today's realized PnL
  /config   — show config (no secrets)
  /dryrun on|off — toggle dry run
  /help     — list commands

Uses python-telegram-bot v20+ (async ApplicationBuilder).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Shared state reference (set by main.py on startup)
_state_ref: dict[str, Any] | None = None
_state_lock = threading.Lock()

# Close confirmation expiry tracking
_close_confirm_expiry: datetime | None = None
_CLOSE_CONFIRM_TTL = 60  # seconds


def set_state_ref(state: dict[str, Any]) -> None:
    """Register the live state dict for command handlers to read/write."""
    global _state_ref
    _state_ref = state


# ── Sync send (used during startup / reconcile before bot loop starts) ────────

def send_message_sync(text: str) -> None:
    """Send a Telegram message synchronously (blocking).

    Falls back silently if Telegram is not configured.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.debug("Telegram not configured — skipping message: %s", text[:80])
        return

    try:
        import requests as _req
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = _req.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Telegram send failed: %s", resp.text[:200])
    except Exception as exc:
        logger.warning("Telegram send_message_sync error: %s", exc)


# ── Async application (runs in background thread) ─────────────────────────────

_app = None
_bot_thread: threading.Thread | None = None


def start_bot(state: dict[str, Any], on_cycle_trigger: Any = None) -> None:
    """Start the Telegram bot in a background daemon thread.

    `on_cycle_trigger` is an optional callable() that forces the bot loop to
    run immediately (used by /analyze).
    """
    global _bot_thread
    set_state_ref(state)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — bot commands disabled")
        return

    _bot_thread = threading.Thread(
        target=_run_bot_loop,
        args=(token, on_cycle_trigger),
        daemon=True,
        name="telegram-bot",
    )
    _bot_thread.start()
    logger.info("Telegram bot started in background thread")


def _run_bot_loop(token: str, on_cycle_trigger: Any) -> None:
    """Entry point for the background thread.

    Uses explicit initialize/start/stop lifecycle instead of run_polling() to
    avoid the "Cannot close a running event loop" issue that python-telegram-bot
    >=20 hits when run_polling() is invoked inside a non-main thread.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_start_application(token, on_cycle_trigger))
    except Exception as exc:
        logger.error("Telegram bot loop crashed: %s", exc, exc_info=True)
    finally:
        try:
            loop.close()
        except Exception:
            pass


async def _start_application(token: str, on_cycle_trigger: Any) -> None:
    from telegram import Update
    from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

    app = ApplicationBuilder().token(token).build()

    # ── Command handlers ──────────────────────────────────────────────────────

    async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        state = _get_state()
        if state is None:
            await update.message.reply_text("Bot not ready yet.")
            return
        try:
            import okx_client
            symbol = os.getenv("BOT_SYMBOL", "BTC-USDT-SWAP").replace("-SWAP", "")
            ticker = okx_client.get_ticker(symbol)
            price = float(ticker.get("last", 0))
            balance = _get_balance(state)
        except Exception:
            price = 0.0
            balance = 0.0
        from bot.report import status_report
        await update.message.reply_text(status_report(state, balance, price), parse_mode="HTML")

    async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        await update.message.reply_text("🔄 <b>Running analysis…</b>\n<i>Report will follow shortly.</i>", parse_mode="HTML")
        if on_cycle_trigger:
            threading.Thread(target=on_cycle_trigger, daemon=True).start()

    async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        global _close_confirm_expiry
        if not _authorized(update):
            return

        args = ctx.args or []
        state = _get_state()
        if state is None:
            await update.message.reply_text("Bot not ready.")
            return

        # Two-step: /close confirm
        if args and args[0].lower() == "confirm":
            if _close_confirm_expiry is None or datetime.now(tz=timezone.utc) > _close_confirm_expiry:
                await update.message.reply_text("⏱ Confirmation expired. Send /close first.")
                return
            if not state["position"]["active"]:
                await update.message.reply_text("⚪ No open position to close.")
                _close_confirm_expiry = None
                return

            from bot.state import save_state as _save_state
            state["pending_close_confirm"] = True
            _save_state(state)
            _close_confirm_expiry = None
            await update.message.reply_text("✅ <b>Close confirmed</b> — executing now…", parse_mode="HTML")
            return

        # First step: prompt for confirmation
        if not state["position"]["active"]:
            await update.message.reply_text("⚪ No open position to close.")
            return

        try:
            import okx_client
            symbol = os.getenv("BOT_SYMBOL", "BTC-USDT-SWAP").replace("-SWAP", "")
            ticker = okx_client.get_ticker(symbol)
            price = float(ticker.get("last", 0))
        except Exception:
            price = 0.0

        from bot.report import close_confirm_prompt
        from datetime import timedelta
        _close_confirm_expiry = datetime.now(tz=timezone.utc) + timedelta(seconds=_CLOSE_CONFIRM_TTL)
        await update.message.reply_text(close_confirm_prompt(state["position"], price), parse_mode="HTML")

    async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        from bot.state import load_state, save_state
        state = load_state()
        state["bot_paused"] = True
        save_state(state)
        await update.message.reply_text(
            "⏸ <b>Bot paused</b>\n<i>No new orders. Send /resume to continue.</i>",
            parse_mode="HTML",
        )

    async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        from bot.state import load_state, save_state
        state = load_state()
        state["bot_paused"] = False
        save_state(state)
        await update.message.reply_text("▶️ <b>Bot resumed</b>", parse_mode="HTML")

    async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        state = _get_state()
        if state is None:
            await update.message.reply_text("Bot not ready.")
            return
        realized = state.get("daily_realized_pnl", 0)
        trades = state.get("daily_trades", 0)
        emoji = "✅" if realized > 0 else "❌" if realized < 0 else "➖"
        sign = "+" if realized > 0 else ""
        await update.message.reply_text(
            f"📊 <b>Today's PnL</b>\n\n"
            f"Realized: <b>{sign}${realized:,.4f}</b> USDT {emoji}\n"
            f"Trades: <b>{trades}</b>",
            parse_mode="HTML",
        )

    async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        groups = [
            ("⚙️ Bot", ["BOT_SYMBOL", "BOT_INTERVAL_HOURS", "DRY_RUN"]),
            ("💰 Sizing", ["RISK_PCT", "LEVERAGE", "ORDER_TYPE"]),
            ("🎲 Signal", ["MIN_CONFIDENCE", "MIN_NET_SCORE", "MIN_AGREEING_TF"]),
            ("🎯 TP/SL", ["SL_ATR_MULT", "TP1_ATR_MULT", "TP2_ATR_MULT"]),
            ("🛡 Risk", ["MAX_DAILY_LOSS_PCT", "MAX_OPEN_HOURS", "DANGER_SL_ATR_MULT", "DANGER_LOSS_PCT"]),
            ("⏱ Pending", ["PENDING_CANCEL_HOURS", "PENDING_CANCEL_DRIFT_PCT"]),
        ]
        lines = ["<b>⚙️ Config</b>"]
        for title, keys in groups:
            lines.append("")
            lines.append(f"<b>{title}</b>")
            for k in keys:
                v = os.getenv(k, "<i>(not set)</i>")
                lines.append(f"  <code>{k}</code>: <b>{v}</b>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def cmd_dryrun(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        args = ctx.args or []
        if not args or args[0].lower() not in ("on", "off"):
            await update.message.reply_text("Usage: <code>/dryrun on|off</code>", parse_mode="HTML")
            return
        value = "true" if args[0].lower() == "on" else "false"
        os.environ["DRY_RUN"] = value
        emoji = "⚠️" if value == "true" else "🚀"
        await update.message.reply_text(
            f"{emoji} DRY_RUN set to <b>{value}</b>.", parse_mode="HTML"
        )

    async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            "<b>🤖 Bot Commands</b>\n\n"
            "📊 /status  — balance, position, PnL\n"
            "🔄 /analyze — run analysis now\n"
            "🛑 /close   — close position (2-step confirm)\n"
            "⏸ /pause   — pause bot\n"
            "▶️ /resume  — resume bot\n"
            "💰 /pnl     — today's realized PnL\n"
            "⚙️ /config  — show current config\n"
            "🎚 /dryrun on|off — toggle dry run\n"
            "ℹ️ /help    — this message"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    # Register handlers
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("dryrun", cmd_dryrun))
    app.add_handler(CommandHandler("help", cmd_help))

    logger.info("Telegram command handlers registered")
    # Manual lifecycle — avoids asyncio conflicts with background-thread loop
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    # Block forever; background thread is daemon, exits when main process exits
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_state() -> dict[str, Any] | None:
    """Always reload state from disk so Telegram commands see fresh cycle updates.

    The `_state_ref` is kept only as a fallback if disk read fails.
    """
    try:
        from bot.state import load_state
        return load_state()
    except Exception as exc:
        logger.warning("load_state failed, using in-memory ref: %s", exc)
        return _state_ref


def _authorized(update: Any) -> bool:
    """Only accept messages from the configured chat ID."""
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        return True  # No restriction configured
    msg_chat_id = str(update.effective_chat.id)
    if msg_chat_id != str(chat_id):
        logger.warning("Rejected message from unauthorized chat %s", msg_chat_id)
        return False
    return True


def _get_balance(state: dict[str, Any]) -> float:
    if os.getenv("DRY_RUN", "true").lower() == "true":
        return float(os.getenv("ACCOUNT_BALANCE_USDT", "100"))
    try:
        from bot import okx_private as okx
        return okx.get_balance()
    except Exception:
        return 0.0
