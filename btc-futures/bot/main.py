"""BTC Futures Auto-Bot — main entrypoint.

Usage:
    python bot/main.py [--once] [--dry-run] [--no-agent]

Options:
    --once      Run one cycle then exit (useful for cron / testing)
    --dry-run   Override DRY_RUN=true for this session
    --no-agent  Skip agent subprocess, use local analysis only
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is importable
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env", override=False)
# Also try parent directory .env (monorepo layout)
load_dotenv(_ROOT.parent / ".env", override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("bot.main")


def _verify_leverage() -> None:
    """Check leverage on OKX matches config. Auto-adjust if different."""
    if os.getenv("DRY_RUN", "true").lower() == "true":
        return

    from bot import okx_private as okx
    from bot.telegram_bot import send_message_sync

    inst_id = os.getenv("BOT_SYMBOL", "BTC-USDT-SWAP")
    configured = int(os.getenv("LEVERAGE", "5"))

    try:
        current = okx.get_account_leverage(inst_id)
        if current is None:
            logger.warning("Could not read leverage from OKX — skipping verify")
            return
        if current != configured:
            logger.warning(
                "Leverage mismatch: OKX=%dx, config=%dx — adjusting",
                current, configured,
            )
            send_message_sync(
                f"Leverage mismatch: OKX={current}x vs config={configured}x — auto-adjusting."
            )
            okx.set_leverage(inst_id, configured)
    except Exception as exc:
        logger.error("Leverage verify failed: %s", exc)


def _get_current_price() -> float:
    """Fetch latest mark price (falls back to ticker)."""
    import okx_client
    inst_id = os.getenv("BOT_SYMBOL", "BTC-USDT-SWAP")
    symbol = inst_id.replace("-SWAP", "")
    try:
        mark = okx_client.get_mark_price(inst_id)
        return float(mark.get("markPx", 0))
    except Exception:
        try:
            ticker = okx_client.get_ticker(symbol)
            return float(ticker.get("last", 0))
        except Exception:
            return 0.0


def _get_balance(dry_run: bool) -> float:
    if dry_run:
        return float(os.getenv("ACCOUNT_BALANCE_USDT", "100"))
    from bot import okx_private as okx
    try:
        return okx.get_balance()
    except Exception as exc:
        logger.error("Could not fetch balance: %s", exc)
        return 0.0


def _run_analysis(symbol: str, use_agent: bool) -> dict | None:
    """Run multi-TF analysis + optional agent. Returns signal dict or None."""
    from commands.trade_agent import (
        TradeSignal,
        build_market_snapshot,
        build_prompt,
        check_agent_configured,
        local_fallback_signal,
        parse_agent_output,
        run_agent_subprocess,
    )
    from commands.trade import TIMEFRAMES, _confluence, _score_tf, _calc_levels
    import okx_client
    from concurrent.futures import ThreadPoolExecutor, as_completed

    swap_id = symbol if symbol.endswith("-SWAP") else symbol + "-SWAP"
    bare_symbol = symbol.replace("-SWAP", "")

    results: dict = {}

    def fetch(key, fn, *a, **kw):
        try:
            results[key] = fn(*a, **kw)
        except Exception as exc:
            logger.warning("Fetch %s failed: %s", key, exc)
            results[key] = None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [
            pool.submit(fetch, "15m",    okx_client.get_ohlcv, bare_symbol, "15m", 300),
            pool.submit(fetch, "1H",     okx_client.get_ohlcv, bare_symbol, "1H",  300),
            pool.submit(fetch, "4H",     okx_client.get_ohlcv, bare_symbol, "4H",  300),
            pool.submit(fetch, "1D",     okx_client.get_ohlcv, bare_symbol, "1D",  300),
            pool.submit(fetch, "fund",   okx_client.get_funding_rate, swap_id),
            pool.submit(fetch, "mark",   okx_client.get_mark_price,   swap_id),
        ]
        for f in as_completed(futs):
            pass

    for tf in TIMEFRAMES:
        if results.get(tf) is None or (hasattr(results[tf], "empty") and results[tf].empty):
            logger.error("Missing OHLCV for %s — skipping cycle", tf)
            return None

    tf_scores = {tf: _score_tf(results[tf]) for tf in TIMEFRAMES}
    confluence = _confluence(tf_scores)
    direction = confluence["signal"]

    current_price = float((results.get("mark") or {}).get("markPx", 0))
    if not current_price:
        logger.error("Could not determine current price — skipping cycle")
        return None

    atr_4h = tf_scores["4H"]["atr"]
    atr_1h = tf_scores["1H"]["atr"]
    atr_15m = tf_scores["15m"]["atr"]
    levels = _calc_levels(direction, current_price, atr_4h, atr_1h, atr_15m)

    # Confidence % numeric
    conf_map = {"HIGH": 90, "MEDIUM": 70, "LOW": 40}
    confidence_pct = conf_map.get(confluence["confidence"], 0) if direction != 0 else 0

    local_signal = {
        "direction": direction,
        "confidence": confidence_pct,
        "net_score": confluence["net_score"],
        "agreeing_tfs": confluence["agreeing_tfs"],
        "entry": levels["entry"],
        "sl": levels["sl"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "source": "local",
    }

    # Try agent
    if use_agent and check_agent_configured():
        try:
            snapshot = build_market_snapshot(tf_scores, confluence, levels,
                                             results.get("fund", {}), None)
            prompt = build_prompt(snapshot, bare_symbol)
            agent_output = run_agent_subprocess(prompt)
            parsed = parse_agent_output(agent_output, current_price)
            if parsed:
                return {
                    "direction": parsed.direction,
                    "confidence": int(parsed.confidence.replace("%", "")) if isinstance(parsed.confidence, str) else parsed.confidence,
                    "net_score": confluence["net_score"],
                    "agreeing_tfs": confluence["agreeing_tfs"],
                    "entry": parsed.entry,
                    "sl": parsed.sl,
                    "tp1": parsed.tp1,
                    "tp2": parsed.tp2,
                    "source": "agent",
                }
        except Exception as exc:
            logger.warning("Agent failed — falling back to local: %s", exc)

    return local_signal


def _signal_passes_filters(signal: dict) -> tuple[bool, str]:
    """Check all signal quality filters from BOT.md Section 6."""
    direction = signal.get("direction", 0)
    confidence = signal.get("confidence", 0)
    net_score = abs(signal.get("net_score", 0))
    agreeing_tfs = signal.get("agreeing_tfs", 0)
    entry = signal.get("entry", 0)
    sl = signal.get("sl", 0)

    if direction == 0:
        return False, "No direction (signal == 0)"

    min_conf = float(os.getenv("MIN_CONFIDENCE", "60"))
    if confidence < min_conf:
        return False, f"Confidence {confidence}% < {min_conf}%"

    min_score = float(os.getenv("MIN_NET_SCORE", "0.3"))
    if net_score < min_score:
        return False, f"|net_score| {net_score:.3f} < {min_score}"

    min_tfs = int(os.getenv("MIN_AGREEING_TF", "2"))
    if agreeing_tfs < min_tfs:
        return False, f"Only {agreeing_tfs} TFs agree (min {min_tfs})"

    min_sl_pct = float(os.getenv("MIN_SL_PCT", "0.3")) / 100
    if entry and sl:
        sl_pct = abs(entry - sl) / entry
        if sl_pct < min_sl_pct:
            return False, f"SL too close to entry ({sl_pct*100:.2f}% < {min_sl_pct*100:.2f}%)"

    return True, ""


def _bot_cycle(use_agent: bool = True) -> None:
    """One full bot cycle: fetch → check → act → report."""
    from bot import circuit_breaker, order_manager, pending_order, position_guard
    from bot.okx_errors import SkipCycleError
    from bot.state import load_state, reset_daily_if_needed, save_state
    from bot.telegram_bot import send_message_sync
    from bot import report as rpt

    now = datetime.now(tz=timezone.utc)
    logger.info("=== Cycle start %s ===", now.isoformat())

    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    inst_id = os.getenv("BOT_SYMBOL", "BTC-USDT-SWAP")
    symbol = inst_id.replace("-SWAP", "")

    # Load and reset daily counters
    state = load_state()
    state = reset_daily_if_needed(state)

    # Check if paused
    if state.get("bot_paused"):
        logger.info("Bot is paused — skipping cycle")
        return

    # Check if /close confirm was received from Telegram
    if state.get("pending_close_confirm") and state["position"]["active"]:
        logger.info("Manual close requested via Telegram")
        try:
            balance = _get_balance(dry_run)
            close_price = _get_current_price()
            pos = state["position"]
            open_time_str = pos.get("open_time")
            hold_hours = 0.0
            if open_time_str:
                from bot.pending_order import _parse_dt
                ot = _parse_dt(open_time_str)
                if ot:
                    hold_hours = (now - ot).total_seconds() / 3600

            state = order_manager.close_trade(state, reason="manual_close", current_price=close_price)
            msg = rpt.position_closed(
                pos.get("side", "?"), pos.get("entry_price", 0), close_price,
                pos.get("size_contracts", 0), hold_hours, balance, "manual_close", dry_run,
            )
            send_message_sync(msg)
        except Exception as exc:
            logger.error("Manual close failed: %s", exc)
            send_message_sync(f"Manual close FAILED: {exc}")
        state["pending_close_confirm"] = False
        save_state(state)
        return

    # ── Circuit breaker check ─────────────────────────────────────────────────
    balance = _get_balance(dry_run)
    if circuit_breaker.check_and_trigger(state, balance):
        cb_until = state.get("circuit_break_until", "?")
        msg = rpt.circuit_break_alert(
            state.get("daily_loss_usdt", 0), 0, float(os.getenv("MAX_DAILY_LOSS_PCT", "3")), str(cb_until)
        )
        send_message_sync(msg)
        save_state(state)
        logger.info("Circuit break active — skipping cycle")
        return

    # ── Fetch current price ───────────────────────────────────────────────────
    current_price = _get_current_price()
    if not current_price:
        logger.error("Cannot determine current price — skipping cycle")
        return

    # ── Sync position size (live vs state) ────────────────────────────────────
    if state["position"]["active"]:
        state = position_guard.sync_position_size(state)

    # ── Route: HAS POSITION ───────────────────────────────────────────────────
    if state["position"]["active"]:
        logger.info("Has position — managing")

        # Ensure algo TP/SL exists
        if not position_guard.has_algo_tp_sl(state):
            logger.warning("No algo TP/SL found — attaching")
            try:
                state = order_manager.attach_tp_sl(state)
            except Exception as exc:
                logger.error("attach_tp_sl failed: %s", exc)

        # Danger check
        try:
            is_dangerous, reasons = position_guard.evaluate_danger(state, current_price, balance)
        except Exception as exc:
            logger.error("evaluate_danger failed: %s", exc)
            is_dangerous, reasons = False, []

        if is_dangerous:
            send_message_sync(rpt.danger_alert(reasons, state["position"], current_price))
            pos_snap = dict(state["position"])
            open_time_str = pos_snap.get("open_time")
            hold_hours = 0.0
            if open_time_str:
                from bot.pending_order import _parse_dt
                ot = _parse_dt(open_time_str)
                if ot:
                    hold_hours = (now - ot).total_seconds() / 3600

            try:
                close_reason = "danger_" + "_".join(r.split()[0].lower() for r in reasons[:1])
                state = order_manager.close_trade(state, reason=close_reason, current_price=current_price)
                send_message_sync(rpt.position_closed(
                    pos_snap.get("side", "?"), pos_snap.get("entry_price", 0),
                    current_price, pos_snap.get("size_contracts", 0),
                    hold_hours, balance, close_reason, dry_run,
                ))
            except Exception as exc:
                logger.error("close_trade failed: %s", exc)
                send_message_sync(f"FAILED to close dangerous position: {exc}")
        else:
            # Safe — send status report
            try:
                import okx_client
                fund_data = okx_client.get_funding_rate(inst_id)
                funding_rate = float(fund_data.get("fundingRate", 0))
            except Exception:
                funding_rate = None
            send_message_sync(rpt.cycle_report(state, balance, current_price, funding_rate))

        save_state(state)
        return

    # ── Route: HAS PENDING ORDER ──────────────────────────────────────────────
    if state["pending_order"]["active"]:
        logger.info("Has pending order — managing")
        state = pending_order.manage(state, current_price)
        save_state(state)
        return

    # ── Route: IDLE — analyze and maybe place order ───────────────────────────
    logger.info("IDLE — running analysis")
    signal = _run_analysis(inst_id, use_agent=use_agent)
    if signal is None:
        logger.error("Analysis failed — no signal produced")
        save_state(state)
        return

    passes, reject_reason = _signal_passes_filters(signal)

    if not passes:
        logger.info("No trade — %s", reject_reason)
        next_run = _next_run_str()
        send_message_sync(rpt.no_trade(reject_reason, signal, next_run))
        state["last_action"] = "no_trade"
        state["last_signal"] = {
            "direction": signal.get("direction"),
            "confidence": signal.get("confidence"),
            "net_score": signal.get("net_score"),
            "source": signal.get("source"),
        }
        save_state(state)
        return

    # Place order
    logger.info("Signal passes filters — placing order (dry_run=%s)", dry_run)
    try:
        state = order_manager.open_trade(state, signal, balance)

        # Notify
        risk_pct = float(os.getenv("RISK_PCT", "1.0"))
        sl_pct = abs(signal["entry"] - signal["sl"]) / signal["entry"] if signal["entry"] else 0
        risk_usdt = balance * risk_pct / 100
        contracts = order_manager.calc_contracts(
            balance, risk_pct, signal["entry"], signal["sl"],
            int(os.getenv("LEVERAGE", "5"))
        )
        send_message_sync(rpt.order_placed(signal, contracts, risk_usdt,
                                           state["pending_order"].get("order_id", "?"), dry_run))
    except Exception as exc:
        logger.error("open_trade failed: %s", exc)
        send_message_sync(f"Order placement FAILED: {exc}")

    save_state(state)
    logger.info("=== Cycle end ===")


def _next_run_str() -> str:
    from datetime import timedelta
    hours = float(os.getenv("BOT_INTERVAL_HOURS", "2"))
    from datetime import timedelta
    nxt = datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    return nxt.strftime("%H:%M UTC")


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="BTC Futures Auto-Bot")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Force DRY_RUN=true")
    parser.add_argument("--no-agent", action="store_true", help="Skip agent subprocess")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"

    use_agent = not args.no_agent

    logger.info("BTC Futures Bot starting (dry_run=%s, agent=%s)", os.getenv("DRY_RUN"), use_agent)

    # ── Startup checks ────────────────────────────────────────────────────────
    _verify_leverage()

    # ── Startup reconciliation ────────────────────────────────────────────────
    from bot.state import load_state, save_state
    from bot.reconciler import reconcile
    state = load_state()
    state = reconcile(state)

    # ── Start Telegram bot ────────────────────────────────────────────────────
    from bot.telegram_bot import send_message_sync, start_bot

    def force_cycle():
        _bot_cycle(use_agent=use_agent)

    start_bot(state, on_cycle_trigger=force_cycle)
    send_message_sync("Bot started. DRY_RUN=" + os.getenv("DRY_RUN", "true"))

    # ── Run ───────────────────────────────────────────────────────────────────
    if args.once:
        _bot_cycle(use_agent=use_agent)
    else:
        from bot.scheduler import run_scheduler
        run_scheduler(lambda: _bot_cycle(use_agent=use_agent))


if __name__ == "__main__":
    main()
