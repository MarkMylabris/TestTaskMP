"""Этап 3: устойчивые интеграции. Критерии приёмки 4, 5, 6.

Главное, что здесь проверяется: таймаут поставщика НЕ равен отказу.
Поставщик мог успеть выдать код, и ни повтор, ни переключение на резервного
поставщика не должны привести ко второй выдаче.
"""
from __future__ import annotations

import asyncio
import os
import signal
import uuid
from datetime import datetime, timezone

from tests.conftest import (
    SUP_A_PORT,
    control,
    create_order,
    restock,
    supplier_request,
    timeline,
    wait_status,
)


def evt(order_id: str, amount: float) -> dict:
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "order_id": order_id,
        "status": "paid",
        "amount": amount,
        "currency": "RUB",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


async def pay(api, order: dict) -> None:
    r = await api.post("/webhook/payment", json=evt(order["id"], order["amount"]))
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Критерий 4: таймаут поставщика, который на самом деле выдал код
# --------------------------------------------------------------------------- #
async def test_timeout_after_issue_does_not_double_issue(api):
    """A выдаёт код и "зависает". Повтор идёт с тем же request_id -> одна выдача."""
    await control("a", {"mode": "timeout_after_issue", "hang_seconds": 2.0})
    await restock("a", "KEY-EFT", 5)

    order = await create_order(api, "KEY-EFT")
    await pay(api, order)

    final = await wait_status(api, order["id"], {"delivered"}, timeout=60)
    tl = await timeline(api, order["id"])

    assert final["issuance"]["supplier"] == "a", "код должен прийти именно от A"

    request_ids = {a["request_id"] for a in tl["supplier_attempts"]}
    a_requests = {r for r in request_ids if r.endswith("-a")}
    assert len(a_requests) == 1, f"request_id к A обязан быть стабильным: {a_requests}"

    # У поставщика A под этим request_id ровно один код - тот же, что у клиента.
    rid = final["issuance"]["request_id"]
    r = await supplier_request("a", rid)
    assert r.status_code == 200
    assert r.json()["code"] == final["issuance"]["code"]

    # B к этому заказу вообще не привлекался: fallback из "неизвестно" запрещён.
    assert not [a for a in tl["supplier_attempts"] if a["supplier"] == "b"]
    b_probe = await supplier_request("b", f"req_{order['id']}-b")
    assert b_probe.status_code == 404, "B не должен был выдавать второй код"

    assert len([e for e in tl["ledger_entries"] if e["kind"] == "delivery_cost"]) == 2


async def test_pure_timeout_resolves_by_probe_then_falls_back(api):
    """A зависает, НИЧЕГО не выдав.

    Сам по себе таймаут не даёт права уходить к B. Но статус-запрос по тому же
    request_id отвечает 404 - "такого запроса не было", значит код точно не
    выдан, и переключение на B становится безопасным.
    """
    await control("a", {"mode": "timeout", "hang_seconds": 2.0})
    await restock("b", "KEY-EFT", 5)

    order = await create_order(api, "KEY-EFT")
    await pay(api, order)

    final = await wait_status(api, order["id"], {"delivered"}, timeout=60)
    tl = await timeline(api, order["id"])

    assert final["issuance"]["supplier"] == "b"
    a_attempts = [a for a in tl["supplier_attempts"] if a["supplier"] == "a"]
    assert a_attempts and all(a["state"] == "unknown" for a in a_attempts), (
        "таймаут обязан фиксироваться как 'unknown', а не как отказ"
    )
    assert (await supplier_request("a", f"req_{order['id']}-a")).status_code == 404


async def test_unresolvable_timeout_blocks_fallback(api):
    """Исход у A выяснить нечем: и /issue, и статус-запрос зависают.

    Единственное безопасное поведение - НЕ переключаться на B, оставить заказ
    в восстановимом состоянии и повторять тем же request_id. Иначе клиент
    получит два кода, а мы - двойную себестоимость.
    """
    await control("a", {"mode": "timeout_after_issue", "hang_seconds": 8.0,
                        "probe_hangs": True})
    await restock("a", "KEY-EFT", 5)
    await restock("b", "KEY-EFT", 5)

    order = await create_order(api, "KEY-EFT")
    await pay(api, order)

    await asyncio.sleep(6)
    mid = (await api.get(f"/orders/{order['id']}")).json()
    assert mid["issuance"] is None, "нельзя выдавать код, пока исход у A неизвестен"

    tl = await timeline(api, order["id"])
    assert not [a for a in tl["supplier_attempts"] if a["supplier"] == "b"], (
        "fallback на B при неразрешённом таймауте запрещён"
    )
    assert any(a["state"] == "unknown" for a in tl["supplier_attempts"])

    # Сверка показывает заказ как требующий внимания, но деньги сходятся.
    rec = (await api.get("/admin/reconciliation", params={"grace_seconds": 0})).json()
    assert order["id"] in [i["id"] for i in rec["paid_not_delivered"]["items"]]
    assert rec["unresolved_supplier_attempts"]["count"] >= 1
    assert rec["ledger"]["balanced"]

    # A ожил: тот же request_id возвращает тот самый, ранее выданный код.
    await control("a", {"mode": "ok", "probe_hangs": False})
    final = await wait_status(api, order["id"], {"delivered"}, timeout=90)
    tl = await timeline(api, order["id"])

    assert final["issuance"]["supplier"] == "a"
    a_requests = {a["request_id"] for a in tl["supplier_attempts"] if a["supplier"] == "a"}
    assert len(a_requests) == 1
    rid = a_requests.pop()
    r = await supplier_request("a", rid)
    assert r.json()["code"] == final["issuance"]["code"], (
        "клиент получил ровно тот код, который A зафиксировал в самой первой попытке"
    )
    assert (await supplier_request("b", f"req_{order['id']}-b")).status_code == 404


