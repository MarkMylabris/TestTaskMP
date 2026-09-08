"""Выдача товара: ровно один код на позицию заказа.

Единица выдачи - позиция, у каждой свой поставщик и свой исход. Статус заказа
считается из статусов позиций после обхода всех.

Шаг выдачи разбит на три фазы: короткая транзакция (заблокировать позицию,
перевести в delivering), сеть без открытой транзакции, короткая транзакция
(зафиксировать выдачу). Держать транзакцию открытой на время HTTP-вызова -
выесть пул соединений и растянуть блокировку строки на весь таймаут.

От двойной выдачи защищают четыре разных уровня: частичный уникальный индекс
на jobs, FOR UPDATE на позиции, issuances.order_item_id UNIQUE и
issuances.code UNIQUE. Первые два обходятся рефакторингом, последние два нет.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import ITEM_TERMINAL_STATUSES, Issuance
from app.services import events, ledger, settlement
from app.services import supplier_client
from app.services.supplier_client import SupplierClient

log = get_logger("delivery")

_RESOLVE_DISCREPANCIES = text(
    """
    UPDATE supplier_discrepancies
       SET resolved_at = now(), resolution = :res
     WHERE order_item_id = :iid AND resolved_at IS NULL
    """
)


# Насколько плох исход: заказ наследует худший статус своих позиций.
_SEVERITY = {
    "delivered": 0,
    "refunded": 0,
    "delivering": 1,
    "pending": 1,
    "out_of_stock": 2,
    "delivery_failed": 3,
    "unavailable": 3,
}
# Как исход позиции выглядит на уровне заказа.
_ORDER_STATUS_OF = {
    "out_of_stock": "out_of_stock",
    "delivery_failed": "delivery_failed",
    "unavailable": "delivery_failed",
}


@dataclass(slots=True)
class DeliveryResult:
    status: str          # delivered | out_of_stock | delivery_failed | unresolved
                         # | unavailable | throttled | settled | skipped
    code: str | None = None
    supplier: str | None = None
    note: str | None = None
    retry_after: timedelta | None = None


# --------------------------------------------------------------------------- #
# Заказ целиком
# --------------------------------------------------------------------------- #
async def deliver_order(order_id: str, client: SupplierClient) -> DeliveryResult:
    """Пройти по всем невыданным позициям заказа и свести его статус."""
    async with session_scope() as s:
        order = (
            await s.execute(
                text("SELECT id, status FROM orders WHERE id=:id"), {"id": order_id}
            )
        ).mappings().first()
        if order is None:
            return DeliveryResult("skipped", note="order not found")
        if order["status"] == "payment_failed":
            return DeliveryResult("skipped", note="order status payment_failed")
        if order["status"] == "created":
            # Выдача до оплаты запрещена.
            return DeliveryResult("skipped", note="not paid yet")

        pending = (
            await s.execute(
                text(
                    "SELECT id FROM order_items "
                    " WHERE order_id=:id AND status NOT IN ('delivered','refunded') "
                    " ORDER BY position"
                ),
                {"id": order_id},
            )
        ).scalars().all()

        await s.execute(
            text(
                "UPDATE orders SET status='delivering', delivery_attempts=delivery_attempts+1, "
                "updated_at=now() WHERE id=:id AND status NOT IN "
                "('delivered','partially_delivered','refunded','payment_failed')"
            ),
            {"id": order_id},
        )

    results = [await deliver_item(item_id, client) for item_id in pending]
    return await _finalize_order(order_id, results)


async def _finalize_order(order_id: str, results: list[DeliveryResult]) -> DeliveryResult:
    """Свести заказ: назвать статус и запланировать возвраты за невыданное.

    Возврат только планируется задачей, деньги двигает `settlement`. Повтор
    шага выдачи не задвоит его: задача дедуплицируется по позиции, проводка -
    по (заказ, позиция, повод).
    """
    async with session_scope() as s:
        await settlement.schedule_refunds(s, order_id)
        rows = (
            await s.execute(
                text(
                    "SELECT status, delivery_attempts, last_error "
                    "  FROM order_items WHERE order_id=:id"
                ),
                {"id": order_id},
            )
        ).mappings().all()

        statuses = [r["status"] for r in rows]
        note = next((r["last_error"] for r in rows if r["last_error"]), None)

        if statuses and all(st == "delivered" for st in statuses):
            await settlement.finalize_order_in(s, order_id)
            codes = [r for r in results if r.status == "delivered"]
            single = len(results) == 1 and codes
            return DeliveryResult(
                "delivered",
                code=codes[0].code if single else None,
                supplier=codes[0].supplier if single else None,
            )

        # Позиции, по которым ещё имеет смысл ходить к поставщику.
        awaiting = [
            r for r in rows
            if r["status"] not in ("delivered", "refunded", "unavailable")
            and r["delivery_attempts"] < settings.delivery_max_attempts
        ]
        throttled = [r for r in results if r.status == "throttled"]
        if throttled:
            # Ждём окна лимита, статус заказа не портим.
            return DeliveryResult(
                "throttled",
                note=throttled[0].note,
                retry_after=min(
                    (r.retry_after for r in throttled if r.retry_after), default=None
                ),
            )

        if not awaiting:
            # Выдавать нечего: остаток закроют запланированные возвраты.
            await settlement.finalize_order_in(s, order_id)
            return DeliveryResult("settled", note=note)

        worst = max(
            (r["status"] for r in awaiting),
            key=lambda st: _SEVERITY.get(st, 1),
            default="delivering",
        )
        status = _ORDER_STATUS_OF.get(worst, "delivering")
        await s.execute(
            text(
                "UPDATE orders SET status=:st, last_error=:e, updated_at=now() "
                "WHERE id=:id AND status NOT IN "
                "('delivered','partially_delivered','refunded','payment_failed')"
            ),
            {"id": order_id, "st": status, "e": note},
        )
        await events.order_status(s, order_id, status, note=note)

    unresolved = any(r.status == "unresolved" for r in results)
    retry = min(
        (r.retry_after for r in results if r.retry_after is not None),
        default=timedelta(seconds=5),
    )
    return DeliveryResult(
        "unresolved" if unresolved else status, note=note, retry_after=retry
    )


# --------------------------------------------------------------------------- #
# Одна позиция
# --------------------------------------------------------------------------- #
async def deliver_item(item_id: str, client: SupplierClient) -> DeliveryResult:
    # ---------- фаза 1: захват ------------------------------------------- #
    async with session_scope() as s:
        row = (
            await s.execute(
                text(
                    "SELECT i.id, i.order_id, i.sku, i.status, i.amount_minor, i.currency, "
                    "       i.delivery_attempts, o.status AS order_status "
                    "  FROM order_items i JOIN orders o ON o.id = i.order_id "
                    " WHERE i.id = :id FOR UPDATE OF i"
                ),
                {"id": item_id},
            )
        ).mappings().first()

        if row is None:
            return DeliveryResult("skipped", note="item not found")

        # Выдача уже есть: досводим статус, ничего не запрашивая.
        existing = (
            await s.execute(
                text("SELECT code, supplier FROM issuances WHERE order_item_id=:id"),
                {"id": item_id},
            )
        ).mappings().first()
        if existing:
            await _finalize_item(
                s, item_id, existing["supplier"],
                order_id=row["order_id"], code=existing["code"],
            )
            log.info(
                "delivery.already_done", item_id=item_id, code=existing["code"],
                supplier=existing["supplier"],
            )
            return DeliveryResult(
                "delivered", code=existing["code"], supplier=existing["supplier"],
                note="idempotent replay",
            )

        if row["status"] in ITEM_TERMINAL_STATUSES:
            return DeliveryResult("skipped", note=f"item status {row['status']}")
        if row["order_status"] == "payment_failed":
            return DeliveryResult("skipped", note="order status payment_failed")
        if row["order_status"] == "created":
            return DeliveryResult("skipped", note="not paid yet")

        order_id = row["order_id"]
        sku = row["sku"]
        amount_minor = row["amount_minor"]
        currency = row["currency"]
        attempts = row["delivery_attempts"] + 1

        await s.execute(
            text(
                "UPDATE order_items SET status='delivering', delivery_attempts=:a, "
                "updated_at=now() WHERE id=:id"
            ),
            {"id": item_id, "a": attempts},
        )
        await events.item_status(s, order_id, item_id, "delivering", attempt=attempts)
        log.info(
            "delivery.started", item_id=item_id, order_id=order_id, sku=sku,
            attempt=attempts,
        )

    # ---------- фаза 2: сеть (без транзакции) ----------------------------- #
    outcome = await client.acquire_code(order_id, item_id, sku)

    # ---------- фаза 3: фиксация ------------------------------------------ #
    async with session_scope() as s:
        if outcome.kind == "throttled":
            # Лимит поставщика - не попытка выдачи: откатываем счётчик, иначе
            # всплеск съест бюджет попыток и закончится возвратами.
            await s.execute(
                text(
                    "UPDATE order_items SET delivery_attempts = GREATEST(:a - 1, 0), "
                    "status='pending', last_error=:e, updated_at=now() "
                    " WHERE id=:id AND status NOT IN ('delivered','refunded')"
                ),
                {"id": item_id, "a": attempts, "e": outcome.reason},
            )
            log.info(
                "delivery.throttled", item_id=item_id, order_id=order_id,
                supplier=outcome.supplier, wait_seconds=outcome.retry_after,
            )
            return DeliveryResult(
                "throttled", supplier=outcome.supplier, note=outcome.reason,
                retry_after=timedelta(seconds=outcome.retry_after or 1.0),
            )

        if outcome.kind == "ok":
            inserted = (
                await s.execute(
                    pg_insert(Issuance)
                    .values(
                        order_id=order_id,
                        order_item_id=item_id,
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
                    text("SELECT code FROM issuances WHERE order_item_id=:id"), {"id": item_id}
                )
            ).scalar_one_or_none()
            if own_code is None:
                # Вставка не прошла, своей выдачи нет: код принадлежит другой
                # позиции. Расхождение поднимет поколение request_id, и
                # следующая попытка попросит другой код.
                await supplier_client.record_discrepancy(
                    order_id=order_id, item_id=item_id, supplier=outcome.supplier,
                    request_id=outcome.request_id, kind="code_collision",
                    code=outcome.code, detail="code already issued to another item",
                )
                await _fail_item(
                    s, item_id, "delivery_failed", f"code collision: {outcome.code}",
                    order_id=order_id,
                )
                log.error(
                    "delivery.code_collision", item_id=item_id, order_id=order_id,
                    code=outcome.code, supplier=outcome.supplier,
                    request_id=outcome.request_id,
                )
                return DeliveryResult(
                    "delivery_failed", note="code already issued to another order"
                )

            await _finalize_item(
                s, item_id, outcome.supplier, order_id=order_id, code=own_code
            )
            await ledger.post_delivery(s, order_id, item_id, amount_minor, currency)

            # Остаток витрины уменьшаем по факту выдачи.
            await s.execute(
                text(
                    "UPDATE sku_stock SET available = GREATEST(available - 1, 0), "
                    "updated_at = now() WHERE sku = :sku"
                ),
                {"sku": sku},
            )

            code = own_code  # код, реально привязанный к этой позиции

            log.info(
                "delivery.completed", item_id=item_id, order_id=order_id, sku=sku, code=code,
                supplier=outcome.supplier, request_id=outcome.request_id,
                first_time=inserted is not None,
            )
            return DeliveryResult("delivered", code=code, supplier=outcome.supplier)

        if outcome.kind == "unavailable":
            # Никто такое не продаёт: ждать нечего, позицию закроет возврат.
            await _fail_item(
                s, item_id, "unavailable", f"not fulfillable: {outcome.reason}",
                order_id=order_id,
            )
            log.warning(
                "delivery.unavailable", item_id=item_id, order_id=order_id, sku=sku,
                reason=outcome.reason,
            )
            return DeliveryResult("unavailable", note=outcome.reason)

        if outcome.kind == "out_of_stock":
            await _fail_item(
                s, item_id, "out_of_stock", "out_of_stock at all suppliers", order_id=order_id
            )
            log.warning("delivery.out_of_stock", item_id=item_id, order_id=order_id, sku=sku)
            return DeliveryResult(
                "out_of_stock", note="no stock at A and B", retry_after=timedelta(seconds=10)
            )

        if outcome.kind == "unknown":
            # Исход не выяснен: не переключаемся на другого и не считаем
            # отказом. Повторим тем же request_id.
            await _fail_item(
                s, item_id, "delivering", f"unresolved supplier outcome: {outcome.reason}",
                order_id=order_id,
            )
            log.warning(
                "delivery.unresolved", item_id=item_id, order_id=order_id,
                supplier=outcome.supplier, request_id=outcome.request_id,
                reason=outcome.reason,
            )
            return DeliveryResult(
                "unresolved", supplier=outcome.supplier, note=outcome.reason,
                retry_after=timedelta(seconds=2),
            )

        await _fail_item(
            s, item_id, "delivery_failed", str(outcome.reason), order_id=order_id
        )
        log.error(
            "delivery.failed", item_id=item_id, order_id=order_id, sku=sku,
            reason=outcome.reason,
        )
        return DeliveryResult(
            "delivery_failed", note=outcome.reason, retry_after=timedelta(seconds=5)
        )


async def _finalize_item(
    session, item_id: str, supplier: str | None, *, order_id: str = "", code: str | None = None
) -> None:
    # Позиция выдана: расхождения по ней разобраны, код у клиента.
    await session.execute(_RESOLVE_DISCREPANCIES, {"iid": item_id, "res": "reissued"})
    changed = (
        await session.execute(
            text(
                "UPDATE order_items SET status='delivered', supplier=COALESCE(:sup, supplier), "
                "delivered_at=COALESCE(delivered_at, now()), last_error=NULL, updated_at=now() "
                "WHERE id=:id AND status <> 'delivered' RETURNING id"
            ),
            {"id": item_id, "sup": supplier},
        )
    ).scalar_one_or_none()
    if changed and order_id:
        await events.item_status(
            session, order_id, item_id, "delivered", supplier=supplier, code=code
        )


async def _fail_item(
    session, item_id: str, status: str, error: str, *, order_id: str = ""
) -> None:
    # Событие пишем, только если статус действительно сменился. Иначе выдача,
    # начатая до возврата, допишет в историю статус, которого в таблице нет.
    changed = (
        await session.execute(
            text(
                "UPDATE order_items SET status=:st, last_error=:e, updated_at=now() "
                "WHERE id=:id AND status NOT IN ('delivered','refunded') RETURNING id"
            ),
            {"id": item_id, "st": status, "e": error},
        )
    ).scalar_one_or_none()
    if changed and order_id:
        await events.item_status(session, order_id, item_id, status, reason=error)
