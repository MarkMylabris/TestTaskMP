"""Сверка: где деньги и товар разошлись.

Две основные проверки смотрят в разные стороны. "Оплачен, но не выдан" - это
потеря для клиента, самая болезненная. "Выдан, но не оплачен" - потеря для нас;
в норме там всегда пусто, потому что выдача возможна только из paid.

Плюс проверяется, что журнал проводок сходится, и отдельно собираются заказы с
незакрытыми попытками к поставщику: по ним исход у внешней системы неизвестен,
и трогать их вслепую нельзя.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PAID_NOT_DELIVERED = text(
    """
    SELECT o.id, o.sku, o.status, o.amount_minor, o.currency, o.paid_at,
           o.delivery_attempts, o.last_error
      FROM orders o
     WHERE o.paid_at IS NOT NULL
       AND o.status <> 'delivered'
       AND NOT EXISTS (SELECT 1 FROM issuances i WHERE i.order_id = o.id)
       AND o.paid_at < now() - make_interval(secs => :grace)
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
    SELECT id, sku, status, updated_at, delivery_attempts, last_error
      FROM orders
     WHERE status NOT IN ('delivered','payment_failed')
       AND paid_at IS NOT NULL
       AND updated_at < now() - make_interval(secs => :stuck)
     ORDER BY updated_at
     LIMIT :limit
    """
)

UNRESOLVED_ATTEMPTS = text(
    """
    SELECT order_id, supplier, request_id, attempt_no, state, started_at
      FROM supplier_attempts
     WHERE state IN ('in_flight','unknown')
       AND started_at < now() - make_interval(secs => :grace)
       AND NOT EXISTS (SELECT 1 FROM issuances i WHERE i.order_id = supplier_attempts.order_id)
     ORDER BY started_at
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
        "healthy": (
            not paid_not_delivered and not delivered_not_paid and balance["balanced"]
        ),
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
    """Полная история заказа: платежи, попытки к поставщикам, выдача, деньги."""
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
    attempts = (
        await session.execute(
            text(
                "SELECT supplier, request_id, attempt_no, state, http_status, reason, code, "
                "latency_ms, started_at, finished_at FROM supplier_attempts "
                "WHERE order_id=:id ORDER BY started_at"
            ),
            {"id": order_id},
        )
    ).mappings().all()
    issuance = (
        await session.execute(
            text("SELECT code, supplier, request_id, created_at FROM issuances WHERE order_id=:id"),
            {"id": order_id},
        )
    ).mappings().first()
    entries = (
        await session.execute(
            text(
                "SELECT kind, account, amount_minor, currency, created_at "
                "FROM ledger_entries WHERE order_id=:id ORDER BY id"
            ),
            {"id": order_id},
        )
    ).mappings().all()
    return {
        "order": dict(order) if order else None,
        "payment_events": [dict(r) for r in events],
        "supplier_attempts": [dict(r) for r in attempts],
        "issuance": dict(issuance) if issuance else None,
        "ledger_entries": [dict(r) for r in entries],
    }