# --------------------------------------------------------------------------- #
# Критерий 5: поставщик A недоступен -> fallback на B, ровно одна выдача
# --------------------------------------------------------------------------- #
async def test_fallback_to_b_when_a_returns_error(api):
    await control("a", {"mode": "error_5xx"})
    await restock("b", "KEY-GTA5", 5)

    order = await create_order(api, "KEY-GTA5")
    await pay(api, order)

    final = await wait_status(api, order["id"], {"delivered"}, timeout=60)
    tl = await timeline(api, order["id"])

    assert final["issuance"]["supplier"] == "b"
    assert [a for a in tl["supplier_attempts"] if a["supplier"] == "a" and a["state"] == "failed"]
    # У A кода нет - он ошибался ДО выдачи.
    assert (await supplier_request("a", f"req_{order['id']}-a")).status_code == 404


async def test_fallback_to_b_when_a_is_down(stack, api):
    """A физически недоступен (процесс убит) -> соединение отвергнуто -> это ТОЧНЫЙ отказ."""
    await restock("b", "KEY-GTA5", 5)
    proc = stack["sup_a"]
    # Именно SIGKILL: нужен обрыв "насмерть", а не корректное завершение -
    # клиент должен получить connection refused, то есть ТОЧНЫЙ отказ.
    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    proc.wait(timeout=15)
    try:
        order = await create_order(api, "KEY-GTA5")
        await pay(api, order)

        final = await wait_status(api, order["id"], {"delivered"}, timeout=60)
        tl = await timeline(api, order["id"])

        assert final["issuance"]["supplier"] == "b"
        a_attempts = [a for a in tl["supplier_attempts"] if a["supplier"] == "a"]
        assert a_attempts and all(a["state"] == "failed" for a in a_attempts)
        assert all("connect" in (a["reason"] or "") for a in a_attempts)
    finally:
        import subprocess
        import sys

        from tests.conftest import ENV, ROOT, _wait_http

        stack["sup_a"] = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "suppliers.main:app", "--host", "127.0.0.1",
             "--port", str(SUP_A_PORT), "--log-level", "warning"],
            cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _wait_http(f"http://127.0.0.1:{SUP_A_PORT}/health")


async def test_both_suppliers_flaky_still_exactly_once(api):
    """Оба поставщика случайно падают и зависают - 10 заказов, 10 разных кодов."""
    await restock("a", "SUB-YT-3M", 20)
    await restock("b", "SUB-YT-3M", 20)
    await control("a", {"mode": "random", "error_rate": 0.4, "timeout_rate": 0.3,
                        "timeout_after_issue_share": 0.5, "hang_seconds": 1.5})
    await control("b", {"mode": "random", "error_rate": 0.3, "timeout_rate": 0.2,
                        "timeout_after_issue_share": 0.5, "hang_seconds": 1.5})

    orders = [await create_order(api, "SUB-YT-3M") for _ in range(10)]
    await asyncio.gather(*(pay(api, o) for o in orders))

    await control("a", {"mode": "ok"})   # даём системе дошагать до конца
    await control("b", {"mode": "ok"})

    codes = []
    for o in orders:
        final = await wait_status(api, o["id"], {"delivered"}, timeout=90)
        codes.append(final["issuance"]["code"])

    assert len(set(codes)) == 10, "каждый заказ получил свой уникальный код"

    balance = (await api.get("/admin/ledger/balance")).json()
    assert balance["balanced"]


# --------------------------------------------------------------------------- #
# Критерий 6: пустой остаток -> восстановимое состояние без падения
# --------------------------------------------------------------------------- #
async def test_out_of_stock_is_recoverable(api):
    await control("a", {"out_of_stock_skus": ["GIFT-ROBLOX-800"]})
    await control("b", {"out_of_stock_skus": ["GIFT-ROBLOX-800"]})

    order = await create_order(api, "GIFT-ROBLOX-800")
    await pay(api, order)

    stalled = await wait_status(api, order["id"], {"out_of_stock"}, timeout=30)
    assert stalled["issuance"] is None
    assert stalled["last_error"]

    # Сервис жив и отвечает.
    assert (await api.get("/health")).status_code == 200

    # Сверка видит заказ как "оплачен, но не выдан".
    rec = (await api.get("/admin/reconciliation", params={"grace_seconds": 0})).json()
    assert order["id"] in [i["id"] for i in rec["paid_not_delivered"]["items"]]
    assert rec["ledger"]["balanced"], "деньги сходятся даже при неудачной выдаче"

    # Пополнили остаток - заказ доводится сам, без ручного вмешательства.
    await control("a", {"out_of_stock_skus": []})
    await control("b", {"out_of_stock_skus": []})
    await restock("a", "GIFT-ROBLOX-800", 3)

    final = await wait_status(api, order["id"], {"delivered"}, timeout=60)
    assert final["issuance"] is not None

    rec = (await api.get("/admin/reconciliation", params={"grace_seconds": 0})).json()
    assert order["id"] not in [i["id"] for i in rec["paid_not_delivered"]["items"]]
