"""Создание и чтение заказа.

Заказ многотоварный: `orders` - агрегат, единица выдачи и денег - позиция.
Сумма заказа равна сумме позиций, эту величину и сверяет вебхук оплаты.

id позиции детерминированный (`<order_id>.<position>`): он уходит в request_id
и в dedupe_key задачи, поэтому обязан совпадать при любом повторе. Разделитель
точка, а не решётка: request_id попадает в путь URL.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_conf import get_logger
from app.models import Order, OrderItem, Product
from app.services import events, jobs

log = get_logger("orders")

MAX_ITEMS = 50


def new_order_id() -> str:
    return f"ord_{uuid.uuid4().hex[:16]}"


def item_id(order_id: str, position: int) -> str:
    return f"{order_id}.{position}"


class OrderConflict(Exception):
    """Заказ с таким id уже есть, но с другим составом."""


class MixedCurrency(Exception):
    """В одном заказе товары в разных валютах."""


async def create_order(
    session: AsyncSession,
    skus: list[str],
    customer_email: str | None = None,
    order_id: str | None = None,
) -> tuple[Order, list[OrderItem], bool]:
    """Создать заказ: одна позиция на единицу товара.

    Возвращает (заказ, позиции, создан_ли_сейчас). С переданным order_id
    создание идемпотентно: повтор с тем же составом вернёт существующий заказ.
    """
    if not skus:
        raise ValueError("order must contain at least one item")
    if len(skus) > MAX_ITEMS:
        raise ValueError(f"too many items: {len(skus)} > {MAX_ITEMS}")

    products: dict[str, Product] = {}
    for sku in skus:
        if sku not in products:
            product = await session.get(Product, sku)
            if product is None or not product.is_active:
                raise LookupError(sku)
            products[sku] = product

    currencies = {p.currency for p in products.values()}
    if len(currencies) > 1:
        raise MixedCurrency(", ".join(sorted(currencies)))
    currency = currencies.pop()

    if order_id is not None:
        existing = await session.get(Order, order_id)
        if existing is not None:
            items = await get_items(session, order_id)
            if sorted(i.sku for i in items) != sorted(skus):
                raise OrderConflict(order_id)
            return existing, items, False

    oid = order_id or new_order_id()
    total = sum(products[sku].price_minor for sku in skus)
    order = Order(
        id=oid,
        amount_minor=total,
        currency=currency,
        status="created",
        customer_email=customer_email,
    )
    session.add(order)
    items = [
        OrderItem(
            id=item_id(oid, position),
            order_id=oid,
            position=position,
            sku=sku,
            amount_minor=products[sku].price_minor,
            currency=products[sku].currency,
            status="pending",
        )
        for position, sku in enumerate(skus, start=1)
    ]
    session.add_all(items)
    await events.emit(
        session, oid, "order_created",
        amount_minor=total, currency=currency,
        items=[{"id": i.id, "sku": i.sku, "amount_minor": i.amount_minor} for i in items],
    )
    await session.flush()
    log.info(
        "order.created", order_id=oid, items=len(items), skus=skus,
        amount_minor=total, currency=currency,
    )
    # Вебхук мог прийти раньше заказа и осесть как orphan. Задачу ставим,
    # только если такие события есть: лишние строки в очереди ни к чему.
    has_orphans = (
        await session.execute(
            text(
                "SELECT 1 FROM payment_events "
                "WHERE order_id = :id AND processing_state = 'orphan' LIMIT 1"
            ),
            {"id": oid},
        )
    ).scalar_one_or_none()
    if has_orphans:
        await jobs.enqueue(
            session, jobs.KIND_APPLY_ORPHAN, dedupe_key=oid, payload={"order_id": oid}
        )
    return order, items, True


async def get_items(session: AsyncSession, order_id: str) -> list[OrderItem]:
    return list(
        (
            await session.scalars(
                select(OrderItem)
                .where(OrderItem.order_id == order_id)
                .order_by(OrderItem.position)
            )
        ).all()
    )
