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

def _setup_logging() -> None:
    """Color-coded console via Rich + plain file log.

    Third-party libs (httpx, telegram, apscheduler) flood INFO with polling
    noise that buries real bot events (and leaks the bot token in URLs).
    They're pinned at WARNING so only bot.* chatter shows at INFO.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Clear any pre-existing handlers (idempotent re-import safe)
    for h in list(root.handlers):
        root.removeHandler(h)

    # Console: WARNING+ only — the dashboard (console_ui) handles normal
    # INFO events as Rich panels. INFO lines would just spam under panels.
    try:
        from rich.logging import RichHandler
        console_handler: logging.Handler = RichHandler(
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
            markup=False,
            log_time_format="[%H:%M:%S]",
        )
        console_handler.setFormatter(logging.Formatter("%(name)s — %(message)s"))
    except ImportError:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
            datefmt="%H:%M:%S",
        ))
    console_handler.setLevel(logging.WARNING)

    # File: plain + full timestamps, easy to grep
    file_handler = logging.FileHandler("bot.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    ))

    root.addHandler(console_handler)
    root.addHandler(file_handler)

    # Silence noisy libraries (still captured at WARNING+)
    for name in ("httpx", "httpcore", "telegram", "telegram.ext",
                 "apscheduler", "apscheduler.executors.default",
                 "apscheduler.scheduler", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


_setup_logging()
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
            pool.submit(fetch, "fund_h", okx_client.get_funding_rate_history, swap_id, 21),
            pool.submit(fetch, "mark",   okx_client.get_mark_price,   swap_id),
            pool.submit(fetch, "spot",   okx_client.get_ticker,       bare_symbol),
            pool.submit(fetch, "oi",     okx_client.get_open_interest, swap_id),
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

    # Regime filter — short-circuit choppy markets (trend-follow fails)
    if os.getenv("REGIME_FILTER", "true").lower() == "true":
        from bot import regime as _regime
        regime_info = _regime.classify(tf_scores)
        if regime_info.get("block_entry"):
            logger.info("Regime filter blocked: %s — %s",
                        regime_info.get("regime"), regime_info.get("reason"))
            # Return a forced-zero signal so downstream logs "No trade"
            return {
                "direction": 0,
                "confidence": 0,
                "net_score": confluence["net_score"],
                "agreeing_tfs": confluence["agreeing_tfs"],
                "tf_breakdown": {
                    tf: {
                        "signal": tf_scores[tf]["signal"],
                        "score": tf_scores[tf]["score"],
                        "rsi": tf_scores[tf]["rsi"],
                        "adx": tf_scores[tf]["adx"],
                        "ema_cross": tf_scores[tf]["ema_cross"],
                        "atr": tf_scores[tf]["atr"],
                    } for tf in TIMEFRAMES
                },
                "entry": 0, "sl": 0, "tp1": 0, "tp2": 0,
                "source": "local",
                "reasoning": f"Regime filter: {regime_info.get('regime')} — {regime_info.get('reason')}",
                "regime": regime_info,
            }

    current_price = float((results.get("mark") or {}).get("markPx", 0))
    if not current_price:
        logger.error("Could not determine current price — skipping cycle")
        return None

    atr_4h = tf_scores["4H"]["atr"]
    atr_1h = tf_scores["1H"]["atr"]
    atr_15m = tf_scores["15m"]["atr"]
    levels = _calc_levels(direction, current_price, atr_4h, atr_1h, atr_15m)

    # Confidence % numeric
    # Granular confidence: blend confluence label + net_score magnitude + TF agreement
    # net_score is in [-3, +3] (sum of 3 TF scores weighted). Take |net| / 3 as 0..1 scale.
    if direction == 0:
        confidence_pct = 0
    else:
        base_map = {"HIGH": 70, "MEDIUM": 55, "LOW": 40}
        base = base_map.get(confluence["confidence"], 40)
        net_mag = min(abs(confluence.get("net_score", 0)) / 3.0, 1.0)
        agree_bonus = max(0, confluence.get("agreeing_tfs", 0) - 1) * 5  # 0/5/10
        confidence_pct = int(min(95, base + net_mag * 20 + agree_bonus))

    # Per-TF breakdown for reporting — keeps only the fields the UI needs.
    # ATR is kept so downstream filters (pullback gate) can compute distances
    # without re-fetching OHLCV.
    tf_breakdown = {
        tf: {
            "signal": tf_scores[tf]["signal"],
            "score": tf_scores[tf]["score"],
            "rsi": tf_scores[tf]["rsi"],
            "adx": tf_scores[tf]["adx"],
            "ema_cross": tf_scores[tf]["ema_cross"],
            "atr": tf_scores[tf]["atr"],
        }
        for tf in TIMEFRAMES
    }

    # Local reasoning: one-liner summarising confluence state
    local_reasoning = (
        f"Multi-TF confluence {confluence.get('confidence','?')}: "
        f"{confluence.get('agreeing_tfs',0)}/3 big TFs agree "
        f"(net={confluence.get('net_score',0):+.2f})"
    )

    local_signal = {
        "direction": direction,
        "confidence": confidence_pct,
        "net_score": confluence["net_score"],
        "agreeing_tfs": confluence["agreeing_tfs"],
        "tf_breakdown": tf_breakdown,
        "entry": levels["entry"],
        "sl": levels["sl"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "source": "local",
        "reasoning": local_reasoning,
    }

    # Try agent
    agent_ok, agent_err = check_agent_configured() if use_agent else (False, "")
    if use_agent and agent_ok:
        try:
            from commands.funding import _classify_regime
            fund_raw = results.get("fund") or {}
            fund_hist = results.get("fund_h") or []
            rate_8h = float(fund_raw.get("fundingRate", 0))
            hist_rates = [float(h["fundingRate"]) for h in fund_hist]
            funding_data = {
                "rate_8h": rate_8h,
                "ann_pct": rate_8h * 3 * 365 * 100,
                "avg_7d": sum(hist_rates) / len(hist_rates) if hist_rates else 0.0,
                "regime": _classify_regime(hist_rates),
            }
            oi_raw = results.get("oi") or {}
            oi_data = {"oi_usd_b": float(oi_raw.get("oiUsd", 0)) / 1e9}
            mark_price = float((results.get("mark") or {}).get("markPx", current_price))
            spot = results.get("spot") or {}
            open_24h = float(spot.get("open24h", current_price))
            change_24h = (current_price - open_24h) / open_24h * 100 if open_24h else 0

            snapshot = build_market_snapshot(
                tf_scores, confluence, funding_data, oi_data,
                current_price, mark_price, change_24h,
            )
            prompt = build_prompt(snapshot, current_price)
            agent_result = run_agent_subprocess(prompt)
            if agent_result.get("status") != "success":
                logger.warning("Agent returned non-success: %s", agent_result.get("error", "?"))
                return local_signal
            parsed = parse_agent_output(agent_result.get("content", ""), current_price)
            if parsed:
                # Map HIGH/MEDIUM/LOW to numeric if agent returned string
                if isinstance(parsed.confidence, str):
                    conf_map = {"HIGH": 90, "MEDIUM": 70, "LOW": 50}
                    # strip % suffix if any
                    cstr = parsed.confidence.replace("%", "").strip().upper()
                    try:
                        conf_num = int(cstr)
                    except ValueError:
                        conf_num = conf_map.get(cstr, 50)
                else:
                    conf_num = parsed.confidence

                agent_signal = {
                    "direction": parsed.direction,
                    "confidence": conf_num,
                    "net_score": confluence["net_score"],
                    "agreeing_tfs": confluence["agreeing_tfs"],
                    "tf_breakdown": tf_breakdown,
                    "entry": parsed.entry,
                    "sl": parsed.sl,
                    "tp1": parsed.tp1,
                    "tp2": parsed.tp2,
                    "source": "agent",
                    "reasoning": (parsed.agent_reasoning or "").strip()[:400],
                }
                # Validate agent levels — fall back to local if unreasonable
                return _validate_agent_signal(agent_signal, local_signal, current_price)
        except Exception as exc:
            logger.warning("Agent failed — falling back to local: %s", exc)

    return local_signal


def _validate_agent_signal(agent_signal: dict, local_signal: dict, current_price: float) -> dict:
    """Sanity-check agent entry/SL/TP. Fall back to local levels if off.

    Keeps agent direction + reasoning regardless — only replaces prices
    when they violate SL bounds or R:R requirements.
    """
    if agent_signal.get("direction") == 0:
        return agent_signal  # NO TRADE — nothing to validate

    entry = float(agent_signal.get("entry", 0))
    sl = float(agent_signal.get("sl", 0))
    tp1 = float(agent_signal.get("tp1", 0))

    if entry <= 0 or sl <= 0 or tp1 <= 0:
        logger.warning("Agent signal has zero levels — using local levels")
        return _patch_levels(agent_signal, local_signal)

    sl_pct = abs(entry - sl) / entry
    min_sl_pct = float(os.getenv("MIN_SL_PCT", "0.3")) / 100
    max_sl_pct = float(os.getenv("SL_PCT_MAX", "0.5")) / 100

    if sl_pct < min_sl_pct:
        logger.warning(
            "Agent SL too tight (%.3f%% < %.3f%%) — patching with local levels",
            sl_pct * 100, min_sl_pct * 100,
        )
        return _patch_levels(agent_signal, local_signal)
    if sl_pct > max_sl_pct:
        logger.warning(
            "Agent SL too wide (%.3f%% > %.3f%%) — patching with local levels",
            sl_pct * 100, max_sl_pct * 100,
        )
        return _patch_levels(agent_signal, local_signal)

    rr1 = abs(tp1 - entry) / abs(entry - sl)
    if rr1 < 1.0:
        logger.warning("Agent R:R at TP1 = %.2f < 1 — patching TP from local", rr1)
        agent_signal["tp1"] = local_signal["tp1"]
        agent_signal["tp2"] = local_signal["tp2"]
        agent_signal["reasoning"] = (
            agent_signal.get("reasoning", "") + f" [TP patched: R:R was {rr1:.2f}]"
        ).strip()

    # Direction vs entry sanity — LONG entry must be < tp1 for buy, etc.
    dir_ = agent_signal["direction"]
    if dir_ == 1 and (entry >= tp1 or entry <= sl):
        return _patch_levels(agent_signal, local_signal)
    if dir_ == -1 and (entry <= tp1 or entry >= sl):
        return _patch_levels(agent_signal, local_signal)

    return agent_signal


def _patch_levels(agent_signal: dict, local_signal: dict) -> dict:
    """Copy entry/sl/tp1/tp2 from local into agent signal; keep direction + reasoning."""
    agent_signal = dict(agent_signal)
    for k in ("entry", "sl", "tp1", "tp2"):
        agent_signal[k] = local_signal.get(k, agent_signal.get(k))
    agent_signal["source"] = "agent+local_levels"
    note = "[levels fell back to local due to agent bounds violation]"
    prev = agent_signal.get("reasoning", "") or ""
    if note not in prev:
        agent_signal["reasoning"] = (prev + " " + note).strip()
    return agent_signal


def _signal_passes_filters(signal: dict, state: dict | None = None) -> tuple[bool, str]:
    """Check all signal quality filters. Returns (ok, reject_reason)."""
    direction = signal.get("direction", 0)
    confidence = signal.get("confidence", 0)
    net_score = abs(signal.get("net_score", 0))
    agreeing_tfs = signal.get("agreeing_tfs", 0)
    entry = signal.get("entry", 0)
    sl = signal.get("sl", 0)
    tfb = signal.get("tf_breakdown") or {}

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

    # ── 15m veto: block entries fighting current 15m momentum ────────────────
    if os.getenv("FIFTEEN_MIN_VETO", "true").lower() == "true":
        s15 = tfb.get("15m") or {}
        s15_sig = s15.get("signal", 0)
        s15_score = s15.get("score", 0)
        oppose_mag = float(os.getenv("FIFTEEN_MIN_VETO_SCORE", "0.3"))
        if s15_sig == -direction:
            return False, (
                f"15m veto: 15m signal {'SHORT' if s15_sig==-1 else 'LONG'} "
                f"opposes {'LONG' if direction==1 else 'SHORT'} (score {s15_score:+.2f})"
            )
        if direction == 1 and s15_score <= -oppose_mag:
            return False, f"15m veto: score {s15_score:+.2f} < -{oppose_mag}"
        if direction == -1 and s15_score >= oppose_mag:
            return False, f"15m veto: score {s15_score:+.2f} > +{oppose_mag}"

    # ── RSI guard: don't chase overbought/oversold on primary entry TF (1H) ──
    # 15m is too noisy — a spike to 75 on 15m often resolves in one candle.
    # 1H RSI ≥ 72 is a more reliable overbought signal for swing entries.
    if os.getenv("RSI_GUARD", "true").lower() == "true":
        s1h = tfb.get("1H") or {}
        rsi = s1h.get("rsi", 50)
        rsi_ob = float(os.getenv("RSI_OVERBOUGHT", "72"))
        rsi_os = float(os.getenv("RSI_OVERSOLD", "28"))
        if direction == 1 and rsi > rsi_ob:
            return False, f"RSI guard: 1H RSI {rsi:.0f} > {rsi_ob:.0f} (overbought)"
        if direction == -1 and rsi < rsi_os:
            return False, f"RSI guard: 1H RSI {rsi:.0f} < {rsi_os:.0f} (oversold)"

    # ── Pullback gate: require retrace after same-direction TP ───────────────
    if state is not None and os.getenv("PULLBACK_GATE", "true").lower() == "true":
        gate_ok, gate_reason = _pullback_gate_check(state, signal, tfb)
        if not gate_ok:
            return False, gate_reason

    # ── Loss cooldown: block same-direction re-entry after a losing close ────
    if state is not None:
        cooldown_ok, cooldown_reason = _loss_cooldown_check(state, signal)
        if not cooldown_ok:
            return False, cooldown_reason

    return True, ""


def _pullback_gate_check(state: dict, signal: dict, tfb: dict) -> tuple[bool, str]:
    """Block same-direction re-entry unless price retraced from last TP.

    Required pullback decays linearly over PULLBACK_WINDOW_HOURS: full at
    t=0 (right after TP), zero at t=window. Both ATR and % thresholds are
    evaluated; the tighter one binds and is named in the reject message so
    it is clear which constraint is active.
    """
    last_tp = state.get("last_tp_close") or {}
    if not last_tp:
        return True, ""

    from bot.pending_order import _parse_dt

    window_h = float(os.getenv("PULLBACK_WINDOW_HOURS", "12"))
    ts = _parse_dt(last_tp.get("time"))
    if ts is None:
        return True, ""
    elapsed_h = (datetime.now(tz=timezone.utc) - ts).total_seconds() / 3600
    if elapsed_h >= window_h:
        return True, ""  # window expired → no gate

    last_side = last_tp.get("side")
    last_close = float(last_tp.get("close_price", 0))
    direction = signal.get("direction", 0)
    entry = float(signal.get("entry", 0))
    want_long = direction == 1
    was_long = last_side == "long"
    if want_long != was_long or not entry or not last_close:
        return True, ""  # different direction → no gate

    # Linear decay: 1.0 at t=0, 0.0 at t=window_h.
    decay = max(0.0, 1.0 - elapsed_h / window_h)

    k_atr = float(os.getenv("PULLBACK_REQUIRE_ATR", "0.3"))
    k_pct = float(os.getenv("PULLBACK_REQUIRE_PCT", "0.3")) / 100
    atr_1h = float((tfb.get("1H") or {}).get("atr", 0) or 0)

    req_atr = k_atr * atr_1h * decay if atr_1h > 0 else 0.0
    req_pct = last_close * k_pct * decay

    # Binding constraint = the tighter (larger) of the two distances.
    if req_atr >= req_pct and req_atr > 0:
        required = req_atr
        basis = f"{k_atr}×ATR1H (${atr_1h:,.0f}) × decay {decay:.2f} = ${required:,.0f}"
    elif req_pct > 0:
        required = req_pct
        basis = f"{k_pct*100:.2f}% × decay {decay:.2f} = ${required:,.0f}"
    else:
        return True, ""  # no valid requirement

    head = f"Pullback gate [{elapsed_h:.1f}h/{window_h:.0f}h]"
    if want_long and entry > last_close - required:
        threshold = last_close - required
        return False, (
            f"{head}: LONG entry ${entry:,.0f} > threshold ${threshold:,.0f} "
            f"(last TP ${last_close:,.0f} − {basis})"
        )
    if not want_long and entry < last_close + required:
        threshold = last_close + required
        return False, (
            f"{head}: SHORT entry ${entry:,.0f} < threshold ${threshold:,.0f} "
            f"(last TP ${last_close:,.0f} + {basis})"
        )
    return True, ""


def _loss_cooldown_check(state: dict, signal: dict) -> tuple[bool, str]:
    """Block same-direction re-entry within LOSS_COOLDOWN_MINUTES of a loss.

    Prevents revenge re-entries when a SL / danger-close / reconcile loss
    just happened. Opposite-direction entries are allowed — the cooldown
    only binds when the bot wants to repeat the side that just lost.
    """
    last_loss = state.get("last_loss_close") or {}
    if not last_loss:
        return True, ""

    cooldown_min = float(os.getenv("LOSS_COOLDOWN_MINUTES", "60"))
    if cooldown_min <= 0:
        return True, ""

    from bot.pending_order import _parse_dt
    ts = _parse_dt(last_loss.get("time"))
    if ts is None:
        return True, ""

    elapsed_min = (datetime.now(tz=timezone.utc) - ts).total_seconds() / 60
    if elapsed_min >= cooldown_min:
        return True, ""

    last_side = last_loss.get("side")
    direction = signal.get("direction", 0)
    same_dir = (direction == 1 and last_side == "long") or \
               (direction == -1 and last_side == "short")
    if not same_dir:
        return True, ""

    last_price = float(last_loss.get("close_price", 0) or 0)
    return False, (
        f"Loss cooldown: {elapsed_min:.0f}m/{cooldown_min:.0f}m since losing "
        f"{(last_side or '?').upper()} @ ${last_price:,.0f}"
    )


import threading as _threading
_cycle_lock = _threading.Lock()


def _bot_cycle(use_agent: bool = True, position_only: bool = False) -> None:
    """One full bot cycle: fetch → check → act → report.

    Guarded by a process-wide lock so scheduler + /analyze + external triggers
    cannot run concurrently and double-place orders.

    position_only=True runs the position-management path only (no analysis /
    no new orders). Used by the 5-minute position-monitor job so TP/SL hits
    are detected quickly without re-running multi-TF analysis every 5 min.
    """
    if not _cycle_lock.acquire(blocking=False):
        logger.warning("Another cycle is already running — skipping this trigger")
        return
    try:
        _bot_cycle_impl(use_agent, position_only=position_only)
    finally:
        _cycle_lock.release()


def _position_monitor_tick(use_agent: bool = True) -> None:
    """Lightweight 5-minute check — only runs if we have a position / pending."""
    try:
        from bot.state import load_state
        state = load_state()
        if not state["position"]["active"] and not state["pending_order"]["active"]:
            return  # nothing to monitor
    except Exception as exc:
        logger.warning("position_monitor: could not load state: %s", exc)
        return
    _bot_cycle(use_agent=use_agent, position_only=True)


def _bot_cycle_impl(use_agent: bool = True, position_only: bool = False) -> None:
    from bot import circuit_breaker, console_ui, order_manager, pending_order, position_guard, trail_manager
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

    # ── Detect external close (TP/SL algo fired on OKX between cycles) ────────
    if not dry_run and state["position"]["active"]:
        try:
            from bot import okx_private as okx
            live_pos = okx.get_position(inst_id)
            if live_pos is None:
                # Position was closed externally — TP/SL hit, or manual close on OKX
                logger.info("External close detected — position no longer on OKX")
                pos_snap = dict(state["position"])
                close_price = _get_current_price() or (pos_snap.get("entry_price") or 0)

                # Try to derive actual close price + PnL from fills
                try:
                    fills = okx.get_fills(inst_id, limit=10)
                    entry_ord = pos_snap.get("entry_order_id")
                    algo_id = pos_snap.get("algo_order_id")
                    # Pick the most recent fill that's NOT the entry
                    close_fills = [
                        f for f in fills
                        if f.get("ordId") and f.get("ordId") != entry_ord
                    ]
                    if close_fills:
                        close_price = float(close_fills[0].get("fillPx", close_price))
                except Exception as exc:
                    logger.warning("Could not fetch close fill: %s", exc)

                entry_price = pos_snap.get("entry_price", 0) or 0
                size = pos_snap.get("size_contracts", 0) or 0
                direction = 1 if pos_snap.get("side") == "long" else -1
                from bot.order_manager import CONTRACT_SIZE
                realized = size * CONTRACT_SIZE * (close_price - entry_price) * direction

                # Guess reason from SL/TP levels
                sl = pos_snap.get("sl_price") or 0
                tp1 = pos_snap.get("tp1_price") or 0
                if sl and abs(close_price - sl) / close_price < 0.005:
                    reason = "sl_hit"
                elif tp1 and abs(close_price - tp1) / close_price < 0.005:
                    reason = "tp1_hit"
                else:
                    reason = "reconcile_external_close"

                # Compute hold hours
                open_time_str = pos_snap.get("open_time")
                hold_hours = 0.0
                if open_time_str:
                    from bot.pending_order import _parse_dt
                    ot = _parse_dt(open_time_str)
                    if ot:
                        hold_hours = (now - ot).total_seconds() / 3600

                # Refresh balance + update daily stats
                balance_now = _get_balance(dry_run)
                circuit_breaker.record_pnl(state, realized)

                # Clear position state
                state["position"] = {
                    "active": False, "side": None, "entry_price": None,
                    "size_contracts": None, "open_time": None,
                    "sl_price": None, "tp1_price": None, "tp2_price": None,
                    "algo_order_id": None, "entry_order_id": None,
                    "reconciled": False, "dry_run": False,
                    "original_sl_price": None, "trail_stage": 0,
                    "trail_reason": None,
                }
                state["pending_order"] = {
                    "active": False, "order_id": None, "entry_price": None,
                    "placed_at": None, "side": None,
                }
                state["last_action"] = reason

                # Remember last TP close for pullback gate
                if reason == "tp1_hit":
                    state["last_tp_close"] = {
                        "side": pos_snap.get("side"),
                        "close_price": close_price,
                        "time": now.isoformat(),
                    }
                # Remember last losing close for loss-cooldown gate
                elif realized < 0:
                    state["last_loss_close"] = {
                        "side": pos_snap.get("side"),
                        "close_price": close_price,
                        "time": now.isoformat(),
                    }

                # Post-trade review log
                _append_trade_review(
                    pos_snap, close_price, realized, hold_hours, reason, balance_now,
                )

                try:
                    from bot import console_ui as _cui
                    _cui.position_closed_panel(
                        pos_snap.get("side", "?"), entry_price, close_price,
                        realized, hold_hours, balance_now, reason,
                    )
                except Exception:
                    pass
                send_message_sync(rpt.position_closed(
                    pos_snap.get("side", "?"), entry_price, close_price,
                    size, hold_hours, balance_now, reason, dry_run,
                ))
                save_state(state)
        except Exception as exc:
            logger.error("External close detection failed: %s", exc)

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

            pos_snap = dict(pos)
            state = order_manager.close_trade(state, reason="manual_close", current_price=close_price)
            from bot.order_manager import CONTRACT_SIZE
            _realized = (pos_snap.get("size_contracts") or 0) * CONTRACT_SIZE * (
                close_price - (pos_snap.get("entry_price") or 0)
            ) * (1 if pos_snap.get("side") == "long" else -1)
            _append_trade_review(
                pos_snap, close_price, _realized, hold_hours, "manual_close", balance,
            )
            msg = rpt.position_closed(
                pos.get("side", "?"), pos.get("entry_price", 0), close_price,
                pos.get("size_contracts", 0), hold_hours, balance, "manual_close", dry_run,
            )
            send_message_sync(msg)
        except Exception as exc:
            logger.error("Manual close failed: %s", exc)
            send_message_sync(f"❌ <b>Manual close FAILED</b>\n<i>{rpt._esc(exc)}</i>")
        state["pending_close_confirm"] = False
        save_state(state)
        return

    # ── Circuit breaker check ─────────────────────────────────────────────────
    balance = _get_balance(dry_run)
    cb_status = circuit_breaker.check_and_trigger(state, balance)
    if cb_status != "not_triggered":
        # Only notify on fresh trigger — avoid spam on subsequent cycles
        if cb_status == "newly_triggered":
            cb_until = state.get("circuit_break_until", "?")
            daily_loss = state.get("daily_loss_usdt", 0.0)
            loss_pct = (daily_loss / balance * 100) if balance > 0 else 0.0
            max_pct = float(os.getenv("MAX_DAILY_LOSS_PCT", "3"))
            send_message_sync(rpt.circuit_break_alert(daily_loss, loss_pct, max_pct, str(cb_until)))
        save_state(state)
        logger.info("Circuit break active (%s) — skipping cycle", cb_status)
        return

    # ── Fetch current price ───────────────────────────────────────────────────
    current_price = _get_current_price()
    if not current_price:
        logger.error("Cannot determine current price — skipping cycle")
        return

    # Dashboard cycle header (skip on 5-min silent ticks to keep console clean)
    if not position_only:
        if state["position"]["active"]:
            state_label = "HOLDING"
        elif state["pending_order"]["active"]:
            state_label = "PENDING FILL"
        else:
            state_label = "IDLE · analyzing"
        console_ui.cycle_header(state_label, current_price, balance)

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

        # Progressive trail: move SL to BE / lock profit if earned.
        # Silent on 5-min ticks (position_only): trail is not a close event.
        try:
            pos_before = dict(state["position"])
            old_sl = pos_before.get("sl_price") or 0
            state, trail_moved_flag, trail_reason = trail_manager.manage_trail(state, current_price)
            if trail_moved_flag:
                new_sl = state["position"].get("sl_price") or 0
                if not position_only:
                    send_message_sync(rpt.trail_moved(
                        state["position"], old_sl, new_sl, trail_reason, current_price,
                    ))
                save_state(state)
        except Exception as exc:
            logger.error("trail_manager failed: %s", exc)

        # Danger check
        try:
            is_dangerous, reasons = position_guard.evaluate_danger(state, current_price, balance)
        except Exception as exc:
            logger.error("evaluate_danger failed: %s", exc)
            is_dangerous, reasons = False, []

        if is_dangerous:
            console_ui.danger_panel(reasons, state["position"], current_price)
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
                from bot.order_manager import CONTRACT_SIZE
                _realized = (pos_snap.get("size_contracts") or 0) * CONTRACT_SIZE * (
                    current_price - (pos_snap.get("entry_price") or 0)
                ) * (1 if pos_snap.get("side") == "long" else -1)
                _append_trade_review(
                    pos_snap, current_price, _realized, hold_hours, close_reason, balance,
                    extra={"danger_reasons": reasons},
                )
                console_ui.position_closed_panel(
                    pos_snap.get("side", "?"), pos_snap.get("entry_price", 0),
                    current_price, _realized, hold_hours, balance, close_reason,
                )
                send_message_sync(rpt.position_closed(
                    pos_snap.get("side", "?"), pos_snap.get("entry_price", 0),
                    current_price, pos_snap.get("size_contracts", 0),
                    hold_hours, balance, close_reason, dry_run,
                ))
            except Exception as exc:
                logger.error("close_trade failed: %s", exc)
                send_message_sync(f"❌ <b>FAILED to close dangerous position</b>\n<i>{rpt._esc(exc)}</i>")
        else:
            # Safe — optionally ask agent (full cycles only, API cost control).
            if not position_only and os.getenv("AGENT_HOLD_CHECK", "false").lower() == "true":
                state = _agent_hold_review(state, current_price, balance, use_agent)
                save_state(state)
                if not state["position"]["active"]:
                    return  # agent closed it

            # Status report — suppress on 5-min silent ticks.
            if not position_only:
                try:
                    import okx_client
                    fund_data = okx_client.get_funding_rate(inst_id)
                    funding_rate = float(fund_data.get("fundingRate", 0))
                except Exception:
                    funding_rate = None

                # Console dashboard — compact holding panel
                pos = state["position"]
                entry = float(pos.get("entry_price") or 0)
                size = float(pos.get("size_contracts") or 0)
                direction = 1 if pos.get("side") == "long" else -1
                pnl_usdt = size * order_manager.CONTRACT_SIZE * (current_price - entry) * direction
                pnl_pct = pnl_usdt / balance * 100 if balance else 0
                hold_h = 0.0
                if pos.get("open_time"):
                    from bot.pending_order import _parse_dt as _pdt
                    ot = _pdt(pos["open_time"])
                    if ot:
                        hold_h = (datetime.now(tz=timezone.utc) - ot).total_seconds() / 3600
                console_ui.cycle_report_panel(pos, balance, current_price, pnl_usdt, pnl_pct, hold_h)

                send_message_sync(rpt.cycle_report(state, balance, current_price, funding_rate))

        save_state(state)
        return

    # ── Route: HAS PENDING ORDER ──────────────────────────────────────────────
    if state["pending_order"]["active"]:
        logger.info("Has pending order — managing")
        before_action = state.get("last_action")
        state = pending_order.manage(state, current_price)
        save_state(state)

        # Notify based on what happened.
        # Silent ticks only surface close events (fresh-fill-then-close counts).
        if state["position"]["active"]:
            pos = state["position"]
            # Detection-lag fix: position filled AND already closed in the
            # gap. Reconcile immediately + send a combined message.
            if pos.get("_needs_fresh_close_reconcile") and not dry_run:
                try:
                    _handle_fresh_fill_then_close(state, balance)
                    save_state(state)
                except Exception as exc:
                    logger.error("fresh-fill reconcile failed: %s", exc)
            elif not position_only:
                # Just-filled notification — only on full cycle
                send_message_sync(
                    f"✅ <b>Limit order filled</b>\n\n"
                    f"🟢 {pos.get('side', '?').upper()} · {pos.get('size_contracts', 0)} contracts @ <code>${pos.get('entry_price', 0):,.0f}</code>\n"
                    f"🎯 TP1: <code>${pos.get('tp1_price') or 0:,.0f}</code>  🛑 SL: <code>${pos.get('sl_price') or 0:,.0f}</code>"
                )
        elif not state["pending_order"]["active"]:
            if not position_only:
                send_message_sync(
                    f"⚠️ <b>Pending order cleared</b>\n<i>{rpt._esc(state.get('last_action', 'unknown'))}</i>\n\n"
                    f"💰 Balance: <b>${balance:,.2f}</b>  📊 Current: <code>${current_price:,.0f}</code>"
                )
        else:
            if not position_only:
                pending = state["pending_order"]
                placed_at = pending.get("placed_at", "?")
                entry = pending.get("entry_price") or 0
                diff_pct = (current_price - entry) / entry * 100 if entry else 0
                send_message_sync(
                    f"⏱ <b>Waiting for limit fill</b>\n\n"
                    f"{('🟢' if pending.get('side') == 'buy' else '🔴')} "
                    f"Entry: <code>${entry:,.0f}</code>  Now: <code>${current_price:,.0f}</code> ({diff_pct:+.2f}%)\n"
                    f"💰 Balance: <b>${balance:,.2f}</b>"
                )
        return

    # Position-monitor ticks skip the expensive IDLE analysis path.
    if position_only:
        logger.info("position_only cycle — skipping IDLE analysis")
        save_state(state)
        return

    # ── Route: IDLE — analyze and maybe place order ───────────────────────────
    logger.info("IDLE — running analysis")
    signal = _run_analysis(inst_id, use_agent=use_agent)
    if signal is None:
        logger.error("Analysis failed — no signal produced")
        save_state(state)
        return

    # Console dashboard — TF breakdown table
    tfb = signal.get("tf_breakdown") or {}
    if tfb:
        console_ui.signal_table(
            tf_breakdown=tfb,
            confluence={
                "agreeing_tfs": signal.get("agreeing_tfs", 0),
                "net_score": signal.get("net_score", 0),
                "confidence": (
                    "HIGH" if signal.get("confidence", 0) >= 75
                    else "MEDIUM" if signal.get("confidence", 0) >= 55
                    else "LOW"
                ),
            },
            confidence_pct=int(signal.get("confidence", 0) or 0),
        )

    passes, reject_reason = _signal_passes_filters(signal, state)

    if not passes:
        logger.info("No trade — %s", reject_reason)
        next_run = _next_run_str()
        console_ui.no_trade_panel(reject_reason, signal, next_run)
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

    # Pre-trade sizing + daily-loss cap check — skip if worst-case loss
    # would breach remaining daily budget, so a single trade can never
    # push us over MAX_DAILY_LOSS_PCT.
    risk_pct = float(os.getenv("RISK_PCT", "1.0"))
    leverage = int(os.getenv("LEVERAGE", "5"))
    contracts = order_manager.calc_contracts(
        balance, risk_pct, signal["entry"], signal["sl"], leverage,
    )
    if contracts < 1:
        reason = "Sizing produced 0 contracts (risk cap / balance too small)"
        logger.info("No trade — %s", reason)
        send_message_sync(rpt.no_trade(reason, signal, _next_run_str()))
        state["last_action"] = "skip_size_zero"
        save_state(state)
        return

    # Daily-cap gate: block only AFTER we've already lost today.
    # Strong-signal override: bypass cap for A+ setups (3/3 TFs agree
    # + very high confidence). Still respects MAX_RISK_PCT per trade.
    from bot.order_manager import CONTRACT_SIZE
    worst_case_loss = contracts * CONTRACT_SIZE * abs(signal["entry"] - signal["sl"])
    daily_loss = state.get("daily_loss_usdt", 0.0)
    max_daily_pct = float(os.getenv("MAX_DAILY_LOSS_PCT", "3.0"))
    daily_budget = balance * max_daily_pct / 100

    strong_conf = float(os.getenv("STRONG_SIGNAL_CONF", "85"))
    strong_agree = int(os.getenv("STRONG_SIGNAL_AGREE", "3"))
    is_strong = (
        signal.get("confidence", 0) >= strong_conf
        and signal.get("agreeing_tfs", 0) >= strong_agree
    )

    if (
        daily_loss > 0
        and daily_loss + worst_case_loss > daily_budget
        and not is_strong
    ):
        reason = (
            f"Would breach daily cap: loss so far $"
            f"{daily_loss:.2f} + worst-case $"
            f"{worst_case_loss:.2f} > budget $"
            f"{daily_budget:.2f}"
        )
        logger.info("No trade — %s", reason)
        send_message_sync(rpt.no_trade(reason, signal, _next_run_str()))
        state["last_action"] = "skip_daily_cap"
        save_state(state)
        return

    if is_strong and daily_loss > 0 and daily_loss + worst_case_loss > daily_budget:
        logger.info(
            "Strong-signal override: conf=%s agree=%s — bypassing daily cap "
            "(loss=$%.2f, worst-case=$%.2f, budget=$%.2f)",
            signal.get("confidence"), signal.get("agreeing_tfs"),
            daily_loss, worst_case_loss, daily_budget,
        )

    # Place order
    logger.info("Signal passes filters — placing order (dry_run=%s)", dry_run)
    try:
        state = order_manager.open_trade(state, signal, balance)

        # Notify
        risk_usdt = worst_case_loss
        order_id = state["pending_order"].get("order_id", "?")
        console_ui.order_placed_panel(signal, contracts, risk_usdt, order_id, dry_run)
        send_message_sync(rpt.order_placed(signal, contracts, risk_usdt, order_id, dry_run))
    except Exception as exc:
        logger.error("open_trade failed: %s", exc)
        send_message_sync(f"Order placement FAILED: {rpt._esc(exc)}")

    save_state(state)
    logger.info("=== Cycle end ===")


def _append_trade_review(
    pos_snap: dict,
    close_price: float,
    realized: float,
    hold_hours: float,
    reason: str,
    balance: float,
    extra: dict | None = None,
) -> None:
    """Append one JSONL line per closed trade for later post-mortem analysis.

    Path: logs/trade_review.jsonl (created if missing). Every field is
    JSON-serialisable. Includes entry signal if state['last_signal'] was set.
    """
    import json as _json
    log_path = Path(os.getenv("TRADE_REVIEW_LOG", "logs/trade_review.jsonl"))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "side": pos_snap.get("side"),
            "entry_price": pos_snap.get("entry_price"),
            "close_price": close_price,
            "size_contracts": pos_snap.get("size_contracts"),
            "original_sl": pos_snap.get("original_sl_price"),
            "final_sl": pos_snap.get("sl_price"),
            "tp1": pos_snap.get("tp1_price"),
            "tp2": pos_snap.get("tp2_price"),
            "realized_usdt": round(realized, 4),
            "hold_hours": round(hold_hours, 3),
            "reason": reason,
            "balance_after": round(balance, 2),
            "trail_stage": pos_snap.get("trail_stage"),
            "trail_reason": pos_snap.get("trail_reason"),
        }
        if extra:
            record.update(extra)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(record, default=str) + "\n")
        logger.info("Trade review logged: %s → %s", reason, log_path)
    except Exception as exc:
        logger.warning("Failed to write trade review: %s", exc)


def _agent_hold_review(
    state: dict, current_price: float, balance: float, use_agent: bool,
) -> dict:
    """Ask agent whether to HOLD / CLOSE / TIGHTEN_SL on open position."""
    if not use_agent:
        return state
    from bot import agent_hold, order_manager, report as rpt, trail_manager
    from bot.order_manager import CONTRACT_SIZE
    from bot.telegram_bot import send_message_sync
    from commands.trade import TIMEFRAMES, _confluence, _score_tf
    from commands.trade_agent import build_market_snapshot, check_agent_configured
    import okx_client

    ok, err = check_agent_configured()
    if not ok:
        logger.info("Agent hold check skipped — not configured: %s", err)
        return state

    pos = state["position"]
    entry = pos.get("entry_price") or 0
    size = pos.get("size_contracts") or 0
    direction = 1 if pos.get("side") == "long" else -1
    pnl = size * CONTRACT_SIZE * (current_price - entry) * direction
    pnl_pct = pnl / balance * 100 if balance else 0

    open_time_str = pos.get("open_time")
    hold_hours = 0.0
    if open_time_str:
        from bot.pending_order import _parse_dt
        ot = _parse_dt(open_time_str)
        if ot:
            hold_hours = (datetime.now(tz=timezone.utc) - ot).total_seconds() / 3600

    # Build a lightweight snapshot (re-fetch only what agent needs)
    inst_id = os.getenv("BOT_SYMBOL", "BTC-USDT-SWAP")
    symbol = inst_id.replace("-SWAP", "")
    try:
        tf_scores = {}
        for tf in TIMEFRAMES:
            df = okx_client.get_ohlcv(symbol, bar=tf, limit=200)
            if df is None or df.empty:
                logger.info("Skipping agent hold — missing %s OHLCV", tf)
                return state
            tf_scores[tf] = _score_tf(df)
        confluence = _confluence(tf_scores)
        funding_data = {"rate_8h": 0, "ann_pct": 0, "avg_7d": 0, "regime": "?"}
        try:
            fund = okx_client.get_funding_rate(inst_id)
            r8h = float(fund.get("fundingRate", 0))
            funding_data["rate_8h"] = r8h
            funding_data["ann_pct"] = r8h * 3 * 365 * 100
        except Exception:
            pass
        try:
            oi_raw = okx_client.get_open_interest(inst_id)
            oi_data = {"oi_usd_b": float(oi_raw.get("oiUsd", 0)) / 1e9}
        except Exception:
            oi_data = {"oi_usd_b": 0}
        try:
            mark = okx_client.get_mark_price(inst_id)
            mark_price = float(mark.get("markPx", current_price))
        except Exception:
            mark_price = current_price
        try:
            spot = okx_client.get_ticker(symbol)
            open_24h = float(spot.get("open24h", current_price))
            change_24h = (current_price - open_24h) / open_24h * 100 if open_24h else 0
        except Exception:
            change_24h = 0
        snapshot = build_market_snapshot(
            tf_scores, confluence, funding_data, oi_data,
            current_price, mark_price, change_24h,
        )
    except Exception as exc:
        logger.warning("Agent hold snapshot build failed: %s", exc)
        return state

    prompt = agent_hold.build_hold_prompt(
        pos, current_price, pnl, pnl_pct, hold_hours, snapshot,
    )
    decision = agent_hold.ask_agent(prompt)
    if not decision:
        logger.info("Agent hold check returned no decision — keeping HOLD")
        return state

    d = decision["decision"]
    reasoning = decision.get("reasoning", "")
    logger.info("Agent hold decision: %s — %s", d, reasoning)

    if d == "HOLD":
        send_message_sync(
            f"🤖 <b>Agent: HOLD</b>\n💡 <i>{rpt._esc(reasoning[:320])}</i>"
        )
        return state

    if d == "CLOSE":
        send_message_sync(
            f"🤖 <b>Agent: CLOSE</b>\n💡 <i>{rpt._esc(reasoning[:320])}</i>\n\n"
            f"🛑 Executing market close…"
        )
        try:
            pos_snap = dict(state["position"])
            state = order_manager.close_trade(
                state, reason="agent_close", current_price=current_price,
            )
            _append_trade_review(
                pos_snap, current_price,
                size * CONTRACT_SIZE * (current_price - entry) * direction,
                hold_hours, "agent_close", balance,
                extra={"agent_reasoning": reasoning},
            )
            send_message_sync(rpt.position_closed(
                pos_snap.get("side", "?"), entry, current_price, size,
                hold_hours, balance, "agent_close",
                os.getenv("DRY_RUN", "true").lower() == "true",
            ))
        except Exception as exc:
            logger.error("Agent close failed: %s", exc)
            send_message_sync(f"❌ Agent close FAILED: {rpt._esc(exc)}")
        return state

    if d == "TIGHTEN_SL":
        new_sl = decision.get("new_sl", 0)
        tp1 = pos.get("tp1_price") or 0
        if not new_sl or not tp1:
            return state
        # Don't allow SL to move *away* from entry in bad direction
        if direction == 1 and new_sl <= (pos.get("sl_price") or 0):
            logger.info("Agent TIGHTEN_SL ignored — new SL not tighter than current")
            return state
        if direction == -1 and new_sl >= (pos.get("sl_price") or 0):
            logger.info("Agent TIGHTEN_SL ignored — new SL not tighter than current")
            return state
        # Reuse trail_manager._replace_algo to update the OCO on exchange
        old_sl = pos.get("sl_price") or 0
        ok_replace = trail_manager._replace_algo(
            state, new_sl, tp1, pos.get("side"),
        ) if os.getenv("DRY_RUN", "true").lower() != "true" else True
        if ok_replace:
            state["position"]["sl_price"] = round(new_sl, 2)
            state["position"]["trail_reason"] = f"agent tighten: {reasoning[:100]}"
            send_message_sync(
                f"🤖 <b>Agent: TIGHTEN SL</b>\n"
                f"   SL <code>${old_sl:,.0f}</code> → <code>${new_sl:,.0f}</code>\n"
                f"💡 <i>{rpt._esc(reasoning[:280])}</i>"
            )
    return state


def _handle_fresh_fill_then_close(state: dict, balance: float) -> None:
    """Position was filled AND closed between cycles — reconcile in one shot.

    Called when pending_order.manage flagged `_needs_fresh_close_reconcile`.
    Computes close price from recent fills, updates daily PnL, clears
    position state, and sends a single combined Telegram message.
    """
    from bot import circuit_breaker, okx_private as okx
    from bot.order_manager import CONTRACT_SIZE
    from bot.telegram_bot import send_message_sync
    from bot import report as rpt

    inst_id = os.getenv("BOT_SYMBOL", "BTC-USDT-SWAP")
    pos = dict(state["position"])
    entry_price = pos.get("entry_price") or 0
    size = pos.get("size_contracts") or 0
    side = pos.get("side") or "?"
    direction = 1 if side == "long" else -1
    entry_ord = pos.get("entry_order_id")

    # Determine close price from recent fills (exclude the entry fill)
    close_price = entry_price
    try:
        fills = okx.get_fills(inst_id, limit=20)
        close_fills = [f for f in fills if f.get("ordId") and f.get("ordId") != entry_ord]
        if close_fills:
            close_price = float(close_fills[0].get("fillPx", close_price))
    except Exception as exc:
        logger.warning("Could not fetch close fill in fresh-close reconcile: %s", exc)

    realized = size * CONTRACT_SIZE * (close_price - entry_price) * direction

    # Guess reason from SL/TP levels
    sl = pos.get("sl_price") or 0
    tp1 = pos.get("tp1_price") or 0
    if sl and abs(close_price - sl) / close_price < 0.005:
        reason = "sl_hit"
    elif tp1 and abs(close_price - tp1) / close_price < 0.005:
        reason = "tp1_hit"
    else:
        reason = "fresh_close_reconcile"

    circuit_breaker.record_pnl(state, realized)

    now = datetime.now(tz=timezone.utc)
    open_time_str = pos.get("open_time")
    hold_hours = 0.0
    if open_time_str:
        from bot.pending_order import _parse_dt
        ot = _parse_dt(open_time_str)
        if ot:
            hold_hours = (now - ot).total_seconds() / 3600

    # Clear position
    state["position"] = {
        "active": False, "side": None, "entry_price": None,
        "size_contracts": None, "open_time": None,
        "sl_price": None, "tp1_price": None, "tp2_price": None,
        "algo_order_id": None, "entry_order_id": None,
        "reconciled": False, "dry_run": False,
        "original_sl_price": None, "trail_stage": 0, "trail_reason": None,
    }
    state["last_action"] = "filled_then_closed"

    # Remember last TP close for pullback gate
    if reason == "tp1_hit":
        state["last_tp_close"] = {
            "side": side,
            "close_price": close_price,
            "time": now.isoformat(),
        }
    # Remember last losing close for loss-cooldown gate
    elif realized < 0:
        state["last_loss_close"] = {
            "side": side,
            "close_price": close_price,
            "time": now.isoformat(),
        }

    # Post-trade review log
    _append_trade_review(pos, close_price, realized, hold_hours, reason, balance)

    try:
        from bot import console_ui as _cui
        _cui.position_closed_panel(
            side, entry_price, close_price, realized, hold_hours, balance, reason,
        )
    except Exception:
        pass

    send_message_sync(
        f"⚡ <b>Filled then closed</b> · <i>{rpt._reason_label(reason)}</i>\n\n"
        f"{rpt._side_emoji(side)} {side.upper()} · {size} contracts\n"
        f"   Entry: <code>${entry_price:,.0f}</code>\n"
        f"   Close: <code>${close_price:,.0f}</code>\n"
        f"   PnL  : <b>{rpt._fmt_usdt(realized)}</b> {rpt._pnl_emoji(realized)}\n\n"
        f"⚠️ Detected late — fill + close occurred between cycles.\n"
        f"⏱ Hold: {int(hold_hours*60)}m · {rpt.E_MONEY} Balance: <b>${balance:,.2f}</b>"
    )


def _next_run_str() -> str:
    """Next cycle time aligned to UTC candle-close boundary."""
    from datetime import timedelta
    hours = float(os.getenv("BOT_INTERVAL_HOURS", "2"))
    now = datetime.now(tz=timezone.utc)

    # Integer hours ≥ 1 → align to HH:00
    if hours == int(hours) and hours >= 1:
        h = int(hours)
        next_hour_slot = ((now.hour // h) + 1) * h
        if next_hour_slot >= 24:
            nxt = (now + timedelta(days=1)).replace(
                hour=next_hour_slot % 24, minute=0, second=0, microsecond=0,
            )
        else:
            nxt = now.replace(hour=next_hour_slot, minute=0, second=0, microsecond=0)
        return nxt.strftime("%H:%M UTC")

    # Sub-hour intervals dividing 60 cleanly → align to the next MM boundary
    minutes = int(round(hours * 60))
    if 0 < minutes < 60 and 60 % minutes == 0 and abs(hours * 60 - minutes) < 1e-6:
        cur = now.minute
        next_minute_slot = ((cur // minutes) + 1) * minutes
        if next_minute_slot >= 60:
            nxt = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        else:
            nxt = now.replace(minute=next_minute_slot, second=0, microsecond=0)
        return nxt.strftime("%H:%M UTC")

    # Fallback
    nxt = now + timedelta(hours=hours)
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

    dry = os.getenv("DRY_RUN", "true").lower() == "true"
    demo = os.getenv("OKX_DEMO_MODE", "false").lower() == "true"
    symbol = os.getenv("BOT_SYMBOL", "BTC-USDT-SWAP")
    interval = os.getenv("BOT_INTERVAL_HOURS", "2")
    leverage = os.getenv("LEVERAGE", "5")
    risk = os.getenv("RISK_PCT", "1.0")
    mode_emoji = "⚠️" if dry else ("🧪" if demo else "🚀")
    mode_label = "DRY RUN" if dry else ("DEMO" if demo else "LIVE")

    # Console dashboard banner
    from bot import console_ui
    console_ui.startup_banner({
        "mode": mode_label, "symbol": symbol, "interval": interval,
        "leverage": leverage, "risk_pct": risk, "agent": use_agent,
        "regime_filter": os.getenv("REGIME_FILTER", "true").lower() == "true",
    })

    send_message_sync(
        f"{mode_emoji} <b>Bot started</b> · <i>{mode_label}</i>\n\n"
        f"📊 Symbol: <code>{symbol}</code>\n"
        f"⏱ Cycle: every <b>{interval}h</b>\n"
        f"💰 Risk: <b>{risk}%</b> · Leverage: <b>{leverage}x</b>\n"
        f"🤖 Agent: <b>{'ON' if use_agent else 'OFF'}</b>\n\n"
        f"ℹ️ Send /help for commands"
    )

    # ── Run ───────────────────────────────────────────────────────────────────
    if args.once:
        _bot_cycle(use_agent=use_agent)
    else:
        from bot.scheduler import run_scheduler
        run_scheduler(
            cycle_fn=lambda: _bot_cycle(use_agent=use_agent),
            position_monitor_fn=lambda: _position_monitor_tick(use_agent=use_agent),
        )


if __name__ == "__main__":
    main()
