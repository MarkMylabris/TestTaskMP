"""Журнал денежных движений двойной записью.

Знак задаёт сторону: amount_minor > 0 это дебет, < 0 кредит. Сумма строк одной
проводки всегда ноль, поэтому и весь журнал всегда сходится в ноль - проверять
это можно одним SELECT SUM.

Задвоить проводку нельзя: UNIQUE(order_id, kind, account). Это важно, потому что
обработчики идемпотентны и вполне могут отработать повторно.
"""
from __future__ import annotations

import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LedgerEntry

# Условная себестоимость закупки кода у поставщика - 70% от цены.
COST_RATE = 0.70


def cost_of(amount_minor: int) -> int:
    return int(round(amount_minor * COST_RATE))


async def post(
    session: AsyncSession,
    *,
    order_id: str | None,
    kind: str,
    currency: str,
    legs: list[tuple[str, int]],
    meta: dict | None = None,
) -> uuid.UUID | None:
    """Записать сбалансированную проводку. Идемпотентно по (order_id, kind, account).

    Возвращает txn_id или None, если проводка уже была сделана раньше.
    """
    total = sum(amount for _, amount in legs)
    if total != 0:
        raise ValueError(f"unbalanced ledger transaction {kind}: sum={total}")

    txn_id = uuid.uuid4()
    stmt = (
        pg_insert(LedgerEntry)
        .values(
            [
                {
                    "txn_id": txn_id,
                    "order_id": order_id,
                    "account": account,
                    "amount_minor": amount,
                    "currency": currency,
                    "kind": kind,
                    "meta": meta,
                }
                for account, amount in legs
            ]
        )
        .on_conflict_do_nothing(constraint="uq_ledger_once_per_order_kind")
        .returning(LedgerEntry.id)
    )
    inserted = (await session.execute(stmt)).scalars().all()
    return txn_id if inserted else None


async def post_payment(session: AsyncSession, order_id: str, amount_minor: int, currency: str):
    """Оплата принята: деньги пришли, у нас возникло обязательство выдать товар."""
    return await post(
        session,
        order_id=order_id,
        kind="payment_captured",
        currency=currency,
        legs=[("customer", amount_minor), ("revenue", -amount_minor)],
    )


async def post_delivery(session: AsyncSession, order_id: str, amount_minor: int, currency: str):
    """Выдача кода: списываем себестоимость со склада."""
    cost = cost_of(amount_minor)
    return await post(
        session,
        order_id=order_id,
        kind="delivery_cost",
        currency=currency,
        legs=[("supplier_cost", cost), ("inventory", -cost)],
    )


async def post_refund(session: AsyncSession, order_id: str, amount_minor: int, currency: str):
    """Возврат по невыдаваемому заказу - обратная проводка к оплате."""
    return await post(
        session,
        order_id=order_id,
        kind="refund",
        currency=currency,
        legs=[("refund", amount_minor), ("customer", -amount_minor)],
    )
