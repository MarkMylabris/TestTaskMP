"""Эмулятор платёжной системы. Он же - инструмент проверки гонок.

Примеры:

    # обычная оплата
    python -m scripts.payment_sim pay --order ord_x

    # 50 параллельных вебхуков "оплачено" по одному заказу (критерий 1)
    python -m scripts.payment_sim race --order ord_x --concurrency 50

    # повтор того же event_id (критерий 2)
    python -m scripts.payment_sim race --order ord_x --concurrency 50 --same-event

    # вебхук раньше заказа (критерий 3)
    python -m scripts.payment_sim pay --order ord_does_not_exist_yet

    # полный сквозной сценарий: создать заказ -> 50 вебхуков -> дождаться выдачи
    python -m scripts.payment_sim scenario --sku KEY-CS2-PRIME --concurrency 50
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from datetime import datetime, timezone

import httpx

DEFAULT_API = "http://127.0.0.1:8000"


def _event(order_id: str, amount: float, currency: str, status: str, event_id: str) -> dict:
    return {
        "event_id": event_id,
        "order_id": order_id,
        "status": status,
        "amount": amount,
        "currency": currency,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


async def _send(client: httpx.AsyncClient, api: str, payload: dict) -> dict:
    r = await client.post(f"{api}/webhook/payment", json=payload)
    return {"http": r.status_code, **(r.json() if r.headers.get("content-type","").startswith("application/json") else {})}


async def create_order(client: httpx.AsyncClient, api: str, sku: str) -> dict:
    r = await client.post(f"{api}/orders", json={"sku": sku})
    r.raise_for_status()
    return r.json()


async def get_order(client: httpx.AsyncClient, api: str, order_id: str) -> dict:
    r = await client.get(f"{api}/orders/{order_id}")
    r.raise_for_status()
    return r.json()


async def wait_delivered(
    client: httpx.AsyncClient, api: str, order_id: str, timeout: float = 30.0
) -> dict:
    deadline = time.monotonic() + timeout
    order = await get_order(client, api, order_id)
    while time.monotonic() < deadline:
        if order["status"] in ("delivered", "payment_failed"):
            return order
        await asyncio.sleep(0.2)
        order = await get_order(client, api, order_id)
    return order


async def race(
    api: str, order_id: str, amount: float, currency: str, n: int, same_event: bool, status: str
) -> list[dict]:
    """Отправить n вебхуков строго одновременно (барьер на asyncio.Event)."""
    gate = asyncio.Event()
    shared_event_id = f"evt_{uuid.uuid4().hex[:12]}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        async def one(i: int) -> dict:
            eid = shared_event_id if same_event else f"evt_{uuid.uuid4().hex[:12]}"
            await gate.wait()
            return await _send(client, api, _event(order_id, amount, currency, status, eid))

        tasks = [asyncio.create_task(one(i)) for i in range(n)]
        await asyncio.sleep(0.1)   # дать всем корутинам дойти до барьера
        gate.set()
        return await asyncio.gather(*tasks)


def summarize(results: list[dict]) -> dict:
    by_result: dict[str, int] = {}
    by_http: dict[int, int] = {}
    for r in results:
        by_result[r.get("result", "?")] = by_result.get(r.get("result", "?"), 0) + 1
        by_http[r["http"]] = by_http.get(r["http"], 0) + 1
    return {"by_result": by_result, "by_http": by_http, "total": len(results)}


async def cmd_pay(args) -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        amount, currency = args.amount, args.currency
        if amount is None:
            try:
                order = await get_order(client, args.api, args.order)
                amount, currency = order["amount"], order["currency"]
            except httpx.HTTPStatusError:
                amount, currency = 500.0, "RUB"   # заказа ещё нет - сценарий "вебхук раньше"
        payload = _event(args.order, amount, currency, args.status, f"evt_{uuid.uuid4().hex[:12]}")
        print(json.dumps(await _send(client, args.api, payload), ensure_ascii=False, indent=2))


async def cmd_race(args) -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        order = await get_order(client, args.api, args.order)
    results = await race(
        args.api, args.order, order["amount"], order["currency"],
        args.concurrency, args.same_event, args.status,
    )
    print(json.dumps(summarize(results), ensure_ascii=False, indent=2))

    async with httpx.AsyncClient(timeout=30.0) as client:
        final = await wait_delivered(client, args.api, args.order, args.wait)
    print(json.dumps(final, ensure_ascii=False, indent=2))


async def cmd_scenario(args) -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        order = await create_order(client, args.api, args.sku)
        print(f"created order {order['id']} ({order['sku']}, {order['amount']} {order['currency']})")

    t0 = time.monotonic()
    results = await race(
        args.api, order["id"], order["amount"], order["currency"],
        args.concurrency, args.same_event, "paid",
    )
    print(f"{args.concurrency} webhooks in {time.monotonic()-t0:.2f}s -> "
          f"{json.dumps(summarize(results), ensure_ascii=False)}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        final = await wait_delivered(client, args.api, order["id"], args.wait)
        r = await client.get(f"{args.api}/admin/orders/{order['id']}/timeline")
        timeline = r.json()

    issued = timeline["issuance"]
    print(json.dumps(
        {
            "order_id": order["id"],
            "final_status": final["status"],
            "issuances": 1 if issued else 0,
            "code": issued["code"] if issued else None,
            "supplier": issued["supplier"] if issued else None,
            "payment_events": len(timeline["payment_events"]),
            "applied_events": sum(
                1 for e in timeline["payment_events"] if e["processing_state"] == "applied"
            ),
            "supplier_attempts": len(timeline["supplier_attempts"]),
            "ledger_entries": len(timeline["ledger_entries"]),
        },
        ensure_ascii=False, indent=2,
    ))
    ok = final["status"] == "delivered" and issued is not None
    print("VERDICT:", "OK - ровно одна выдача" if ok else "FAILED")


def main() -> None:
    ap = argparse.ArgumentParser(description="Эмулятор платёжки и проверка гонок")
    ap.add_argument("--api", default=DEFAULT_API)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pay", help="один вебхук")
    p.add_argument("--order", required=True)
    p.add_argument("--status", default="paid", choices=["paid", "failed"])
    p.add_argument("--amount", type=float, default=None)
    p.add_argument("--currency", default="RUB")
    p.set_defaults(fn=cmd_pay)

    p = sub.add_parser("race", help="N параллельных вебхуков по одному заказу")
    p.add_argument("--order", required=True)
    p.add_argument("--concurrency", type=int, default=50)
    p.add_argument("--same-event", action="store_true", help="один и тот же event_id")
    p.add_argument("--status", default="paid", choices=["paid", "failed"])
    p.add_argument("--wait", type=float, default=30.0)
    p.set_defaults(fn=cmd_race)

    p = sub.add_parser("scenario", help="создать заказ, обстрелять вебхуками, дождаться выдачи")
    p.add_argument("--sku", default="KEY-CS2-PRIME")
    p.add_argument("--concurrency", type=int, default=50)
    p.add_argument("--same-event", action="store_true")
    p.add_argument("--wait", type=float, default=60.0)
    p.set_defaults(fn=cmd_scenario)

    args = ap.parse_args()
    asyncio.run(args.fn(args))


if __name__ == "__main__":
    main()
