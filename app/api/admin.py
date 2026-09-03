"""Административные и наблюдательные ручки (этап 4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.logging_conf import get_logger
from app.services import jobs, ledger, reconciliation

router = APIRouter(prefix="/admin", tags=["admin"])
log = get_logger("admin")


@router.get("/reconciliation")
async def reconciliation_report(
    session: AsyncSession = Depends(get_session),
    grace_seconds: int = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
):
    """Сверка: "оплачен, но не выдан" / "выдан, но не оплачен" + баланс журнала."""
    grace = grace_seconds if grace_seconds is not None else settings.stuck_order_seconds
    return await reconciliation.report(session, grace_seconds=grace, limit=limit)


@router.get("/ledger/balance")
async def ledger_balance(session: AsyncSession = Depends(get_session)):
    return await reconciliation.ledger_balance(session)


@router.get("/orders/{order_id}/timeline")
async def order_timeline(order_id: str, session: AsyncSession = Depends(get_session)):
    """Вся история заказа в одном ответе: платежи, попытки, выдача, деньги."""
    data = await reconciliation.order_timeline(session, order_id)
    if data["order"] is None:
        raise HTTPException(status_code=404, detail="order not found")
    return data


@router.post("/orders/{order_id}/redeliver")
async def redeliver(order_id: str, session: AsyncSession = Depends(get_session)):
    """Ручное добивание заказа. Безопасно: та же идемпотентная задача выдачи."""
    row = (
        await session.execute(
            text("SELECT status, paid_at FROM orders WHERE id=:id"), {"id": order_id}
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")
    if row["paid_at"] is None:
        raise HTTPException(status_code=409, detail="order is not paid")
    if row["status"] == "delivered":
        return {"order_id": order_id, "enqueued": False, "note": "already delivered"}

    enqueued = await jobs.enqueue(
        session, jobs.KIND_DELIVER, dedupe_key=order_id, payload={"order_id": order_id}
    )
    log.info("admin.redeliver", order_id=order_id, enqueued=enqueued)
    return {"order_id": order_id, "enqueued": enqueued}


@router.post("/orders/{order_id}/refund")
async def refund(order_id: str, session: AsyncSession = Depends(get_session)):
    """Возврат по невыдаваемому заказу - обратная проводка, журнал остаётся сходящимся."""
    row = (
        await session.execute(
            text(
                "SELECT status, amount_minor, currency, paid_at FROM orders WHERE id=:id "
                "FOR UPDATE"
            ),
            {"id": order_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")
    if row["paid_at"] is None:
        raise HTTPException(status_code=409, detail="order was never paid")
    if row["status"] == "delivered":
        raise HTTPException(status_code=409, detail="order is delivered, refund manually")

    txn = await ledger.post_refund(session, order_id, row["amount_minor"], row["currency"])
    log.info("admin.refund", order_id=order_id, txn=str(txn) if txn else None)
    return {"order_id": order_id, "refunded": txn is not None}


@router.get("/jobs")
async def list_jobs(
    session: AsyncSession = Depends(get_session),
    state: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
):
    sql = (
        "SELECT id, kind, dedupe_key, state, attempts, max_attempts, run_at, last_error "
        "FROM jobs {where} ORDER BY id DESC LIMIT :limit"
    ).format(where="WHERE state = :state" if state else "")
    params = {"limit": limit} | ({"state": state} if state else {})
    rows = (await session.execute(text(sql), params)).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_session)):
    orders = (
        await session.execute(
            text("SELECT status, count(*) AS n FROM orders GROUP BY status ORDER BY status")
        )
    ).mappings().all()
    events = (
        await session.execute(
            text(
                "SELECT processing_state, count(*) AS n FROM payment_events "
                "GROUP BY processing_state ORDER BY processing_state"
            )
        )
    ).mappings().all()
    attempts = (
        await session.execute(
            text("SELECT supplier, state, count(*) AS n FROM supplier_attempts "
                 "GROUP BY supplier, state ORDER BY supplier, state")
        )
    ).mappings().all()
    issued = (await session.execute(text("SELECT count(*) FROM issuances"))).scalar_one()
    return {
        "orders_by_status": [dict(r) for r in orders],
        "payment_events_by_state": [dict(r) for r in events],
        "supplier_attempts": [dict(r) for r in attempts],
        "issuances": issued,
    }
