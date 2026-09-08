"""Журнал денежных движений двойной записью.

Схема счетов держит инвариант "оплачено = выдано + возвращено":

    оплата        customer +A       revenue   -A    возникло обязательство
    выдача        delivered +a      customer  -a    закрыто кодом
    возврат       refund    +a      customer  -a    закрыто деньгами
    себестоимость supplier_cost +c  inventory -c    контур закупки

Остаток по `customer` у завершённого заказа обязан быть нулём - это и
проверяет сверка, одним SELECT.

Знак задаёт сторону: > 0 дебет, < 0 кредит; сумма строк проводки всегда ноль,
поэтому и весь журнал сходится в ноль. Задвоить проводку не даёт
UNIQUE(order_id, item_id, kind, account) - обработчики идемпотентны и
повторяются. Оплата идёт на уровне заказа, выдача и возврат - на уровне позиции.
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
    item_id: str = "",
    meta: dict | None = None,
) -> uuid.UUID | None:
    """Сбалансированная проводка, идемпотентная по (заказ, позиция, повод).

    Возвращает txn_id или None, если проводка уже была сделана.
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
                    "item_id": item_id,
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
    """Оплата принята: возникло обязательство выдать товар."""
    return await post(
        session,
        order_id=order_id,
        kind="payment_captured",
        currency=currency,
        legs=[("customer", amount_minor), ("revenue", -amount_minor)],
    )


async def post_delivery(
    session: AsyncSession, order_id: str, item_id: str, amount_minor: int, currency: str
):
    """Выдача кода: гасим обязательство и списываем себестоимость.

    Две проводки, а не одна: в отчётах они нужны порознь.
    """
    settled = await post(
        session,
        order_id=order_id,
        item_id=item_id,
        kind="delivery_settled",
        currency=currency,
        legs=[("delivered", amount_minor), ("customer", -amount_minor)],
    )
    cost = cost_of(amount_minor)
    await post(
        session,
        order_id=order_id,
        item_id=item_id,
        kind="delivery_cost",
        currency=currency,
        legs=[("supplier_cost", cost), ("inventory", -cost)],
    )
    return settled


async def post_refund(
    session: AsyncSession, order_id: str, amount_minor: int, currency: str, item_id: str = ""
):
    """Возврат: обратная проводка к оплате, по позиции или по заказу."""
    return await post(
        session,
        order_id=order_id,
        item_id=item_id,
        kind="refund",
        currency=currency,
        legs=[("refund", amount_minor), ("customer", -amount_minor)],
    )
