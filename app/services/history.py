"""Состояние на прошлый момент: свёртка истории, а не текущие строки.

`orders` и `order_items` отвечают на "как сейчас", их переписывает каждый шаг
выдачи. На "как было в 14:32" отвечают append-only журналы - события заказа и
проводки, оба защищены триггером от UPDATE и DELETE.

Деньги считаются так же: сумма проводок до момента среза. Инвариант
"оплачено = выдано + возвращено + ещё не закрыто" держится на любом срезе.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

EVENTS_UNTIL = text(
    """
    SELECT id, type, item_id, payload, occurred_at
      FROM order_events
     WHERE order_id = :oid AND occurred_at <= :ts
     ORDER BY id
    """
)

MONEY_UNTIL = text(
    """
    SELECT
        COALESCE(SUM(amount_minor) FILTER (
            WHERE kind = 'payment_captured' AND account = 'customer'), 0) AS paid_minor,
        COALESCE(SUM(amount_minor) FILTER (WHERE account = 'delivered'), 0) AS delivered_minor,
        COALESCE(SUM(amount_minor) FILTER (WHERE account = 'refund'), 0)    AS refunded_minor,
        COALESCE(SUM(amount_minor) FILTER (WHERE account = 'customer'), 0)  AS open_minor
      FROM ledger_entries
     WHERE created_at <= :ts AND (:oid = '' OR order_id = :oid)
    """
)

PERIOD_EVENTS = text(
    """
    SELECT type, count(*) AS n
      FROM order_events
     WHERE occurred_at > :frm AND occurred_at <= :to
     GROUP BY type
     ORDER BY type
    """
)

PERIOD_MONEY = text(
    """
    SELECT
        COALESCE(SUM(amount_minor) FILTER (
            WHERE kind = 'payment_captured' AND account = 'customer'), 0) AS paid_minor,
        COALESCE(SUM(amount_minor) FILTER (WHERE account = 'delivered'), 0) AS delivered_minor,
        COALESCE(SUM(amount_minor) FILTER (WHERE account = 'refund'), 0)    AS refunded_minor,
        COALESCE(SUM(amount_minor) FILTER (WHERE account = 'supplier_cost'), 0) AS cost_minor,
        COALESCE(SUM(amount_minor) FILTER (WHERE account = 'customer'), 0)  AS open_delta_minor,
        count(DISTINCT order_id) AS orders_touched
      FROM ledger_entries
     WHERE created_at > :frm AND created_at <= :to
    """
)

# Статусы, при которых по позициям уже ничего не происходит.
_FINAL = ("delivered", "partially_delivered", "refunded", "payment_failed")


async def order_as_of(session: AsyncSession, order_id: str, ts: datetime) -> dict | None:
    """Каким были заказ и деньги по нему на момент ts."""
    events = (
        await session.execute(EVENTS_UNTIL, {"oid": order_id, "ts": ts})
    ).mappings().all()
    if not events:
        return None

    order = {"id": order_id, "status": "created", "amount_minor": 0, "currency": None}
    items: dict[str, dict] = {}

    for ev in events:
        payload = ev["payload"] or {}
        if ev["type"] == "order_created":
            order["amount_minor"] = payload.get("amount_minor", 0)
            order["currency"] = payload.get("currency")
            for it in payload.get("items", []):
                items[it["id"]] = {
                    "id": it["id"],
                    "sku": it["sku"],
                    "amount_minor": it["amount_minor"],
                    "status": "pending",
                }
        elif ev["type"] == "order_paid":
            order["status"] = "paid"
        elif ev["type"] == "order_payment_failed":
            order["status"] = "payment_failed"
        elif ev["type"] == "order_status":
            order["status"] = payload.get("status", order["status"])
        elif ev["type"] in ("item_status", "item_refunded"):
            item = items.setdefault(ev["item_id"], {"id": ev["item_id"], "status": "pending"})
            item["status"] = (
                "refunded" if ev["type"] == "item_refunded" else payload.get("status")
            )
            if payload.get("code"):
                item["code"] = payload["code"]
            if payload.get("supplier"):
                item["supplier"] = payload["supplier"]

    money = (
        await session.execute(MONEY_UNTIL, {"oid": order_id, "ts": ts})
    ).mappings().first()

    return {
        "as_of": ts.isoformat(),
        "order": order | {"items": list(items.values())},
        "money": _money(money),
        "events_applied": len(events),
        "final": order["status"] in _FINAL,
    }


async def money_as_of(session: AsyncSession, ts: datetime) -> dict:
    """Деньги по всей базе на момент ts."""
    row = (await session.execute(MONEY_UNTIL, {"oid": "", "ts": ts})).mappings().first()
    return {"as_of": ts.isoformat()} | _money(row)


async def period_report(session: AsyncSession, frm: datetime, to: datetime) -> dict:
    """Итоги за период из той же истории, что и срезы."""
    money = (await session.execute(PERIOD_MONEY, {"frm": frm, "to": to})).mappings().first()
    events = (await session.execute(PERIOD_EVENTS, {"frm": frm, "to": to})).mappings().all()

    paid = int(money["paid_minor"])
    delivered = int(money["delivered_minor"])
    refunded = int(money["refunded_minor"])
    open_delta = int(money["open_delta_minor"])

    return {
        "from": frm.isoformat(),
        "to": to.isoformat(),
        "money": {
            "paid_minor": paid,
            "delivered_minor": delivered,
            "refunded_minor": refunded,
            "cost_minor": int(money["cost_minor"]),
            # Прирост незакрытых обязательств: оплачено в окне, а выдано или
            # возвращено уже за его границей.
            "open_delta_minor": open_delta,
            "equation": f"{paid} = {delivered} + {refunded} + {open_delta}",
            "balanced": paid == delivered + refunded + open_delta,
        },
        "orders_touched": int(money["orders_touched"]),
        "events": [dict(r) for r in events],
    }


def _money(row) -> dict:
    paid = int(row["paid_minor"])
    delivered = int(row["delivered_minor"])
    refunded = int(row["refunded_minor"])
    open_minor = int(row["open_minor"])
    return {
        "paid_minor": paid,
        "delivered_minor": delivered,
        "refunded_minor": refunded,
        "open_minor": open_minor,
        "equation": f"{paid} = {delivered} + {refunded} + {open_minor}",
        "balanced": paid == delivered + refunded + open_minor,
    }
