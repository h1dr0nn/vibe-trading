"""APScheduler wrapper for the 2-hour bot cycle."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

logger = logging.getLogger(__name__)


def run_scheduler(cycle_fn: Callable[[], None]) -> None:
    """Start the blocking scheduler and run cycle_fn every BOT_INTERVAL_HOURS hours.

    Also runs cycle_fn immediately on startup before the scheduler takes over.
    """
    interval_hours = float(os.getenv("BOT_INTERVAL_HOURS", "2"))

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        cycle_fn,
        trigger="interval",
        hours=interval_hours,
        id="bot_cycle",
        next_run_time=datetime.now(tz=timezone.utc),  # run immediately on start
    )

    logger.info("Scheduler starting — cycle every %.1f hours", interval_hours)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
