from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_conf import get_logger
from app.models import Order, Product
from app.services import jobs

log = get_logger("orders")


def new_order_id() -> str:
    return f"ord_{uuid.uuid4().hex[:16]}"


class OrderConflict(Exception):
    """Заказ с таким id уже есть, но с другим SKU."""


async def create_order(
    session: AsyncSession,
    sku: str,
    customer_email: str | None = None,
    order_id: str | None = None,
) -> tuple[Order, bool]:
    """Создать заказ. Возвращает (заказ, создан_ли_сейчас).

    При переданном order_id создание идемпотентно: повтор с тем же id и SKU
    вернёт существующий заказ, а не заведёт второй.
    """
    product = await session.get(Product, sku)
    if product is None or not product.is_active:
        raise LookupError(sku)

    if order_id is not None:
        existing = await session.get(Order, order_id)
        if existing is not None:
            if existing.sku != sku:
                raise OrderConflict(order_id)
            return existing, False

    order = Order(
        id=order_id or new_order_id(),
        sku=sku,
        amount_minor=product.price_minor,
        currency=product.currency,
        status="created",
        customer_email=customer_email,
    )
    session.add(order)
    await session.flush()
    log.info(
        "order.created", order_id=order.id, sku=sku,
        amount_minor=order.amount_minor, currency=order.currency,
    )
    # Вебхук мог прийти раньше заказа и осесть как orphan. Ставим задачу
    # только если такие события действительно есть: на потоке заказов лишние
    # строки в очереди ни к чему, а "доводчик" подхватит их в любом случае.
    has_orphans = (
        await session.execute(
            text(
                "SELECT 1 FROM payment_events "
                "WHERE order_id = :id AND processing_state = 'orphan' LIMIT 1"
            ),
            {"id": order.id},
        )
    ).scalar_one_or_none()
    if has_orphans:
        await jobs.enqueue(
            session, jobs.KIND_APPLY_ORPHAN, dedupe_key=order.id, payload={"order_id": order.id}
        )
    return order, True


async def get_order(session: AsyncSession, order_id: str) -> Order | None:
    return await session.scalar(select(Order).where(Order.id == order_id))
