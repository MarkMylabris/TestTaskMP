"""Этап 4: сверка, наблюдаемость, восстановление, денежный журнал."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from tests.conftest import control, create_order, restock, timeline, wait_status


def evt(order_id: str, amount: float, status: str = "paid") -> dict:
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "order_id": order_id,
        "status": status,
        "amount": amount,
        "currency": "RUB",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


async def pay(api, order: dict) -> None:
    assert (await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))).status_code == 200


async def test_ledger_always_balances(api):
    order = await create_order(api, "STEAM-TOPUP-2500")
    await pay(api, order)
    await wait_status(api, order["id"], {"delivered"})

    balance = (await api.get("/admin/ledger/balance")).json()
    assert balance["balanced"]
    assert balance["total_minor"] == 0
    assert balance["unbalanced_transactions"] == []

    accounts = {r["account"]: r["total"] for r in balance["by_account"]}
    assert accounts["customer"] > 0 and accounts["revenue"] < 0
    assert accounts["supplier_cost"] > 0 and accounts["inventory"] < 0


async def test_ledger_is_not_double_posted_under_webhook_storm(api):
    order = await create_order(api, "STEAM-TOPUP-500")
    payloads = [evt(order["id"], order["amount"]) for _ in range(30)]
    await asyncio.gather(*(api.post("/webhook/payment", json=p) for p in payloads))
    await wait_status(api, order["id"], {"delivered"})

    tl = await timeline(api, order["id"])
    kinds = [e["kind"] for e in tl["ledger_entries"]]
    assert kinds.count("payment_captured") == 2   # дебет + кредит, ровно одна проводка
    assert kinds.count("delivery_cost") == 2
    assert sum(e["amount_minor"] for e in tl["ledger_entries"]) == 0


async def test_reconciliation_reports_paid_not_delivered(api):
    await control("a", {"out_of_stock_skus": ["KEY-CS2-PRIME"]})
    await control("b", {"out_of_stock_skus": ["KEY-CS2-PRIME"]})

    order = await create_order(api, "KEY-CS2-PRIME")
    await pay(api, order)
    await wait_status(api, order["id"], {"out_of_stock"}, timeout=30)

    rec = (await api.get("/admin/reconciliation", params={"grace_seconds": 0})).json()
    ids = [i["id"] for i in rec["paid_not_delivered"]["items"]]
    assert order["id"] in ids
    assert rec["healthy"] is False
    assert rec["delivered_not_paid"]["count"] == 0, "выдач без оплаты быть не должно"
    assert rec["ledger"]["balanced"]


async def test_sweeper_finishes_stuck_orders(api):
    """Поставщики лежат, задача выдачи исчерпывается - "доводчик" добивает заказ."""
    await control("a", {"mode": "error_5xx"})
    await control("b", {"mode": "error_5xx"})

    order = await create_order(api, "SUB-SPOTIFY-1M")
    await pay(api, order)
    stalled = await wait_status(api, order["id"], {"delivery_failed"}, timeout=30)
    assert stalled["issuance"] is None

    await restock("a", "SUB-SPOTIFY-1M", 3)
    await control("a", {"mode": "ok"})
    await control("b", {"mode": "ok"})

    # Ничего не дёргаем руками: фоновая задача обязана довести заказ сама.
    final = await wait_status(api, order["id"], {"delivered"}, timeout=90)
    assert final["issuance"] is not None
    assert final["delivery_attempts"] >= 2


async def test_manual_redeliver_is_idempotent(api):
    order = await create_order(api, "GIFT-PSN-1000")
    await pay(api, order)
    final = await wait_status(api, order["id"], {"delivered"})

    r = await api.post(f"/admin/orders/{order['id']}/redeliver")
    assert r.status_code == 200 and r.json()["enqueued"] is False

    await asyncio.sleep(1)
    after = (await api.get(f"/orders/{order['id']}")).json()
    assert after["issuance"]["code"] == final["issuance"]["code"]


async def test_refund_keeps_ledger_balanced(api):
    await control("a", {"out_of_stock_skus": ["KEY-GTA5"]})
    await control("b", {"out_of_stock_skus": ["KEY-GTA5"]})

    order = await create_order(api, "KEY-GTA5")
    await pay(api, order)
    await wait_status(api, order["id"], {"out_of_stock"}, timeout=30)

    r = await api.post(f"/admin/orders/{order['id']}/refund")
    assert r.status_code == 200 and r.json()["refunded"] is True
    # Повторный возврат не задваивается.
    assert (await api.post(f"/admin/orders/{order['id']}/refund")).json()["refunded"] is False

    balance = (await api.get("/admin/ledger/balance")).json()
    assert balance["balanced"]

    tl = await timeline(api, order["id"])
    per_account = {}
    for e in tl["ledger_entries"]:
        per_account[e["account"]] = per_account.get(e["account"], 0) + e["amount_minor"]
    assert per_account["customer"] == 0, "после возврата обязательство перед клиентом закрыто"


async def test_admin_stats_and_jobs(api):
    stats = (await api.get("/admin/stats")).json()
    assert stats["issuances"] >= 1
    assert any(r["status"] == "delivered" for r in stats["orders_by_status"])

    jobs = (await api.get("/admin/jobs", params={"limit": 20})).json()
    assert "items" in jobs
