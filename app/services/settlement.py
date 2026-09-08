"""Закрытие заказа: что выдали - остаётся, за что не смогли - возвращаем.

Позиция закрывается ровно одним способом, и оба идемпотентны:

    выдача   issuances.order_item_id UNIQUE       второй код не выдать
    возврат  status='refunded' + UNIQUE проводки  второй возврат не провести

Отсюда "оплачено = выдано + возвращено": каждая позиция оплаченного заказа
попадает ровно в одно слагаемое.

Деньги возвращаются по двум признакам: позиция `unavailable` (такое не
продают, ждать нечего) или исчерпан бюджет попыток. `out_of_stock` поводом
не является - остаток завозят, и заказ доводится сам.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.services import events, jobs, ledger

log = get_logger("settlement")

async def schedule_refunds(session: AsyncSession, order_id: str) -> int:
    """Поставить возвраты по позициям, которые выдать уже не выйдет."""
    rows = (
        await session.execute(
            text(
                """
                SELECT it.id, it.status, it.delivery_attempts, it.last_error
                  FROM order_items it
                  JOIN orders o ON o.id = it.order_id
                 WHERE it.order_id = :oid
                   AND o.paid_at IS NOT NULL
                   AND it.status IN ('out_of_stock', 'delivery_failed', 'unavailable')
                """
            ),
            {"oid": order_id},
        )
    ).mappings().all()

    scheduled = 0
    for row in rows:
        permanent = row["status"] == "unavailable"
        exhausted = row["delivery_attempts"] >= settings.delivery_max_attempts
        if not (permanent or exhausted):
            continue
        reason = "supplier cannot fulfil" if permanent else "delivery attempts exhausted"
        if await jobs.enqueue(
            session,
            jobs.KIND_REFUND_ITEM,
            dedupe_key=row["id"],
            payload={"item_id": row["id"], "order_id": order_id, "reason": reason},
        ):
            scheduled += 1
            log.info(
                "settlement.refund_scheduled", item_id=row["id"], order_id=order_id,
                item_status=row["status"], attempts=row["delivery_attempts"], reason=reason,
            )
    return scheduled


async def settle_order(session: AsyncSession, order_id: str, reason: str) -> int:
    """Досрочно закрыть заказ: всё невыданное уходит в возврат.

    Бюджет задачи исчерпан либо возврат запросили руками.
    """
    ids = (
        await session.execute(
            text(
                "SELECT it.id FROM order_items it JOIN orders o ON o.id = it.order_id "
                " WHERE it.order_id = :oid AND o.paid_at IS NOT NULL "
                "   AND it.status NOT IN ('delivered', 'refunded')"
            ),
            {"oid": order_id},
        )
    ).scalars().all()
    scheduled = 0
    for item_id in ids:
        if await jobs.enqueue(
            session,
            jobs.KIND_REFUND_ITEM,
            dedupe_key=item_id,
            payload={"item_id": item_id, "order_id": order_id, "reason": reason},
        ):
            scheduled += 1
    if scheduled:
        log.warning(
            "settlement.order_settled", order_id=order_id, items=scheduled, reason=reason
        )
    return scheduled


async def refund_item(item_id: str, reason: str) -> bool:
    """Вернуть деньги за позицию. True, если возврат произошёл сейчас."""
    async with session_scope() as s:
        row = (
            await s.execute(
                text(
                    "SELECT it.id, it.order_id, it.amount_minor, it.currency, it.status, "
                    "       o.paid_at "
                    "  FROM order_items it JOIN orders o ON o.id = it.order_id "
                    " WHERE it.id = :id FOR UPDATE OF it"
                ),
                {"id": item_id},
            )
        ).mappings().first()

        if row is None:
            log.warning("settlement.refund_skipped", item_id=item_id, note="item not found")
            return False
        if row["status"] == "delivered":
            # Код у покупателя, возвращать нечего.
            log.info("settlement.refund_skipped", item_id=item_id, note="already delivered")
            return False
        if row["status"] == "refunded":
            return False
        if row["paid_at"] is None:
            # Денег не было.
            log.info("settlement.refund_skipped", item_id=item_id, note="never paid")
            return False

        await s.execute(
            text(
                "UPDATE order_items SET status='refunded', refunded_at=now(), "
                "last_error=:e, updated_at=now() "
                " WHERE id=:id AND status NOT IN ('delivered', 'refunded')"
            ),
            {"id": item_id, "e": f"refunded: {reason}"},
        )
        # Возврат закрывает и расхождения: разбирать больше нечего.
        await s.execute(
            text(
                "UPDATE supplier_discrepancies SET resolved_at = now(), "
                "resolution = 'item refunded' WHERE order_item_id = :iid "
                "  AND resolved_at IS NULL"
            ),
            {"iid": item_id},
        )
        txn = await ledger.post_refund(
            s, row["order_id"], row["amount_minor"], row["currency"], item_id=item_id
        )
        await events.emit(
            s, row["order_id"], "item_refunded", item_id=item_id,
            amount_minor=row["amount_minor"], currency=row["currency"], reason=reason,
        )
        log.info(
            "settlement.refunded", item_id=item_id, order_id=row["order_id"],
            amount_minor=row["amount_minor"], reason=reason,
            txn=str(txn) if txn else None,
        )
    return True


async def finalize_order(order_id: str) -> str:
    """Свести статус заказа из статусов позиций.

    delivered - выдали всё; partially_delivered - часть выдали, за остальное
    вернули деньги; refunded - не выдали ничего.
    """
    async with session_scope() as s:
        return await finalize_order_in(s, order_id)


async def finalize_order_in(session: AsyncSession, order_id: str) -> str:
    rows = (
        await session.execute(
            text("SELECT status, last_error FROM order_items WHERE order_id=:id"),
            {"id": order_id},
        )
    ).mappings().all()
    statuses = [r["status"] for r in rows]
    if not statuses:
        return "created"

    delivered = sum(st == "delivered" for st in statuses)
    refunded = sum(st == "refunded" for st in statuses)

    if delivered + refunded == len(statuses):
        status = (
            "delivered" if refunded == 0
            else "refunded" if delivered == 0
            else "partially_delivered"
        )
        note = None if status == "delivered" else f"{delivered} delivered, {refunded} refunded"
        await session.execute(
            text(
                "UPDATE orders SET status=:st, last_error=:e, "
                "delivered_at=CASE WHEN :d > 0 THEN COALESCE(delivered_at, now()) "
                "                  ELSE delivered_at END, "
                "updated_at=now() "
                " WHERE id=:id AND status <> :st"
            ),
            {"id": order_id, "st": status, "e": note, "d": delivered},
        )
        await events.order_status(
            session, order_id, status, delivered=delivered, refunded=refunded
        )
        log.info(
            "order.finalized", order_id=order_id, status=status,
            delivered=delivered, refunded=refunded,
        )
        return status

    # Не всё решено: заказ остаётся в рабочем состоянии.
    return "delivering"
