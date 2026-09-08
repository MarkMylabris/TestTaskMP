"""Частичный сбой заказа и сходимость денег.

Сценарий детерминирован: поставщики отвечают, что один из SKU они не продают
(410). Это отказ навсегда, ждать нечего - за позицию возвращаются деньги, а
выданные позиции остаются у покупателя.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from tests.conftest import control, create_multi_order, timeline, wait_status

pytestmark = pytest.mark.asyncio

PRIME = 1290_00
EFT = 3490_00
PSN = 1000_00


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


async def unsupported(sku: str) -> None:
    for s in ("a", "b"):
        await control(s, {"unsupported_skus": [sku]})


def accounts(entries: list[dict]) -> dict[str, int]:
    per = {}
    for e in entries:
        per[e["account"]] = per.get(e["account"], 0) + e["amount_minor"]
    return per


# --------------------------------------------------------------------------- #
# Критерий 2: выданное остаётся, за невыданное возвращаются деньги
# --------------------------------------------------------------------------- #
async def test_partial_failure_keeps_delivered_and_refunds_the_rest(api):
    await unsupported("KEY-EFT")
    order = await create_multi_order(
        api, [{"sku": "KEY-CS2-PRIME"}, {"sku": "KEY-EFT"}, {"sku": "GIFT-PSN-1000"}]
    )
    await pay(api, order)

    final = await wait_status(api, order["id"], {"partially_delivered"}, timeout=60)
    by_sku = {i["sku"]: i for i in final["items"]}

    assert by_sku["KEY-CS2-PRIME"]["status"] == "delivered"
    assert by_sku["GIFT-PSN-1000"]["status"] == "delivered"
    assert by_sku["KEY-CS2-PRIME"]["issuance"]["code"]
    assert by_sku["GIFT-PSN-1000"]["issuance"]["code"]

    failed = by_sku["KEY-EFT"]
    assert failed["status"] == "refunded"
    assert failed["issuance"] is None, "за невыданную позицию код не появляется"
    assert failed["refunded_at"] is not None

    # ---- деньги: оплачено = выдано + возвращено
    tl = await timeline(api, order["id"])
    per = accounts(tl["ledger_entries"])
    assert per["customer"] == 0, "обязательство перед клиентом закрыто полностью"
    assert per["delivered"] == PRIME + PSN
    assert per["refund"] == EFT
    assert per["delivered"] + per["refund"] == order["amount_minor"]
    assert sum(e["amount_minor"] for e in tl["ledger_entries"]) == 0


# --------------------------------------------------------------------------- #
# Критерий 3: по деньгам сходится на всей базе, а не только в одном заказе
# --------------------------------------------------------------------------- #
async def test_money_equation_holds_globally(api):
    money = (await api.get("/admin/money")).json()
    assert money["balanced"], money["equation"]
    assert money["paid_minor"] == (
        money["delivered_minor"] + money["refunded_minor"] + money["open_minor"]
    )
    assert money["unsettled_finished_orders"]["count"] == 0

    rec = (await api.get("/admin/reconciliation", params={"grace_seconds": 0})).json()
    assert rec["money"]["balanced"]
    assert rec["ledger"]["balanced"]


# --------------------------------------------------------------------------- #
# Критерий 4: повтор любого шага не плодит ни выдач, ни возвратов
# --------------------------------------------------------------------------- #
async def test_repeating_steps_creates_no_extra_issuance_or_refund(api):
    await unsupported("KEY-EFT")
    order = await create_multi_order(api, [{"sku": "SUB-YT-3M"}, {"sku": "KEY-EFT"}])
    await pay(api, order)
    final = await wait_status(api, order["id"], {"partially_delivered"}, timeout=60)
    code = final["items"][0]["issuance"]["code"]

    # Повтор оплаты, повтор выдачи и повтор возврата - по разу каждого.
    await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
    await api.post(f"/admin/orders/{order['id']}/redeliver")
    again = await api.post(f"/admin/orders/{order['id']}/refund")
    assert again.status_code == 200
    assert again.json()["refunded"] is False, "возвращать уже нечего"

    await asyncio.sleep(1.5)
    after = (await api.get(f"/orders/{order['id']}")).json()
    assert after["status"] == "partially_delivered"
    assert after["items"][0]["issuance"]["code"] == code

    tl = await timeline(api, order["id"])
    kinds = [e["kind"] for e in tl["ledger_entries"]]
    assert kinds.count("payment_captured") == 2, "оплата - одна проводка в две ноги"
    assert kinds.count("refund") == 2
    assert kinds.count("delivery_settled") == 2
    assert len(tl["issuances"]) == 1
    assert accounts(tl["ledger_entries"])["customer"] == 0


# --------------------------------------------------------------------------- #
# Критерий 5: заказ доходит до конечного состояния при любом исходе
# --------------------------------------------------------------------------- #
async def test_order_with_nothing_deliverable_is_fully_refunded(api):
    await unsupported("KEY-EFT")
    order = await create_multi_order(api, [{"sku": "KEY-EFT"}, {"sku": "KEY-EFT"}])
    await pay(api, order)

    final = await wait_status(api, order["id"], {"refunded"}, timeout=60)
    assert [i["status"] for i in final["items"]] == ["refunded", "refunded"]
    assert final["delivered_at"] is None

    tl = await timeline(api, order["id"])
    per = accounts(tl["ledger_entries"])
    assert per["customer"] == 0
    assert per["refund"] == EFT * 2
    assert "delivered" not in per


async def test_unpaid_order_is_not_refunded(api):
    await unsupported("KEY-EFT")
    order = await create_multi_order(api, [{"sku": "KEY-EFT"}])
    r = await api.post(f"/admin/orders/{order['id']}/refund")
    assert r.status_code == 409, "нельзя вернуть то, что не оплачивали"


async def test_two_items_survive_supplier_timeout_trap(api):
    """Поставщик выдал код и "потерял" ответ - повтор не выдаёт второй код."""
    await control("a", {"mode": "timeout_after_issue", "hang_seconds": 2.0})
    order = await create_multi_order(
        api, [{"sku": "STEAM-TOPUP-500"}, {"sku": "STEAM-TOPUP-1000"}]
    )
    await pay(api, order)

    # Ловушка снимается: дальше система обязана разобраться сама.
    await asyncio.sleep(3)
    await control("a", {"mode": "ok"})

    final = await wait_status(api, order["id"], {"delivered"}, timeout=90)
    codes = [i["issuance"]["code"] for i in final["items"]]
    assert len(set(codes)) == 2, "две позиции - два разных кода"

    tl = await timeline(api, order["id"])
    assert len(tl["issuances"]) == 2, "ни одной лишней выдачи"
    assert accounts(tl["ledger_entries"])["customer"] == 0
