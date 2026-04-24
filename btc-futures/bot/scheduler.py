"""APScheduler wrapper for the bot cycle.

Scheduling is aligned to UTC candle-close boundaries when
BOT_INTERVAL_HOURS is an integer (the common case: 1h, 2h, 4h).
Runs at HH:MM:SS where HH is divisible by the interval and
MM:SS = 00:CYCLE_OFFSET_SECONDS (default 15s after candle close
so OKX has served the new bar).

Non-integer intervals fall back to fixed-interval scheduling.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


def _build_trigger(interval_hours: float, offset_sec: int):
    """Build a cron-aligned trigger when the interval divides cleanly, else
    fall back to IntervalTrigger.

    Supported clean divisions:
        ≥ 1h integer hours:   every N hours at HH:00:SS
        < 1h whose minutes divide 60: every N minutes at MM:SS (0, N, 2N, ...)
    """
    # Integer hours ≥ 1
    if interval_hours >= 1 and interval_hours == int(interval_hours):
        hours = int(interval_hours)
        hour_expr = "*" if hours == 1 else f"*/{hours}"
        trigger = CronTrigger(
            hour=hour_expr, minute=0, second=offset_sec, timezone="UTC",
        )
        return trigger, f"cron-aligned every {hours}h at :00:{offset_sec:02d} UTC"

    # Sub-hour: try to represent as whole minutes dividing 60 cleanly
    minutes = int(round(interval_hours * 60))
    if 0 < minutes < 60 and 60 % minutes == 0 and abs(interval_hours * 60 - minutes) < 1e-6:
        minute_list = ",".join(str(m) for m in range(0, 60, minutes))
        trigger = CronTrigger(
            minute=minute_list, second=offset_sec, timezone="UTC",
        )
        minute_preview = "|".join(f"{m:02d}" for m in range(0, 60, minutes))
        return trigger, (
            f"cron-aligned every {minutes}m at :{minute_preview}:{offset_sec:02d} UTC"
        )

    # Fallback — fixed interval, will drift from candle-close boundaries
    trigger = IntervalTrigger(hours=interval_hours)
    return trigger, f"interval every {interval_hours:.3f}h (no clean cron alignment)"


def run_scheduler(
    cycle_fn: Callable[[], None],
    position_monitor_fn: Callable[[], None] | None = None,
) -> None:
    """Start blocking scheduler.

    Two jobs:
      - bot_cycle: every BOT_INTERVAL_HOURS (candle-aligned) — full cycle with
        multi-TF analysis.
      - position_monitor: every POSITION_CHECK_MINUTES (default 5m) — fast
        TP/SL detection + trail updates while holding a position. Only fires
        if position / pending is active (short-circuited inside the callable).

    The full cycle also runs immediately on startup.
    """
    interval_hours = float(os.getenv("BOT_INTERVAL_HOURS", "2"))
    offset_sec = int(os.getenv("CYCLE_OFFSET_SECONDS", "15"))

    trigger, schedule_desc = _build_trigger(interval_hours, offset_sec)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        cycle_fn,
        trigger=trigger,
        id="bot_cycle",
        next_run_time=datetime.now(tz=timezone.utc),
    )
    logger.info("Scheduler: bot_cycle — %s", schedule_desc)

    # Position monitor — fast cadence for TP/SL detection
    if position_monitor_fn is not None:
        pm_minutes = int(os.getenv("POSITION_CHECK_MINUTES", "5"))
        if 0 < pm_minutes < 60 and 60 % pm_minutes == 0:
            pm_minute_list = ",".join(str(m) for m in range(0, 60, pm_minutes))
            pm_trigger = CronTrigger(
                minute=pm_minute_list, second=offset_sec + 5, timezone="UTC",
            )
            pm_desc = f"cron every {pm_minutes}m"
        else:
            pm_trigger = IntervalTrigger(minutes=pm_minutes)
            pm_desc = f"interval every {pm_minutes}m"
        scheduler.add_job(
            position_monitor_fn,
            trigger=pm_trigger,
            id="position_monitor",
            coalesce=True,
            max_instances=1,
        )
        logger.info("Scheduler: position_monitor — %s", pm_desc)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
