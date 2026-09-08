"""Всплеск заказов и лимит поставщика.

Проверяется не "мы старались", а счётчик самой заглушки: она считает запросы
в скользящем окне и фиксирует нарушения.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from tests.conftest import (
    control,
    core_db,
    create_order,
    reset_supplier_stats,
    restock,
    supplier_stats,
    wait_status,
)

pytestmark = pytest.mark.asyncio

BURST = 24
SKU = "STEAM-TOPUP-500"


def evt(order_id: str, amount: float) -> dict:
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "order_id": order_id,
        "status": "paid",
        "amount": amount,
        "currency": "RUB",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def place_paid_order(api, sku: str = SKU) -> dict:
    order = await create_order(api, sku)
    await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
    return order


# --------------------------------------------------------------------------- #
# Критерии 1 и 2: ничего не теряется, лимит не превышается
# --------------------------------------------------------------------------- #
async def test_burst_is_queued_and_never_exceeds_supplier_limit(api):
    for s in ("a", "b"):
        await restock(s, SKU, BURST + 10)
        await reset_supplier_stats(s)
        await control(s, {"rate_limit": 20, "rate_window_seconds": 1.0})

    orders = await asyncio.gather(*(place_paid_order(api) for _ in range(BURST)))

    finals = await asyncio.gather(
        *(wait_status(api, o["id"], {"delivered"}, timeout=120) for o in orders)
    )

    codes = [f["issuance"]["code"] for f in finals]
    assert len(set(codes)) == BURST, "ни один заказ не потерян и не задвоен"

    for s in ("a", "b"):
        stats = await supplier_stats(s)
        assert stats["over_limit"] == 0, f"поставщик {s}: лимит превышен"
        assert stats["max_in_window"] <= stats["rate_limit"], stats
        assert stats["rejected_429"] == 0, (
            f"поставщик {s} отвечал 429 - значит мы ломились сверх лимита"
        )

    for s in ("a", "b"):
        await control(s, {"rate_limit": 0})


# --------------------------------------------------------------------------- #
# Критерий 4: прогресс видно
# --------------------------------------------------------------------------- #
async def test_queue_progress_is_observable(api):
    progress = (await api.get("/admin/queue")).json()

    assert progress["paid_items"]["delivered"] >= BURST
    assert progress["paid_items"]["total"] >= progress["paid_items"]["delivered"]
    assert progress["queue"]["waiting"] >= 0

    budgets = {b["supplier"]: b for b in progress["suppliers"]}
    assert set(budgets) == {"a", "b"}
    assert budgets["a"]["taken_total"] > 0, "запросы к поставщику идут через лимитер"
    assert float(budgets["a"]["rate_per_min"]) > 0


async def test_progress_shows_queue_during_burst(api):
    """Во время всплеска видно, сколько заказов ещё ждёт своей выдачи."""
    for s in ("a", "b"):
        await restock(s, SKU, 12)
        await control(s, {"rate_limit": 2, "rate_window_seconds": 1.0})

    orders = await asyncio.gather(*(place_paid_order(api) for _ in range(8)))
    seen_queue = 0
    for _ in range(40):
        progress = (await api.get("/admin/queue")).json()
        seen_queue = max(seen_queue, progress["paid_items"]["in_progress"])
        if seen_queue:
            break
        await asyncio.sleep(0.1)
    assert seen_queue > 0, "очередь на выдачу должна быть видна"

    for s in ("a", "b"):
        await control(s, {"rate_limit": 0})
    await asyncio.gather(
        *(wait_status(api, o["id"], {"delivered"}, timeout=120) for o in orders)
    )


# --------------------------------------------------------------------------- #
# Критерий 3: оплаченное обслуживается первым
# --------------------------------------------------------------------------- #
async def test_paid_delivery_outranks_background_work(api):
    """Выдача оплаченного заказа стоит в очереди впереди фоновых задач."""
    conn = await core_db()
    try:
        rows = await conn.fetch(
            "SELECT DISTINCT kind, priority FROM jobs ORDER BY priority"
        )
        priorities = {r["kind"]: r["priority"] for r in rows}
        assert priorities["deliver_order"] == 0
        assert all(p > 0 for k, p in priorities.items() if k != "deliver_order")

        # Тот же порядок, что у разборщика очереди. Задачи ставим в будущее,
        # чтобы воркер их не забрал.
        await conn.execute(
            """
            INSERT INTO jobs (kind, dedupe_key, payload, state, priority, run_at,
                              attempts, max_attempts)
            VALUES ('sync_stock', 'prio-test-low', '{}', 'pending', 50,
                    now() + interval '1 hour', 0, 25),
                   ('deliver_order', 'prio-test-high', '{}', 'pending', 0,
                    now() + interval '1 hour', 0, 25)
            """
        )
        first = await conn.fetchval(
            """
            SELECT dedupe_key FROM jobs
             WHERE state = 'pending' AND run_at <= now() + interval '2 hours'
               AND dedupe_key LIKE 'prio-test-%'
             ORDER BY priority, run_at LIMIT 1
            """
        )
        assert first == "prio-test-high", "оплаченный заказ забирается первым"
    finally:
        await conn.execute("DELETE FROM jobs WHERE dedupe_key LIKE 'prio-test-%'")
        await conn.close()


async def test_unpaid_orders_do_not_consume_supplier_limit(api):
    """Неоплаченный заказ не отнимает у поставщика ни одного запроса.

    Самая сильная форма "оплаченные раньше неоплаченных": неоплаченные не
    обслуживаются вовсе.
    """
    unpaid = [await create_order(api, SKU) for _ in range(5)]
    await asyncio.sleep(1.0)

    conn = await core_db()
    try:
        attempts = await conn.fetchval(
            "SELECT count(*) FROM supplier_attempts WHERE order_id = ANY($1::text[])",
            [o["id"] for o in unpaid],
        )
    finally:
        await conn.close()
    assert attempts == 0, "к поставщику ушли запросы по неоплаченным заказам"

    for o in unpaid:
        assert (await api.get(f"/orders/{o['id']}")).json()["status"] == "created"


# --------------------------------------------------------------------------- #
# Лимит на стороне поставщика: 429 не ломает выдачу и не тратит бюджет попыток
# --------------------------------------------------------------------------- #
async def test_supplier_429_does_not_burn_delivery_budget(api):
    await restock("a", "SUB-YT-3M", 6)
    await control("a", {"rate_limit": 1, "rate_window_seconds": 2.0})
    await control("b", {"unsupported_skus": ["SUB-YT-3M"]})

    orders = await asyncio.gather(*(place_paid_order(api, "SUB-YT-3M") for _ in range(3)))
    finals = await asyncio.gather(
        *(wait_status(api, o["id"], {"delivered"}, timeout=120) for o in orders)
    )

    for f in finals:
        assert f["items"][0]["delivery_attempts"] <= 3, (
            "ожидание в очереди не должно засчитываться как неудачная попытка"
        )
        assert f["items"][0]["status"] == "delivered"

    money = (await api.get("/admin/money")).json()
    assert money["balanced"], money["equation"]

    await control("a", {"rate_limit": 0})
    await control("b", {"unsupported_skus": []})
