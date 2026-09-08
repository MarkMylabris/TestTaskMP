"""Поставщику нельзя доверять.

A ведёт себя нечестно тремя способами: отдаёт уже выданный кому-то код, код
от другого товара и отвечает ошибкой, выдав код. Клиент не должен получить
чужой, второй или несуществующий код, и разбирать это руками не нужно.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from tests.conftest import (
    SUP_A,
    control,
    create_multi_order,
    create_order,
    restock,
    supplier_request,
    timeline,
    wait_status,
)

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
    assert (await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
            ).status_code == 200


async def code_owner(supplier: str, code: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{SUP_A}/{supplier}/codes/{code}")
        r.raise_for_status()
        return r.json()


def kinds(tl: dict) -> list[str]:
    return [d["kind"] for d in tl["supplier_discrepancies"]]


async def wait_discrepancy(api, order_id: str, kind: str, timeout: float = 30.0) -> dict:
    """Дождаться, пока подлог заметят, без гонки на sleep."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        tl = await timeline(api, order_id)
        if kind in kinds(tl):
            return tl
        await asyncio.sleep(0.15)
    raise AssertionError(f"расхождение {kind} по заказу {order_id} не зафиксировано")


# --------------------------------------------------------------------------- #
# Критерий 1: один код не уходит в два заказа
# --------------------------------------------------------------------------- #
async def test_duplicate_code_from_supplier_never_reaches_second_order(api):
    """A отдаёт второму заказу код, который уже выдал первому."""
    # Ключи именно у A: подлог должен быть чем совершить, а A опрашивается первым.
    await restock("a", "STEAM-TOPUP-1000", 5)
    first = await create_order(api, "STEAM-TOPUP-1000")
    await pay(api, first)
    first_final = await wait_status(api, first["id"], {"delivered"})
    stolen = first_final["issuance"]["code"]
    assert first_final["issuance"]["supplier"] == "a"

    await control("a", {"mode": "duplicate_code"})
    await restock("a", "STEAM-TOPUP-1000", 5)
    second = await create_order(api, "STEAM-TOPUP-1000")
    await pay(api, second)
    await wait_discrepancy(api, second["id"], "duplicate_code")
    await control("a", {"mode": "ok"})

    second_final = await wait_status(api, second["id"], {"delivered"}, timeout=60)
    assert second_final["issuance"]["code"] != stolen, "дубль не ушёл второму заказу"

    # У первого заказа код не отобрали.
    still = (await api.get(f"/orders/{first['id']}")).json()
    assert still["issuance"]["code"] == stolen

    tl = await timeline(api, second["id"])
    assert "duplicate_code" in kinds(tl), "подлог зафиксирован"
    assert all(d["resolved_at"] for d in tl["supplier_discrepancies"]), "разобрано само"
    # Повтор пошёл под новым поколением request_id - иначе поставщик по
    # идемпотентности вернул бы тот же самый чужой код.
    assert len({a["request_id"] for a in tl["supplier_attempts"]}) >= 2


async def test_no_code_is_shared_between_orders(api):
    """Сквозная проверка по всей базе: одинаковых кодов быть не может."""
    stats = (await api.get("/admin/stats")).json()
    rec = (await api.get("/admin/reconciliation", params={"grace_seconds": 0})).json()
    assert stats["issuances"] >= 1
    assert rec["delivered_not_paid"]["count"] == 0
    assert rec["supplier_discrepancies"]["needs_attention"]["count"] == 0


# --------------------------------------------------------------------------- #
# Критерий 2: покупатель получает ровно один рабочий код
# --------------------------------------------------------------------------- #
async def test_foreign_code_is_rejected(api):
    """A отдаёт код от другого товара."""
    await restock("a", "GIFT-XBOX-1500", 3)
    await control("a", {"mode": "foreign_code"})
    order = await create_order(api, "GIFT-XBOX-1500")
    await pay(api, order)
    await wait_discrepancy(api, order["id"], "foreign_code")
    await control("a", {"mode": "ok"})

    final = await wait_status(api, order["id"], {"delivered"}, timeout=60)
    code = final["issuance"]["code"]

    owner = await code_owner(final["issuance"]["supplier"], code)
    assert owner["sku"] == "GIFT-XBOX-1500", "выдан код именно заказанного товара"
    assert owner["request_id"] == final["issuance"]["request_id"], "код закреплён за заказом"

    tl = await timeline(api, order["id"])
    assert "foreign_code" in kinds(tl)
    assert len(tl["issuances"]) == 1, "ровно один код"


# --------------------------------------------------------------------------- #
# Критерий 3: ответил ошибкой, но код выдал
# --------------------------------------------------------------------------- #
async def test_error_after_issue_does_not_produce_second_code(api):
    await restock("a", "SUB-SPOTIFY-1M", 3)
    await control("a", {"mode": "error_after_issue"})
    order = await create_order(api, "SUB-SPOTIFY-1M")
    await pay(api, order)

    final = await wait_status(api, order["id"], {"delivered"}, timeout=60)
    await control("a", {"mode": "ok"})

    assert final["issuance"]["supplier"] == "a", (
        "код был выдан A, уходить к B нельзя - это был бы второй код"
    )
    # Тот самый код, который A у себя зафиксировал.
    recorded = await supplier_request("a", final["issuance"]["request_id"])
    assert recorded.status_code == 200
    assert recorded.json()["code"] == final["issuance"]["code"]

    tl = await timeline(api, order["id"])
    assert "phantom_error" in kinds(tl), "расхождение 'ошибка, но код выдан' зафиксировано"
    assert len(tl["issuances"]) == 1


async def test_error_after_issue_survives_repeats(api):
    """Повтор шага выдачи не создаёт второй выдачи даже при вранье поставщика."""
    await restock("a", "SUB-DISCORD-1M", 3)
    await restock("a", "KEY-CS2-PRIME", 3)
    await control("a", {"mode": "error_after_issue"})
    order = await create_multi_order(api, [{"sku": "SUB-DISCORD-1M"}, {"sku": "KEY-CS2-PRIME"}])
    await pay(api, order)
    final = await wait_status(api, order["id"], {"delivered"}, timeout=90)
    await control("a", {"mode": "ok"})

    codes = sorted(i["issuance"]["code"] for i in final["items"])

    await api.post(f"/admin/orders/{order['id']}/redeliver")
    await asyncio.sleep(1.5)
    after = (await api.get(f"/orders/{order['id']}")).json()
    assert sorted(i["issuance"]["code"] for i in after["items"]) == codes

    tl = await timeline(api, order["id"])
    assert len(tl["issuances"]) == 2
    assert sum(e["amount_minor"] for e in tl["ledger_entries"]) == 0


# --------------------------------------------------------------------------- #
# Критерий 4: расхождения разбираются автоматически
# --------------------------------------------------------------------------- #
async def test_discrepancies_are_resolved_without_hands(api):
    report = (await api.get("/admin/discrepancies")).json()
    seen = {r["kind"] for r in report["by_kind"]}
    assert {"duplicate_code", "foreign_code", "phantom_error"} <= seen

    assert report["needs_attention"]["count"] == 0, (
        "по закрытым позициям неразобранных расхождений не остаётся"
    )
    money = (await api.get("/admin/money")).json()
    assert money["balanced"], money["equation"]
