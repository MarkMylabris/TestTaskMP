"""Восстановление картины на любой прошлый момент.

Проверяется не то, что история пишется, а то, что по ней восстанавливается
именно бывшее состояние: до оплаты, между оплатой и выдачей, после частичного
сбоя. И то, что переписать историю нельзя физически.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from tests.conftest import (
    control,
    core_db,
    create_multi_order,
    create_order,
    timeline,
    wait_status,
)

pytestmark = pytest.mark.asyncio


def now() -> datetime:
    return datetime.now(timezone.utc)


def evt(order_id: str, amount: float) -> dict:
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "order_id": order_id,
        "status": "paid",
        "amount": amount,
        "currency": "RUB",
        "created_at": now().isoformat(),
    }


async def as_of(api, order_id: str, at: datetime) -> dict:
    r = await api.get(f"/admin/orders/{order_id}/as-of", params={"at": at.isoformat()})
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------- #
# Критерий 1: видно состояние заказа и денег на прошлый момент
# --------------------------------------------------------------------------- #
async def test_state_is_reconstructed_at_every_moment(api):
    order = await create_order(api, "KEY-CS2-PRIME")
    await asyncio.sleep(0.3)
    after_create = now()

    await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
    final = await wait_status(api, order["id"], {"delivered"})
    await asyncio.sleep(0.3)
    after_delivery = now()

    # --- до оплаты
    before = await as_of(api, order["id"], after_create)
    assert before["order"]["status"] == "created"
    assert before["money"]["paid_minor"] == 0
    assert [i["status"] for i in before["order"]["items"]] == ["pending"]
    assert "code" not in before["order"]["items"][0]

    # --- после выдачи
    after = await as_of(api, order["id"], after_delivery)
    assert after["order"]["status"] == "delivered"
    assert after["order"]["items"][0]["code"] == final["issuance"]["code"]
    assert after["money"]["paid_minor"] == order["amount_minor"]
    assert after["money"]["delivered_minor"] == order["amount_minor"]
    assert after["money"]["open_minor"] == 0
    assert after["money"]["balanced"]

    # --- срез "сейчас" совпадает с текущим состоянием
    live = (await api.get(f"/orders/{order['id']}")).json()
    assert (await as_of(api, order["id"], now()))["order"]["status"] == live["status"]

    # --- до создания заказа его не было
    r = await api.get(
        f"/admin/orders/{order['id']}/as-of",
        params={"at": (now() - timedelta(days=1)).isoformat()},
    )
    assert r.status_code == 404


async def test_money_between_payment_and_delivery_is_visible(api):
    """Момент, когда деньги уже наши, а товар ещё нет."""
    await control("a", {"out_of_stock_skus": ["GIFT-XBOX-1500"]})
    await control("b", {"out_of_stock_skus": ["GIFT-XBOX-1500"]})

    order = await create_order(api, "GIFT-XBOX-1500")
    await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
    await wait_status(api, order["id"], {"out_of_stock"}, timeout=30)
    await asyncio.sleep(0.3)
    mid = now()

    snapshot = await as_of(api, order["id"], mid)
    assert snapshot["order"]["status"] == "out_of_stock"
    assert snapshot["money"]["paid_minor"] == order["amount_minor"]
    assert snapshot["money"]["delivered_minor"] == 0
    assert snapshot["money"]["open_minor"] == order["amount_minor"], (
        "обязательство перед клиентом видно как незакрытое"
    )
    assert snapshot["money"]["balanced"]

    await control("a", {"out_of_stock_skus": []})
    await control("b", {"out_of_stock_skus": []})


async def test_partial_failure_is_reconstructable(api):
    for s in ("a", "b"):
        await control(s, {"unsupported_skus": ["KEY-EFT"]})

    order = await create_multi_order(api, [{"sku": "SUB-DISCORD-1M"}, {"sku": "KEY-EFT"}])
    await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
    await wait_status(api, order["id"], {"partially_delivered"}, timeout=60)
    await asyncio.sleep(0.3)

    snapshot = await as_of(api, order["id"], now())
    statuses = {i["sku"]: i["status"] for i in snapshot["order"]["items"] if "sku" in i}
    assert statuses == {"SUB-DISCORD-1M": "delivered", "KEY-EFT": "refunded"}
    assert snapshot["order"]["status"] == "partially_delivered"

    money = snapshot["money"]
    assert money["paid_minor"] == money["delivered_minor"] + money["refunded_minor"]
    assert money["open_minor"] == 0

    for s in ("a", "b"):
        await control(s, {"unsupported_skus": []})


async def test_refund_during_delivery_does_not_rewrite_history(api):
    """Выдача, начатая до возврата, не дописывает в историю чужой статус.

    Поставщики отвечают медленно, так что возврат успевает пройти, пока
    выдача висит в сети. Её фаза фиксации обязана увидеть, что позиция уже
    закрыта, и промолчать.
    """
    for s in ("a", "b"):
        await control(s, {"unsupported_skus": ["KEY-EFT"], "latency_ms": 1500})

    order = await create_order(api, "KEY-EFT")
    await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
    await asyncio.sleep(0.5)   # выдача уже ушла в сеть

    r = await api.post(f"/admin/orders/{order['id']}/refund")
    assert r.json()["refunded"] is True
    await asyncio.sleep(4.0)   # даём выдаче доиграть и попытаться зафиксироваться

    live = (await api.get(f"/orders/{order['id']}")).json()
    snapshot = await as_of(api, order["id"], now())
    assert live["items"][0]["status"] == "refunded"
    assert snapshot["order"]["items"][0]["status"] == live["items"][0]["status"], (
        "история разошлась с текущим состоянием"
    )

    conn = await core_db()
    try:
        last = await conn.fetchval(
            "SELECT type FROM order_events WHERE order_id = $1 AND item_id <> '' "
            " ORDER BY id DESC LIMIT 1",
            order["id"],
        )
    finally:
        await conn.close()
    assert last == "item_refunded", "после возврата по позиции событий быть не должно"

    for s in ("a", "b"):
        await control(s, {"unsupported_skus": [], "latency_ms": 0})


# --------------------------------------------------------------------------- #
# Критерий 2: история только дополняется
# --------------------------------------------------------------------------- #
async def test_history_cannot_be_rewritten(api):
    order = await create_order(api, "SUB-SPOTIFY-1M")
    await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
    await wait_status(api, order["id"], {"delivered"})

    conn = await core_db()
    try:
        for table in ("order_events", "ledger_entries"):
            with pytest.raises(asyncpg.exceptions.RestrictViolationError):
                await conn.execute(f"UPDATE {table} SET order_id = 'hacked'")
            with pytest.raises(asyncpg.exceptions.RestrictViolationError):
                await conn.execute(f"DELETE FROM {table}")

        # Записи на месте, срез не поехал.
        n = await conn.fetchval(
            "SELECT count(*) FROM order_events WHERE order_id = $1", order["id"]
        )
        assert n >= 3
    finally:
        await conn.close()

    snapshot = await as_of(api, order["id"], now())
    assert snapshot["order"]["status"] == "delivered"


async def test_timeline_and_history_agree(api):
    order = await create_order(api, "GIFT-PSN-1000")
    await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
    final = await wait_status(api, order["id"], {"delivered"})
    await asyncio.sleep(0.3)

    tl = await timeline(api, order["id"])
    snapshot = await as_of(api, order["id"], now())
    assert snapshot["order"]["items"][0]["code"] == tl["issuances"][0]["code"]
    assert snapshot["order"]["status"] == final["status"]


# --------------------------------------------------------------------------- #
# Критерий 3: итоги за период считаются из истории и сходятся
# --------------------------------------------------------------------------- #
async def test_period_totals_add_up(api):
    frm = now()
    await asyncio.sleep(0.2)

    orders = [await create_order(api, "STEAM-TOPUP-500") for _ in range(3)]
    for o in orders:
        await api.post("/webhook/payment", json=evt(o["id"], o["amount"]))
    for o in orders:
        await wait_status(api, o["id"], {"delivered"}, timeout=60)
    await asyncio.sleep(0.5)
    to = now()

    r = await api.get(
        "/admin/reports/period", params={"from": frm.isoformat(), "to": to.isoformat()}
    )
    r.raise_for_status()
    report = r.json()

    total = sum(o["amount_minor"] for o in orders)
    assert report["money"]["paid_minor"] >= total
    assert report["money"]["delivered_minor"] >= total
    assert report["money"]["balanced"], report["money"]["equation"]

    kinds = {e["type"]: e["n"] for e in report["events"]}
    assert kinds["order_created"] >= 3 and kinds["order_paid"] >= 3

    # Итог за период равен разности двух срезов: одно число, посчитанное по
    # окну проводок и по двум точкам во времени.
    before = (await api.get("/admin/money/as-of", params={"at": frm.isoformat()})).json()
    after = (await api.get("/admin/money/as-of", params={"at": to.isoformat()})).json()
    assert (
        after["paid_minor"] - before["paid_minor"] == report["money"]["paid_minor"]
    )
    assert (
        after["delivered_minor"] - before["delivered_minor"]
        == report["money"]["delivered_minor"]
    )


async def test_period_bounds_are_validated(api):
    t = now()
    r = await api.get(
        "/admin/reports/period",
        params={"from": t.isoformat(), "to": (t - timedelta(hours=1)).isoformat()},
    )
    assert r.status_code == 422
