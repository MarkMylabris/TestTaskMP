"""Сверка: где деньги и товар разошлись.

Две основные проверки смотрят в разные стороны: "оплачен, но не выдан" - это
потеря для клиента, "выдан, но не оплачен" - для нас (в норме пусто, выдача
возможна только из paid). Отдельно собираются заказы с незакрытыми попытками
к поставщику: их исход неизвестен, и трогать их вслепую нельзя.

Деньги проверяются остатком по счёту `customer`: у завершённого заказа он
обязан быть нулём, у незавершённого равен сумме, за которую мы ещё не
отчитались ни кодом, ни возвратом.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PAID_NOT_DELIVERED = text(
    """
    SELECT o.id, o.status, o.amount_minor, o.currency, o.paid_at,
           o.delivery_attempts, o.last_error,
           count(*) FILTER (WHERE it.status <> 'delivered') AS items_pending,
           count(*) AS items_total
      FROM orders o
      JOIN order_items it ON it.order_id = o.id
     WHERE o.paid_at IS NOT NULL
       AND o.status NOT IN ('delivered', 'partially_delivered', 'refunded')
       AND o.paid_at < now() - make_interval(secs => :grace)
     GROUP BY o.id
    HAVING count(*) FILTER (WHERE it.status NOT IN ('delivered', 'refunded')) > 0
     ORDER BY o.paid_at
     LIMIT :limit
    """
)

DELIVERED_NOT_PAID = text(
    """
    SELECT i.order_id, i.code, i.supplier, i.created_at, o.status, o.paid_at
      FROM issuances i
      JOIN orders o ON o.id = i.order_id
     WHERE o.paid_at IS NULL
     ORDER BY i.created_at
     LIMIT :limit
    """
)

STUCK_ORDERS = text(
    """
    SELECT id, status, updated_at, delivery_attempts, last_error
      FROM orders
     WHERE status NOT IN ('delivered','partially_delivered','refunded','payment_failed')
       AND paid_at IS NOT NULL
       AND updated_at < now() - make_interval(secs => :stuck)
     ORDER BY updated_at
     LIMIT :limit
    """
)

UNRESOLVED_ATTEMPTS = text(
    """
    SELECT order_id, order_item_id, supplier, request_id, attempt_no, state, started_at
      FROM supplier_attempts
     WHERE state IN ('in_flight','unknown')
       AND started_at < now() - make_interval(secs => :grace)
       AND NOT EXISTS (
             SELECT 1 FROM issuances i
              WHERE i.order_item_id = supplier_attempts.order_item_id)
     ORDER BY started_at
     LIMIT :limit
    """
)

# Заказ завершён, а обязательство не закрыто (или закрыто дважды): это и есть
# нарушение "оплачено = выдано + возвращено".
MONEY_MISMATCH = text(
    """
    SELECT o.id, o.status, o.amount_minor,
           COALESCE(SUM(l.amount_minor), 0) AS customer_balance_minor
      FROM orders o
      JOIN ledger_entries l ON l.order_id = o.id AND l.account = 'customer'
     WHERE o.status IN ('delivered', 'partially_delivered', 'refunded')
     GROUP BY o.id
    HAVING COALESCE(SUM(l.amount_minor), 0) <> 0
     ORDER BY o.id
     LIMIT :limit
    """
)

MONEY_TOTALS = text(
    """
    SELECT
        COALESCE(SUM(amount_minor) FILTER (
            WHERE kind = 'payment_captured' AND account = 'customer'), 0) AS paid_minor,
        COALESCE(SUM(amount_minor) FILTER (WHERE account = 'delivered'), 0) AS delivered_minor,
        COALESCE(SUM(amount_minor) FILTER (WHERE account = 'refund'), 0)    AS refunded_minor,
        COALESCE(SUM(amount_minor) FILTER (WHERE account = 'customer'), 0)  AS open_minor
      FROM ledger_entries
    """
)

# Расхождения с поставщиком и их разбор.
DISCREPANCIES = text(
    """
    SELECT kind,
           count(*) AS total,
           count(*) FILTER (WHERE resolved_at IS NULL) AS open
      FROM supplier_discrepancies
     GROUP BY kind
     ORDER BY kind
    """
)

OPEN_DISCREPANCIES = text(
    """
    SELECT d.order_id, d.order_item_id, d.supplier, d.request_id, d.kind, d.code,
           d.detail, d.detected_at, it.status AS item_status
      FROM supplier_discrepancies d
      JOIN order_items it ON it.id = d.order_item_id
     WHERE d.resolved_at IS NULL
     ORDER BY d.detected_at
     LIMIT :limit
    """
)

ORPHAN_EVENTS = text(
    """
    SELECT event_id, order_id, status, received_at, note
      FROM payment_events
     WHERE processing_state = 'orphan'
     ORDER BY received_at
     LIMIT :limit
    """
)


async def report(session: AsyncSession, *, grace_seconds: int = 30, limit: int = 100) -> dict:
    paid_not_delivered = (
        await session.execute(PAID_NOT_DELIVERED, {"grace": grace_seconds, "limit": limit})
    ).mappings().all()
    delivered_not_paid = (
        await session.execute(DELIVERED_NOT_PAID, {"limit": limit})
    ).mappings().all()
    stuck = (
        await session.execute(STUCK_ORDERS, {"stuck": grace_seconds, "limit": limit})
    ).mappings().all()
    unresolved = (
        await session.execute(UNRESOLVED_ATTEMPTS, {"grace": grace_seconds, "limit": limit})
    ).mappings().all()
    orphans = (await session.execute(ORPHAN_EVENTS, {"limit": limit})).mappings().all()
    balance = await ledger_balance(session)
    money = await money_report(session, limit=limit)
    discrepancies = await discrepancy_report(session, limit=limit)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "grace_seconds": grace_seconds,
        "paid_not_delivered": {
            "count": len(paid_not_delivered),
            "items": [dict(r) for r in paid_not_delivered],
        },
        "delivered_not_paid": {
            "count": len(delivered_not_paid),
            "items": [dict(r) for r in delivered_not_paid],
        },
        "stuck_orders": {"count": len(stuck), "items": [dict(r) for r in stuck]},
        "unresolved_supplier_attempts": {
            "count": len(unresolved), "items": [dict(r) for r in unresolved]
        },
        "orphan_payment_events": {"count": len(orphans), "items": [dict(r) for r in orphans]},
        "ledger": balance,
        "money": money,
        "supplier_discrepancies": discrepancies,
        "healthy": (
            not paid_not_delivered and not delivered_not_paid
            and balance["balanced"] and money["balanced"]
            and not discrepancies["needs_attention"]["count"]
        ),
    }


async def discrepancy_report(session: AsyncSession, *, limit: int = 100) -> dict:
    """Что поставщик прислал не то и осталось ли неразобранное.

    Расхождение открыто, только пока позиция не закрыта кодом или деньгами.
    """
    by_kind = (await session.execute(DISCREPANCIES)).mappings().all()
    open_rows = (await session.execute(OPEN_DISCREPANCIES, {"limit": limit})).mappings().all()
    # Позиция в работе: расхождение по ней не висит, а разбирается.
    stuck = [r for r in open_rows if r["item_status"] in ("delivered", "refunded")]
    return {
        "by_kind": [dict(r) for r in by_kind],
        "open": {"count": len(open_rows), "items": [dict(r) for r in open_rows]},
        "needs_attention": {"count": len(stuck), "items": [dict(r) for r in stuck]},
    }


async def money_report(session: AsyncSession, *, limit: int = 100) -> dict:
    """Деньги: оплачено = выдано + возвращено + ещё не закрыто."""
    totals = (await session.execute(MONEY_TOTALS)).mappings().first()
    mismatches = (await session.execute(MONEY_MISMATCH, {"limit": limit})).mappings().all()

    paid = int(totals["paid_minor"])
    delivered = int(totals["delivered_minor"])
    refunded = int(totals["refunded_minor"])
    # Остаток по `customer` - то, за что мы ещё не отчитались.
    open_minor = int(totals["open_minor"])

    return {
        "paid_minor": paid,
        "delivered_minor": delivered,
        "refunded_minor": refunded,
        "open_minor": open_minor,
        "equation": f"{paid} = {delivered} + {refunded} + {open_minor}",
        "balanced": paid == delivered + refunded + open_minor and not mismatches,
        "unsettled_finished_orders": {
            "count": len(mismatches),
            "items": [dict(r) for r in mismatches],
        },
    }


async def ledger_balance(session: AsyncSession) -> dict:
    total = (
        await session.execute(text("SELECT COALESCE(SUM(amount_minor),0) FROM ledger_entries"))
    ).scalar_one()
    unbalanced = (
        await session.execute(
            text(
                """
                SELECT txn_id, SUM(amount_minor) AS delta
                  FROM ledger_entries
                 GROUP BY txn_id
                HAVING SUM(amount_minor) <> 0
                 LIMIT 20
                """
            )
        )
    ).mappings().all()
    by_account = (
        await session.execute(
            text(
                "SELECT account, SUM(amount_minor) AS total FROM ledger_entries "
                "GROUP BY account ORDER BY account"
            )
        )
    ).mappings().all()
    return {
        "total_minor": int(total),
        "balanced": int(total) == 0 and not unbalanced,
        "unbalanced_transactions": [dict(r) for r in unbalanced],
        "by_account": [dict(r) for r in by_account],
    }


async def order_timeline(session: AsyncSession, order_id: str) -> dict:
    """История заказа: платежи, попытки, выдачи, расхождения, деньги."""
    order = (
        await session.execute(text("SELECT * FROM orders WHERE id=:id"), {"id": order_id})
    ).mappings().first()
    events = (
        await session.execute(
            text(
                "SELECT event_id, status, processing_state, note, event_created_at, received_at "
                "FROM payment_events WHERE order_id=:id ORDER BY received_at"
            ),
            {"id": order_id},
        )
    ).mappings().all()
    items = (
        await session.execute(
            text(
                "SELECT id, position, sku, amount_minor, currency, status, supplier, "
                "delivery_attempts, last_error, delivered_at, refunded_at "
                "FROM order_items WHERE order_id=:id ORDER BY position"
            ),
            {"id": order_id},
        )
    ).mappings().all()
    attempts = (
        await session.execute(
            text(
                "SELECT order_item_id, supplier, request_id, attempt_no, state, http_status, "
                "reason, code, latency_ms, started_at, finished_at FROM supplier_attempts "
                "WHERE order_id=:id ORDER BY started_at"
            ),
            {"id": order_id},
        )
    ).mappings().all()
    discrepancies = (
        await session.execute(
            text(
                "SELECT order_item_id, supplier, request_id, kind, code, detail, "
                "detected_at, resolved_at, resolution FROM supplier_discrepancies "
                "WHERE order_id=:id ORDER BY detected_at"
            ),
            {"id": order_id},
        )
    ).mappings().all()
    issuances = (
        await session.execute(
            text(
                "SELECT order_item_id, sku, code, supplier, request_id, created_at "
                "FROM issuances WHERE order_id=:id ORDER BY created_at"
            ),
            {"id": order_id},
        )
    ).mappings().all()
    entries = (
        await session.execute(
            text(
                "SELECT kind, item_id, account, amount_minor, currency, created_at "
                "FROM ledger_entries WHERE order_id=:id ORDER BY id"
            ),
            {"id": order_id},
        )
    ).mappings().all()
    return {
        "order": dict(order) if order else None,
        "items": [dict(r) for r in items],
        "payment_events": [dict(r) for r in events],
        "supplier_attempts": [dict(r) for r in attempts],
        "supplier_discrepancies": [dict(r) for r in discrepancies],
        "issuances": [dict(r) for r in issuances],
        # Совместимость с однотоварным ответом.
        "issuance": dict(issuances[0]) if len(issuances) == 1 else None,
        "ledger_entries": [dict(r) for r in entries],
    }
