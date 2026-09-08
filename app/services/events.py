"""Запись в append-only историю заказа.

`orders` и `order_items` отвечают на вопрос "как сейчас", история - на "как
было в 14:32". Событие пишется той же транзакцией, что и само изменение:
иначе появится окно, в котором картина разойдётся с настоящей.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OrderEvent


async def emit(
    session: AsyncSession,
    order_id: str,
    type_: str,
    *,
    item_id: str = "",
    **payload,
) -> None:
    session.add(
        OrderEvent(order_id=order_id, item_id=item_id, type=type_, payload=payload or {})
    )


async def item_status(
    session: AsyncSession, order_id: str, item_id: str, status: str, **payload
) -> None:
    await emit(session, order_id, "item_status", item_id=item_id, status=status, **payload)


async def order_status(session: AsyncSession, order_id: str, status: str, **payload) -> None:
    await emit(session, order_id, "order_status", status=status, **payload)
