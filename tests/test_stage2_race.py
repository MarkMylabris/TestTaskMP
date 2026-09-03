"""Этап 2: exactly-once под гонками. Критерии приёмки 1, 2, 3."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from tests.conftest import create_order, restock, timeline, wait_status

CONCURRENCY = 50


def evt(order_id: str, amount: float, event_id: str | None = None,
        status: str = "paid", created_at: datetime | None = None) -> dict:
    ts = created_at or datetime.now(timezone.utc)
    return {
        "event_id": event_id or f"evt_{uuid.uuid4().hex[:12]}",
        "order_id": order_id,
        "status": status,
        "amount": amount,
        "currency": "RUB",
        "created_at": ts.isoformat().replace("+00:00", "Z"),
    }


async def fire_together(api, payloads: list[dict]) -> list:
    """Отправить все вебхуки одновременно: барьер держит корутины до старта."""
    gate = asyncio.Event()

    async def one(p):
        await gate.wait()
        return await api.post("/webhook/payment", json=p)

    tasks = [asyncio.create_task(one(p)) for p in payloads]
    await asyncio.sleep(0.1)
    gate.set()
    return await asyncio.gather(*tasks)


# --------------------------------------------------------------------------- #
# Критерий 1: 50 параллельных вебхуков -> ровно один факт выдачи
# --------------------------------------------------------------------------- #
async def test_50_parallel_webhooks_issue_exactly_once(api):
    order = await create_order(api, "KEY-CS2-PRIME")
    payloads = [evt(order["id"], order["amount"]) for _ in range(CONCURRENCY)]

    responses = await fire_together(api, payloads)

    assert all(r.status_code == 200 for r in responses), "все вебхуки должны быть приняты"
    results = [r.json()["result"] for r in responses]
    assert results.count("applied") == 1, f"ожидался ровно один applied, получено {results.count('applied')}"
    assert results.count("ignored") == CONCURRENCY - 1

    final = await wait_status(api, order["id"], {"delivered"})
    tl = await timeline(api, order["id"])

    assert final["issuance"] is not None
    assert len(tl["payment_events"]) == CONCURRENCY, "ни одно событие не потеряно"
    assert sum(1 for e in tl["payment_events"] if e["processing_state"] == "applied") == 1
    # Проводка по оплате сделана ровно один раз (2 строки двойной записи).
    payment_legs = [e for e in tl["ledger_entries"] if e["kind"] == "payment_captured"]
    assert len(payment_legs) == 2
    assert sum(e["amount_minor"] for e in tl["ledger_entries"]) == 0


# --------------------------------------------------------------------------- #
# Критерий 2: повтор с тем же event_id ничего не меняет
# --------------------------------------------------------------------------- #
async def test_same_event_id_is_processed_once(api):
    order = await create_order(api, "KEY-GTA5")
    event_id = f"evt_{uuid.uuid4().hex[:12]}"
    payloads = [evt(order["id"], order["amount"], event_id=event_id) for _ in range(CONCURRENCY)]

    responses = await fire_together(api, payloads)
    results = [r.json()["result"] for r in responses]

    assert all(r.status_code == 200 for r in responses)
    assert results.count("applied") == 1
    assert results.count("duplicate") == CONCURRENCY - 1

    final = await wait_status(api, order["id"], {"delivered"})
    tl = await timeline(api, order["id"])
    assert len(tl["payment_events"]) == 1, "дубликаты не должны попадать в журнал"
    assert final["issuance"] is not None


async def test_replay_after_delivery_changes_nothing(api):
    order = await create_order(api, "SUB-SPOTIFY-1M")
    payload = evt(order["id"], order["amount"])
    assert (await api.post("/webhook/payment", json=payload)).json()["result"] == "applied"
    delivered = await wait_status(api, order["id"], {"delivered"})

    # Тот же event_id ещё раз, уже после выдачи.
    r = await api.post("/webhook/payment", json=payload)
    assert r.status_code == 200 and r.json()["result"] == "duplicate"
    # И новый event_id по тому же заказу.
    r = await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
    assert r.json()["result"] == "ignored"

    await asyncio.sleep(0.5)
    after = (await api.get(f"/orders/{order['id']}")).json()
    assert after["status"] == "delivered"
    assert after["issuance"]["code"] == delivered["issuance"]["code"]
    assert after["delivered_at"] == delivered["delivered_at"]


# --------------------------------------------------------------------------- #
# Критерий 3: вебхуки вне порядка и раньше заказа
# --------------------------------------------------------------------------- #
async def test_webhook_before_order_is_accepted_and_applied_later(api):
    order_id = f"ord_early_{uuid.uuid4().hex[:8]}"

    r = await api.post("/webhook/payment", json=evt(order_id, 399.0))
    assert r.status_code == 200, "нельзя отвечать 5xx: платёжка зациклится на ретраях"
    assert r.json()["result"] == "orphan"

    # Заказ появляется позже - "сиротское" событие должно доехать само.
    await create_order(api, "SUB-DISCORD-1M", order_id=order_id)
    final = await wait_status(api, order_id, {"delivered"}, timeout=30)
    assert final["issuance"] is not None

    tl = await timeline(api, order_id)
    assert [e["processing_state"] for e in tl["payment_events"]] == ["applied"]


async def test_failed_after_paid_does_not_revert(api):
    order = await create_order(api, "GIFT-PSN-1000")
    await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
    await wait_status(api, order["id"], {"delivered"})

    # Вебхук "отказ" пришёл позже успешной оплаты - порядок нарушен.
    r = await api.post(
        "/webhook/payment", json=evt(order["id"], order["amount"], status="failed")
    )
    assert r.status_code == 200 and r.json()["result"] == "ignored"

    after = (await api.get(f"/orders/{order['id']}")).json()
    assert after["status"] == "delivered"


async def test_out_of_order_by_timestamp(api):
    """Событие со "старой" меткой времени не откатывает более новое состояние."""
    order = await create_order(api, "GIFT-XBOX-1500")
    now = datetime.now(timezone.utc)

    paid = evt(order["id"], order["amount"], created_at=now)
    stale_failed = evt(
        order["id"], order["amount"], status="failed", created_at=now - timedelta(minutes=5)
    )
    responses = await fire_together(api, [paid, stale_failed])
    assert all(r.status_code == 200 for r in responses)

    final = await wait_status(api, order["id"], {"delivered", "payment_failed"})
    tl = await timeline(api, order["id"])
    applied = [e for e in tl["payment_events"] if e["processing_state"] == "applied"]
    assert len(applied) == 1, "применено ровно одно из двух конфликтующих событий"
    if final["status"] == "delivered":
        assert applied[0]["status"] == "paid"
        assert final["issuance"] is not None
    else:
        assert applied[0]["status"] == "failed"
        assert final["issuance"] is None


async def test_parallel_races_across_many_orders(api):
    """20 заказов, по 10 параллельных вебхуков на каждый - ни одной лишней выдачи."""
    await restock("a", "STEAM-TOPUP-1000", 30)   # пула из задания на 20 заказов не хватит
    orders = [await create_order(api, "STEAM-TOPUP-1000") for _ in range(20)]
    payloads = [evt(o["id"], o["amount"]) for o in orders for _ in range(10)]

    responses = await fire_together(api, payloads)
    assert all(r.status_code == 200 for r in responses)

    codes = []
    for o in orders:
        final = await wait_status(api, o["id"], {"delivered"}, timeout=60)
        codes.append(final["issuance"]["code"])

    assert len(set(codes)) == len(codes), "один ключ не может уйти в два заказа"

    stats = (await api.get("/admin/stats")).json()
    balance = (await api.get("/admin/ledger/balance")).json()
    assert balance["balanced"], stats
