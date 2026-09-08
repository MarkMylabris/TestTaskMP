"""Административные и наблюдательные ручки."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.logging_conf import get_logger
from app.services import history, jobs, ratelimit, reconciliation, settlement

router = APIRouter(prefix="/admin", tags=["admin"])
log = get_logger("admin")


def _aware(ts: datetime) -> datetime:
    """Момент без зоны считаем UTC: сравнение идёт с timestamptz."""
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@router.get("/reconciliation")
async def reconciliation_report(
    session: AsyncSession = Depends(get_session),
    grace_seconds: int = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
):
    """Сверка: "оплачен, но не выдан", "выдан, но не оплачен", баланс."""
    grace = grace_seconds if grace_seconds is not None else settings.stuck_order_seconds
    return await reconciliation.report(session, grace_seconds=grace, limit=limit)


@router.get("/ledger/balance")
async def ledger_balance(session: AsyncSession = Depends(get_session)):
    return await reconciliation.ledger_balance(session)


@router.get("/orders/{order_id}/timeline")
async def order_timeline(order_id: str, session: AsyncSession = Depends(get_session)):
    """История заказа одним ответом."""
    data = await reconciliation.order_timeline(session, order_id)
    if data["order"] is None:
        raise HTTPException(status_code=404, detail="order not found")
    return data


@router.post("/orders/{order_id}/redeliver")
async def redeliver(order_id: str, session: AsyncSession = Depends(get_session)):
    """Ручное добивание заказа: та же идемпотентная задача выдачи."""
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
    """Вернуть деньги за всё невыданное в заказе.

    Ручная кнопка для того же кода, что работает в фоне. Повтор ничего не
    задваивает.
    """
    row = (
        await session.execute(
            text("SELECT status, paid_at FROM orders WHERE id=:id"), {"id": order_id}
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")
    if row["paid_at"] is None:
        raise HTTPException(status_code=409, detail="order was never paid")
    if row["status"] == "delivered":
        raise HTTPException(status_code=409, detail="order is delivered, refund manually")

    item_ids = (
        await session.execute(
            text(
                "SELECT id FROM order_items WHERE order_id=:id "
                " AND status NOT IN ('delivered', 'refunded') ORDER BY position"
            ),
            {"id": order_id},
        )
    ).scalars().all()

    refunded = [
        item_id
        for item_id in item_ids
        if await settlement.refund_item(item_id, "manual admin refund")
    ]
    final = await settlement.finalize_order(order_id)
    log.info(
        "admin.refund", order_id=order_id, items=len(refunded), order_status=final
    )
    return {
        "order_id": order_id,
        "refunded": bool(refunded),
        "items_refunded": refunded,
        "order_status": final,
    }


@router.get("/money")
async def money(session: AsyncSession = Depends(get_session)):
    """Оплачено = выдано + возвращено + ещё не закрыто."""
    return await reconciliation.money_report(session)


@router.get("/discrepancies")
async def discrepancies(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=1000),
):
    """Расхождения с поставщиками и их разбор."""
    return await reconciliation.discrepancy_report(session, limit=limit)


@router.get("/orders/{order_id}/as-of")
async def order_as_of(
    order_id: str,
    at: datetime = Query(..., description="момент времени в ISO-8601"),
    session: AsyncSession = Depends(get_session),
):
    """Каким были заказ и деньги по нему на указанный момент."""
    state = await history.order_as_of(session, order_id, _aware(at))
    if state is None:
        raise HTTPException(
            status_code=404, detail="order did not exist at that moment (or never existed)"
        )
    return state


@router.get("/money/as-of")
async def money_as_of(
    at: datetime = Query(...), session: AsyncSession = Depends(get_session)
):
    """Деньги по всей базе на указанный момент."""
    return await history.money_as_of(session, _aware(at))


@router.get("/reports/period")
async def period_report(
    frm: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    session: AsyncSession = Depends(get_session),
):
    """Итоги за период, посчитанные из истории."""
    frm, to = _aware(frm), _aware(to)
    if to <= frm:
        raise HTTPException(status_code=422, detail="'to' must be later than 'from'")
    return await history.period_report(session, frm, to)


@router.get("/queue")
async def queue_progress(session: AsyncSession = Depends(get_session)):
    """Прогресс при всплеске: сколько ждёт, сколько выдано, что с лимитом."""
    queue = (
        await session.execute(
            text(
                """
                SELECT kind, state, priority, count(*) AS n,
                       ROUND(EXTRACT(EPOCH FROM (now() - min(run_at)))::numeric, 1)
                           AS oldest_seconds
                  FROM jobs
                 WHERE state IN ('pending', 'running')
                 GROUP BY kind, state, priority
                 ORDER BY priority, kind
                """
            )
        )
    ).mappings().all()
    items = (
        await session.execute(
            text(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE it.status = 'delivered') AS delivered,
                       count(*) FILTER (WHERE it.status = 'refunded')  AS refunded,
                       count(*) FILTER (WHERE it.status NOT IN ('delivered', 'refunded'))
                           AS in_progress
                  FROM order_items it
                  JOIN orders o ON o.id = it.order_id
                 WHERE o.paid_at IS NOT NULL
                """
            )
        )
    ).mappings().first()

    waiting = sum(r["n"] for r in queue)
    return {
        "queue": {
            "waiting": waiting,
            "by_kind": [dict(r) for r in queue],
        },
        "paid_items": dict(items),
        "suppliers": await ratelimit.snapshot(),
    }


@router.get("/jobs")
async def list_jobs(
    session: AsyncSession = Depends(get_session),
    state: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
):
    sql = (
        "SELECT id, kind, dedupe_key, state, priority, attempts, max_attempts, run_at, "
        "last_error "
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
