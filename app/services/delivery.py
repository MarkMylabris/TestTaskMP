"""Выдача товара. Ровно один код на заказ.

Шаг выдачи намеренно разбит на три фазы:

    1) короткая транзакция - заблокировать заказ, проверить инварианты,
       перевести в delivering, закоммитить;
    2) сеть - обращение к поставщикам БЕЗ открытой транзакции;
    3) короткая транзакция - зафиксировать выдачу.

Держать транзакцию открытой во время HTTP-вызова с таймаутом в пару секунд -
верный способ выесть пул соединений и растянуть блокировку строки на всю длину
этого таймаута. Поэтому сеть строго между транзакциями.

От двойной выдачи защищают сразу четыре вещи, и это не паранойя, а разные
уровни отказа: частичный уникальный индекс на jobs (активная задача по заказу
одна), SELECT FOR UPDATE на заказе, issuances.order_id UNIQUE и issuances.code
UNIQUE. Первые два можно случайно обойти рефакторингом, последние два - нет.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import Issuance
from app.services import ledger
from app.services.supplier_client import SupplierClient

log = get_logger("delivery")


@dataclass(slots=True)
class DeliveryResult:
    status: str          # delivered | out_of_stock | delivery_failed | unresolved | skipped
    code: str | None = None
    supplier: str | None = None
    note: str | None = None
    retry_after: timedelta | None = None


async def deliver_order(order_id: str, client: SupplierClient) -> DeliveryResult:
    # ---------- фаза 1: захват ------------------------------------------- #
    async with session_scope() as s:
        row = (
            await s.execute(
                text(
                    "SELECT id, sku, status, amount_minor, currency, delivery_attempts "
                    "FROM orders WHERE id=:id FOR UPDATE"
                ),
                {"id": order_id},
            )
        ).mappings().first()

        if row is None:
            return DeliveryResult("skipped", note="order not found")

        # Уже есть выдача -> просто досводим статус. Идемпотентность.
        existing = (
            await s.execute(
                text("SELECT code, supplier FROM issuances WHERE order_id=:id"), {"id": order_id}
            )
        ).mappings().first()
        if existing:
            await _finalize_delivered(s, order_id)
            log.info(
                "delivery.already_done", order_id=order_id, code=existing["code"],
                supplier=existing["supplier"],
            )
            return DeliveryResult(
                "delivered", code=existing["code"], supplier=existing["supplier"],
                note="idempotent replay",
            )

        if row["status"] in ("payment_failed",):
            return DeliveryResult("skipped", note=f"order status {row['status']}")
        if row["status"] == "created":
            # Выдача до оплаты запрещена.
            return DeliveryResult("skipped", note="not paid yet")

        sku = row["sku"]
        amount_minor = row["amount_minor"]
        currency = row["currency"]
        attempts = row["delivery_attempts"] + 1

        await s.execute(
            text(
                "UPDATE orders SET status='delivering', delivery_attempts=:a, updated_at=now() "
                "WHERE id=:id"
            ),
            {"id": order_id, "a": attempts},
        )
        log.info("delivery.started", order_id=order_id, sku=sku, attempt=attempts)

    # ---------- фаза 2: сеть (без транзакции) ----------------------------- #
    outcome = await client.acquire_code(order_id, sku)

    # ---------- фаза 3: фиксация ------------------------------------------ #
    async with session_scope() as s:
        if outcome.kind == "ok":
            inserted = (
                await s.execute(
                    pg_insert(Issuance)
                    .values(
                        order_id=order_id,
                        sku=sku,
                        code=outcome.code,
                        supplier=outcome.supplier,
                        request_id=outcome.request_id,
                    )
                    .on_conflict_do_nothing()
                    .returning(Issuance.id)
                )
            ).scalar_one_or_none()

            own_code = (
                await s.execute(
                    text("SELECT code FROM issuances WHERE order_id=:id"), {"id": order_id}
                )
            ).scalar_one_or_none()
            if own_code is None:
                # Вставка не прошла, но и своей выдачи нет: код уже принадлежит
                # другому заказу. Молча "доставить" чужой ключ нельзя.
                await s.execute(
                    text(
                        "UPDATE orders SET status='delivery_failed', last_error=:e, "
                        "updated_at=now() WHERE id=:id "
                        "AND status NOT IN ('delivered','payment_failed')"
                    ),
                    {"id": order_id, "e": f"code collision: {outcome.code}"},
                )
                log.error(
                    "delivery.code_collision", order_id=order_id, code=outcome.code,
                    supplier=outcome.supplier, request_id=outcome.request_id,
                )
                return DeliveryResult(
                    "delivery_failed", note="code already issued to another order"
                )

            await _finalize_delivered(s, order_id)
            await ledger.post_delivery(s, order_id, amount_minor, currency)

            # Остаток витрины уменьшаем на факт выдачи.
            await s.execute(
                text(
                    "UPDATE sku_stock SET available = GREATEST(available - 1, 0), "
                    "updated_at = now() WHERE sku = :sku"
                ),
                {"sku": sku},
            )

            code = own_code  # всегда код, реально привязанный к этому заказу

            log.info(
                "delivery.completed", order_id=order_id, sku=sku, code=code,
                supplier=outcome.supplier, request_id=outcome.request_id,
                first_time=inserted is not None,
            )
            return DeliveryResult("delivered", code=code, supplier=outcome.supplier)

        if outcome.kind == "out_of_stock":
            await s.execute(
                text(
                    "UPDATE orders SET status='out_of_stock', last_error=:e, updated_at=now() "
                    "WHERE id=:id AND status NOT IN ('delivered','payment_failed')"
                ),
                {"id": order_id, "e": "out_of_stock at all suppliers"},
            )
            log.warning("delivery.out_of_stock", order_id=order_id, sku=sku)
            return DeliveryResult(
                "out_of_stock", note="no stock at A and B", retry_after=timedelta(seconds=10)
            )

        if outcome.kind == "unknown":
            # Исход у поставщика не выяснен. НЕ переключаемся на другого,
            # НЕ считаем отказом. Оставляем восстановимое состояние и повторим
            # тем же request_id.
            await s.execute(
                text(
                    "UPDATE orders SET status='delivering', last_error=:e, updated_at=now() "
                    "WHERE id=:id AND status NOT IN ('delivered','payment_failed')"
                ),
                {"id": order_id, "e": f"unresolved supplier outcome: {outcome.reason}"},
            )
            log.warning(
                "delivery.unresolved", order_id=order_id, supplier=outcome.supplier,
                request_id=outcome.request_id, reason=outcome.reason,
            )
            return DeliveryResult(
                "unresolved", supplier=outcome.supplier, note=outcome.reason,
                retry_after=timedelta(seconds=2),
            )

        await s.execute(
            text(
                "UPDATE orders SET status='delivery_failed', last_error=:e, updated_at=now() "
                "WHERE id=:id AND status NOT IN ('delivered','payment_failed')"
            ),
            {"id": order_id, "e": str(outcome.reason)},
        )
        log.error("delivery.failed", order_id=order_id, sku=sku, reason=outcome.reason)
        return DeliveryResult(
            "delivery_failed", note=outcome.reason, retry_after=timedelta(seconds=5)
        )


async def _finalize_delivered(session, order_id: str) -> None:
    await session.execute(
        text(
            "UPDATE orders SET status='delivered', "
            "delivered_at=COALESCE(delivered_at, now()), last_error=NULL, updated_at=now() "
            "WHERE id=:id AND status <> 'delivered'"
        ),
        {"id": order_id},
    )


async def max_delivery_attempts_reached(order_id: str) -> bool:
    async with session_scope() as s:
        n = (
            await s.execute(
                text("SELECT delivery_attempts FROM orders WHERE id=:id"), {"id": order_id}
            )
        ).scalar_one_or_none()
    return (n or 0) >= settings.delivery_max_attempts
