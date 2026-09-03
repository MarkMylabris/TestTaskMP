"""Обработка вебхука оплаты. Здесь живёт exactly-once из этапа 2.

Как 50 одновременных вебхуков превращаются в одну выдачу:

INSERT в payment_events с ON CONFLICT (event_id) DO NOTHING отсекает повторы -
дважды один event_id обработать физически нельзя, это первичный ключ.
Дальше SELECT ... FOR UPDATE по заказу: вебхуки с разными event_id по одному
заказу выстраиваются в очередь на строке. Переход created -> paid разрешён
только из created, так что первый выигрывает, а остальные 49 видят статус,
который уже не created, и становятся no-op.

Задача выдачи ставится в очередь в этой же транзакции. Это транзакционный
outbox: между "заказ оплачен" и "выдача запланирована" нет окна, в котором
задачу можно было бы потерять.

Наружу отвечаем быстро и без походов в сеть - к поставщику ходит воркер.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_conf import get_logger
from app.models import PaymentEvent
from app.services import jobs, ledger

log = get_logger("payments")


@dataclass(slots=True)
class WebhookResult:
    accepted: bool
    state: str            # applied | duplicate | ignored | orphan | rejected
    order_status: str | None = None
    note: str | None = None


async def handle_payment_event(
    session: AsyncSession,
    *,
    event_id: str,
    order_id: str,
    status: str,
    amount_minor: int,
    currency: str,
    event_created_at: datetime,
    payload: dict,
) -> WebhookResult:
    # --- 1. дедупликация по event_id ------------------------------------- #
    inserted = (
        await session.execute(
            pg_insert(PaymentEvent)
            .values(
                event_id=event_id,
                order_id=order_id,
                status=status,
                amount_minor=amount_minor,
                currency=currency,
                event_created_at=event_created_at,
                processing_state="ignored",   # уточним ниже
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(PaymentEvent.event_id)
        )
    ).scalar_one_or_none()

    if inserted is None:
        log.info("payment.duplicate", event_id=event_id, order_id=order_id, status=status)
        return WebhookResult(True, "duplicate", note="event already processed")

    # --- 2. блокировка заказа -------------------------------------------- #
    row = (
        await session.execute(
            text(
                "SELECT id, status, amount_minor, currency, sku "
                "FROM orders WHERE id = :id FOR UPDATE"
            ),
            {"id": order_id},
        )
    ).mappings().first()

    if row is None:
        # Вебхук пришёл раньше, чем заказ (или заказа нет вовсе).
        # Не 5xx: платёжка иначе будет долбить ретраями. Сохраняем как orphan
        # и досылаем сами фоновой задачей.
        await _set_event_state(session, event_id, "orphan", "order not found yet")
        await jobs.enqueue(
            session, jobs.KIND_APPLY_ORPHAN, dedupe_key=order_id, payload={"order_id": order_id}
        )
        log.warning("payment.orphan", event_id=event_id, order_id=order_id, status=status)
        return WebhookResult(True, "orphan", note="order not found, will retry")

    return await _apply_to_order(
        session,
        event_id=event_id,
        order=row,
        status=status,
        amount_minor=amount_minor,
        currency=currency,
    )


async def _apply_to_order(
    session: AsyncSession, *, event_id: str, order, status: str,
    amount_minor: int, currency: str,
) -> WebhookResult:
    order_id = order["id"]
    current = order["status"]

    # --- 3. валидация суммы ---------------------------------------------- #
    if status == "paid" and (
        amount_minor != order["amount_minor"] or currency != order["currency"]
    ):
        note = (
            f"amount mismatch: event {amount_minor} {currency} != "
            f"order {order['amount_minor']} {order['currency']}"
        )
        await _set_event_state(session, event_id, "rejected", note)
        log.error("payment.amount_mismatch", event_id=event_id, order_id=order_id, note=note)
        return WebhookResult(True, "rejected", current, note)

    # --- 4. переходы ------------------------------------------------------ #
    if status == "paid":
        if current != "created":
            # Повторная оплата уже оплаченного/выданного заказа - no-op.
            # Сюда же попадают 49 из 50 параллельных вебхуков.
            await _set_event_state(
                session, event_id, "ignored", f"order already in status {current}"
            )
            log.info(
                "payment.noop", event_id=event_id, order_id=order_id, order_status=current
            )
            return WebhookResult(True, "ignored", current, "already past 'created'")

        await session.execute(
            text(
                "UPDATE orders SET status='paid', paid_at=now(), updated_at=now() WHERE id=:id"
            ),
            {"id": order_id},
        )
        await ledger.post_payment(session, order_id, amount_minor, currency)
        # Outbox: задача выдачи в той же транзакции, что и смена статуса.
        await jobs.enqueue(
            session, jobs.KIND_DELIVER, dedupe_key=order_id, payload={"order_id": order_id}
        )
        await _set_event_state(session, event_id, "applied", "created -> paid")
        log.info(
            "payment.captured", event_id=event_id, order_id=order_id,
            amount_minor=amount_minor, currency=currency, order_status="paid",
        )
        return WebhookResult(True, "applied", "paid")

    # status == 'failed'
    if current == "created":
        await session.execute(
            text("UPDATE orders SET status='payment_failed', updated_at=now() WHERE id=:id"),
            {"id": order_id},
        )
        await _set_event_state(session, event_id, "applied", "created -> payment_failed")
        log.info("payment.failed", event_id=event_id, order_id=order_id)
        return WebhookResult(True, "applied", "payment_failed")

    # Вебхук вне порядка: `failed` пришёл после `paid`/`delivered`.
    # Финальные и оплаченные состояния не откатываем - только фиксируем аномалию.
    note = f"out-of-order 'failed' for order in status {current}"
    await _set_event_state(session, event_id, "ignored", note)
    log.warning("payment.out_of_order", event_id=event_id, order_id=order_id, note=note)
    return WebhookResult(True, "ignored", current, note)


async def _set_event_state(session: AsyncSession, event_id: str, state: str, note: str) -> None:
    await session.execute(
        text(
            "UPDATE payment_events SET processing_state=:s, note=:n, processed_at=now() "
            "WHERE event_id=:e"
        ),
        {"s": state, "n": note, "e": event_id},
    )


async def apply_orphan_events(session: AsyncSession, order_id: str) -> int:
    """Досылка "сиротских" событий, пришедших раньше заказа (этап 2, п.3)."""
    row = (
        await session.execute(
            text(
                "SELECT id, status, amount_minor, currency, sku FROM orders "
                "WHERE id=:id FOR UPDATE"
            ),
            {"id": order_id},
        )
    ).mappings().first()
    if row is None:
        return 0

    events = (
        await session.execute(
            text(
                """
                SELECT event_id, status, amount_minor, currency
                  FROM payment_events
                 WHERE order_id = :oid AND processing_state = 'orphan'
                 ORDER BY event_created_at, received_at
                """
            ),
            {"oid": order_id},
        )
    ).mappings().all()

    applied = 0
    for ev in events:
        # Перечитываем статус: предыдущее событие могло его изменить.
        fresh = (
            await session.execute(
                text("SELECT id, status, amount_minor, currency, sku FROM orders WHERE id=:id"),
                {"id": order_id},
            )
        ).mappings().first()
        res = await _apply_to_order(
            session,
            event_id=ev["event_id"],
            order=fresh,
            status=ev["status"],
            amount_minor=ev["amount_minor"],
            currency=ev["currency"],
        )
        if res.state == "applied":
            applied += 1
    return applied


async def mark_delivered_if_issued(session: AsyncSession, order_id: str) -> bool:
    """Если выдача уже есть - заказ обязан быть delivered (идемпотентная финализация)."""
    code = (
        await session.execute(
            text("SELECT code FROM issuances WHERE order_id=:id"), {"id": order_id}
        )
    ).scalar_one_or_none()
    if code is None:
        return False
    await session.execute(
        text(
            "UPDATE orders SET status='delivered', "
            "delivered_at=COALESCE(delivered_at, now()), updated_at=now() "
            "WHERE id=:id AND status <> 'delivered'"
        ),
        {"id": order_id},
    )
    return True
