"""Background watcher for utility-tab features.

Currently: scans TradingViewAlert rows and fires a Telegram nudge when an
alert is about to expire or has just expired. The watcher deduplicates by
stamping notified_at on each row after a successful send so the user is
not spammed every tick.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session
from models import TradingViewAlert

logger = logging.getLogger("algopro.utility_watcher")

# Tunables
TICK_INTERVAL_SECONDS = 30 * 60  # check every 30 min
WARN_WINDOW_HOURS = 24           # nudge starts 24h before expiry
RENOTIFY_INTERVAL_HOURS = 12     # if still unhandled, ping again every 12h


def _is_due_for_notification(
    row: TradingViewAlert,
    now: datetime,
    warn_delta: timedelta,
    renotify_delta: timedelta,
) -> bool:
    """Pure decision: should we send a Telegram nudge for this row right now?"""
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    time_to_expiry = expires_at - now
    if time_to_expiry > warn_delta:
        return False  # too early
    notified_at = row.notified_at
    if notified_at is None:
        return True
    if notified_at.tzinfo is None:
        notified_at = notified_at.replace(tzinfo=timezone.utc)
    return (now - notified_at) >= renotify_delta


def _format_message(row: TradingViewAlert, now: datetime) -> str:
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    delta = expires_at - now
    secs = int(delta.total_seconds())
    if secs <= 0:
        status = f"EXPIRED {abs(secs) // 3600}h ago"
    else:
        h = secs // 3600
        m = (secs % 3600) // 60
        status = f"expires in {h}h {m}m"
    label = row.name or f"{row.symbol} {row.timeframe}"
    lines = [
        f"⏰ TradingView alert: <b>{label}</b>",
        f"{row.symbol} · {row.timeframe} · {status}",
        f"Expires at: {expires_at.strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    if row.note:
        lines.append(f"Note: {row.note}")
    return "\n".join(lines)


async def check_and_notify_alerts(
    db: AsyncSession, warn_hours: int = WARN_WINDOW_HOURS
) -> int:
    """One sweep. Returns the count of rows we notified about."""
    from notifications import send_telegram  # local import to avoid cycles

    now = datetime.now(timezone.utc)
    warn_delta = timedelta(hours=warn_hours)
    renotify_delta = timedelta(hours=RENOTIFY_INTERVAL_HOURS)

    result = await db.execute(select(TradingViewAlert))
    rows = list(result.scalars().all())

    notified = 0
    for row in rows:
        if not _is_due_for_notification(row, now, warn_delta, renotify_delta):
            continue
        try:
            await send_telegram(_format_message(row, now))
        except Exception as e:
            logger.warning(
                "TV alert telegram send failed for id=%s: %s", row.id, e
            )
            continue
        row.notified_at = now
        notified += 1

    if notified > 0:
        await db.commit()
    return notified


async def run_utility_watcher_loop():
    """Background loop launched at app startup."""
    logger.info(
        "TradingView alert watcher started "
        "(tick=%ds, warn=%dh, renotify=%dh)",
        TICK_INTERVAL_SECONDS, WARN_WINDOW_HOURS, RENOTIFY_INTERVAL_HOURS,
    )
    while True:
        try:
            async with async_session() as db:
                await check_and_notify_alerts(db)
        except asyncio.CancelledError:
            logger.info("TradingView alert watcher cancelled — exiting loop")
            raise
        except Exception as e:
            logger.exception("Utility watcher tick failed: %s", e)
        await asyncio.sleep(TICK_INTERVAL_SECONDS)
