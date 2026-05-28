"""Utility endpoints — house out-of-context helpers like TradingView alerts."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import TradingViewAlert
from schemas import TVAlertCreate, TVAlertResponse, TVAlertUpdate

router = APIRouter(prefix="/api/utility", tags=["utility"])
logger = logging.getLogger("algotrade.utility")


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_response(row: TradingViewAlert) -> TVAlertResponse:
    return TVAlertResponse(
        id=row.id,
        symbol=row.symbol,
        timeframe=row.timeframe,
        name=row.name,
        note=row.note,
        expires_at=row.expires_at,
        notified_at=row.notified_at,
        created_at=row.created_at,
    )


# ── TradingView Alerts ────────────────────────────────────────────────


@router.get("/tv-alerts", response_model=list[TVAlertResponse])
async def list_tv_alerts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(TradingViewAlert).order_by(TradingViewAlert.expires_at.asc())
    )
    return [_to_response(r) for r in result.scalars().all()]


@router.post("/tv-alerts", response_model=TVAlertResponse, status_code=201)
async def create_tv_alert(
    body: TVAlertCreate, db: AsyncSession = Depends(get_db)
):
    expires = _ensure_utc(body.expires_at)
    if expires <= datetime.now(timezone.utc):
        raise HTTPException(400, "expires_at must be in the future")

    row = TradingViewAlert(
        symbol=body.symbol.strip().upper(),
        timeframe=body.timeframe.strip(),
        name=body.name,
        note=body.note,
        expires_at=expires,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _to_response(row)


@router.put("/tv-alerts/{alert_id}", response_model=TVAlertResponse)
async def update_tv_alert(
    alert_id: int, body: TVAlertUpdate, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(TradingViewAlert).where(TradingViewAlert.id == alert_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Alert not found")

    if body.symbol is not None:
        row.symbol = body.symbol.strip().upper()
    if body.timeframe is not None:
        row.timeframe = body.timeframe.strip()
    if body.name is not None:
        row.name = body.name
    if body.note is not None:
        row.note = body.note
    if body.expires_at is not None:
        new_exp = _ensure_utc(body.expires_at)
        row.expires_at = new_exp
        # If the expiry was moved forward, allow re-notification.
        row.notified_at = None

    await db.commit()
    await db.refresh(row)
    return _to_response(row)


@router.delete("/tv-alerts/{alert_id}", status_code=204)
async def delete_tv_alert(alert_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(TradingViewAlert).where(TradingViewAlert.id == alert_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Alert not found")
    await db.delete(row)
    await db.commit()


@router.post("/tv-alerts/check-now", status_code=200)
async def check_alerts_now(db: AsyncSession = Depends(get_db)):
    """Force a watcher tick from the UI (useful for manual testing)."""
    from utility_watcher import check_and_notify_alerts
    notified = await check_and_notify_alerts(db, warn_hours=24)
    return {"notified": notified}
