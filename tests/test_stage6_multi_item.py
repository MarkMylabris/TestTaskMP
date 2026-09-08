"""Заказ из нескольких товаров: каркас.

Заказ - агрегат над позициями, у каждой своя выдача, свой поставщик и свои
деньги. Частичный сбой и возвраты проверяются отдельно.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from tests.conftest import create_multi_order, create_order, timeline, wait_status

pytestmark = pytest.mark.asyncio


def evt(order_id: str, amount: float) -> dict:
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "order_id": order_id,
        "status": "paid",
        "amount": amount,
        "currency": "RUB",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def pay(api, order: dict) -> None:
    r = await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
    assert r.status_code == 200


async def test_order_total_is_sum_of_items(api):
    order = await create_multi_order(
        api, [{"sku": "KEY-CS2-PRIME"}, {"sku": "SUB-YT-3M", "qty": 2}]
    )
    assert len(order["items"]) == 3, "qty разворачивается в отдельные позиции"
    assert [i["position"] for i in order["items"]] == [1, 2, 3]
    assert order["amount_minor"] == sum(i["amount_minor"] for i in order["items"])
    assert order["amount_minor"] == 1290_00 + 1490_00 * 2
    assert order["sku"] is None, "у многотоварного заказа нет одного sku"


async def test_every_item_gets_its_own_code(api):
    order = await create_multi_order(
        api, [{"sku": "KEY-CS2-PRIME"}, {"sku": "KEY-GTA5"}, {"sku": "GIFT-PSN-1000"}]
    )
    await pay(api, order)
    final = await wait_status(api, order["id"], {"delivered"})

    items = final["items"]
    assert len(items) == 3
    assert all(i["status"] == "delivered" for i in items)
    codes = [i["issuance"]["code"] for i in items]
    assert len(set(codes)) == 3, "у каждой позиции свой код"
    assert [i["sku"] for i in items] == ["KEY-CS2-PRIME", "KEY-GTA5", "GIFT-PSN-1000"]

    tl = await timeline(api, order["id"])
    assert len(tl["issuances"]) == 3
    # request_id привязан к позиции, а не к заказу: иначе поставщик отдал бы
    # на второй товар тот же самый код по идемпотентности.
    assert len({a["request_id"] for a in tl["supplier_attempts"]}) >= 3
    assert sum(e["amount_minor"] for e in tl["ledger_entries"]) == 0
    costs = [e for e in tl["ledger_entries"] if e["kind"] == "delivery_cost"]
    assert len({e["item_id"] for e in costs}) == 3, "себестоимость считается по позициям"


async def test_single_item_order_keeps_stage1_contract(api):
    order = await create_order(api, "SUB-DISCORD-1M")
    await pay(api, order)
    final = await wait_status(api, order["id"], {"delivered"})
    assert final["sku"] == "SUB-DISCORD-1M"
    assert final["issuance"]["code"]
    assert final["items"][0]["issuance"]["code"] == final["issuance"]["code"]


async def test_repeated_create_is_idempotent_by_basket(api):
    oid = f"ord_multi_{uuid.uuid4().hex[:8]}"
    basket = [{"sku": "KEY-CS2-PRIME"}, {"sku": "KEY-GTA5"}]
    first = await create_multi_order(api, basket, order_id=oid)

    r = await api.post("/orders", json={"items": basket, "order_id": oid})
    assert r.status_code == 200, "повтор не создаёт второй заказ"
    assert [i["id"] for i in r.json()["items"]] == [i["id"] for i in first["items"]]

    other = await api.post(
        "/orders", json={"items": [{"sku": "KEY-EFT"}], "order_id": oid}
    )
    assert other.status_code == 409, "тот же id с другим составом - конфликт"


async def test_order_form_validation(api):
    both = await api.post("/orders", json={"sku": "KEY-GTA5", "items": [{"sku": "KEY-EFT"}]})
    assert both.status_code == 422
    neither = await api.post("/orders", json={"customer_email": "a@b.c"})
    assert neither.status_code == 422
    unknown = await api.post("/orders", json={"items": [{"sku": "NOPE-000"}]})
    assert unknown.status_code == 404
