"""Этап 1: ядро API - заказ, вебхук, автовыдача, переходы статусов."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from tests.conftest import create_order, timeline, wait_status


def evt(order_id: str, amount: float, currency: str = "RUB", status: str = "paid") -> dict:
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "order_id": order_id,
        "status": status,
        "amount": amount,
        "currency": currency,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


async def test_catalog_is_seeded(api):
    r = await api.get("/catalog/storefront", params={"limit": 100})
    assert r.status_code == 200
    skus = {i["sku"] for i in r.json()["items"]}
    assert "STEAM-TOPUP-500" in skus and "KEY-CS2-PRIME" in skus


async def test_create_order_unknown_sku(api):
    r = await api.post("/orders", json={"sku": "NOPE-000"})
    assert r.status_code == 404


async def test_happy_path_created_paid_delivered(api):
    order = await create_order(api, "STEAM-TOPUP-500")
    assert order["status"] == "created"
    assert order["amount"] == 500.0 and order["currency"] == "RUB"
    assert order["issuance"] is None

    r = await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
    assert r.status_code == 200
    assert r.json()["result"] == "applied"

    final = await wait_status(api, order["id"], {"delivered"})
    assert final["issuance"] is not None
    assert final["issuance"]["code"]
    assert final["paid_at"] and final["delivered_at"]


async def test_get_order_404(api):
    assert (await api.get("/orders/ord_nope")).status_code == 404


async def test_failed_payment_is_terminal(api):
    order = await create_order(api, "SUB-DISCORD-1M")
    r = await api.post(
        "/webhook/payment", json=evt(order["id"], order["amount"], status="failed")
    )
    assert r.json()["result"] == "applied"

    got = (await api.get(f"/orders/{order['id']}")).json()
    assert got["status"] == "payment_failed"

    # Оплата после отказа заказ не воскрешает: статус финальный.
    r = await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
    assert r.json()["result"] == "ignored"
    got = (await api.get(f"/orders/{order['id']}")).json()
    assert got["status"] == "payment_failed"
    assert got["issuance"] is None


async def test_amount_mismatch_is_rejected(api):
    order = await create_order(api, "GIFT-XBOX-1500")
    r = await api.post("/webhook/payment", json=evt(order["id"], 1.0))
    assert r.status_code == 200
    assert r.json()["result"] == "rejected"

    got = (await api.get(f"/orders/{order['id']}")).json()
    assert got["status"] == "created"


async def test_order_creation_is_idempotent_with_client_id(api):
    oid = f"ord_idem_{uuid.uuid4().hex[:8]}"
    first = await create_order(api, "SUB-YT-3M", order_id=oid)
    r = await api.post("/orders", json={"sku": "SUB-YT-3M", "order_id": oid})
    assert r.status_code == 200          # 200, а не 201 - заказ уже был
    assert r.json()["id"] == first["id"]

    conflict = await api.post("/orders", json={"sku": "KEY-GTA5", "order_id": oid})
    assert conflict.status_code == 409


async def test_timeline_records_every_step(api):
    order = await create_order(api, "GIFT-ROBLOX-800")
    await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
    await wait_status(api, order["id"], {"delivered"})

    tl = await timeline(api, order["id"])
    assert tl["order"]["status"] == "delivered"
    assert len(tl["payment_events"]) == 1
    assert tl["payment_events"][0]["processing_state"] == "applied"
    assert tl["issuance"]["code"]
    assert len(tl["supplier_attempts"]) >= 1
    # Оплата (2 строки) + себестоимость выдачи (2 строки).
    assert len(tl["ledger_entries"]) == 4
