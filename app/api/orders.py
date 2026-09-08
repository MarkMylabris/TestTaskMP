from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import CreateOrderRequest, IssuanceOut, OrderItemOut, OrderOut
from app.services import orders as orders_svc

router = APIRouter(prefix="/orders", tags=["orders"])

ORDER_SQL = text("SELECT * FROM orders WHERE id = :id")

ITEMS_SQL = text(
    """
    SELECT i.id, i.position, i.sku, i.amount_minor, i.currency, i.status,
           i.supplier, i.delivery_attempts, i.last_error, i.delivered_at, i.refunded_at,
           s.code, s.supplier AS issued_supplier, s.request_id, s.created_at AS issued_at
      FROM order_items i
      LEFT JOIN issuances s ON s.order_item_id = i.id
     WHERE i.order_id = :id
     ORDER BY i.position
    """
)


def _item_out(row) -> OrderItemOut:
    issuance = (
        IssuanceOut(
            code=row["code"], supplier=row["issued_supplier"],
            request_id=row["request_id"], created_at=row["issued_at"],
        )
        if row["code"]
        else None
    )
    return OrderItemOut(
        id=row["id"],
        position=row["position"],
        sku=row["sku"],
        amount=row["amount_minor"] / 100,
        amount_minor=row["amount_minor"],
        currency=row["currency"],
        status=row["status"],
        supplier=row["supplier"] or row["issued_supplier"],
        delivery_attempts=row["delivery_attempts"],
        last_error=row["last_error"],
        delivered_at=row["delivered_at"],
        refunded_at=row["refunded_at"],
        issuance=issuance,
    )


def _order_out(order_row, item_rows) -> OrderOut:
    items = [_item_out(r) for r in item_rows]
    single = items[0] if len(items) == 1 else None
    return OrderOut(
        id=order_row["id"],
        amount=order_row["amount_minor"] / 100,
        amount_minor=order_row["amount_minor"],
        currency=order_row["currency"],
        status=order_row["status"],
        delivery_attempts=order_row["delivery_attempts"],
        last_error=order_row["last_error"],
        created_at=order_row["created_at"],
        paid_at=order_row["paid_at"],
        delivered_at=order_row["delivered_at"],
        items=items,
        sku=single.sku if single else None,
        issuance=single.issuance if single else None,
    )


async def _load(session: AsyncSession, order_id: str) -> OrderOut | None:
    order = (await session.execute(ORDER_SQL, {"id": order_id})).mappings().first()
    if order is None:
        return None
    items = (await session.execute(ITEMS_SQL, {"id": order_id})).mappings().all()
    return _order_out(order, items)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=OrderOut)
async def create_order(
    body: CreateOrderRequest, response: Response, session: AsyncSession = Depends(get_session)
):
    try:
        order, _, created = await orders_svc.create_order(
            session, body.sku_list(), body.customer_email, body.order_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=f"unknown sku: {exc.args[0]}")
    except orders_svc.OrderConflict:
        raise HTTPException(status_code=409, detail="order_id already used with another basket")
    except orders_svc.MixedCurrency as exc:
        raise HTTPException(
            status_code=422, detail=f"mixed currencies in one order: {exc.args[0]}"
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    await session.flush()
    if not created:
        response.status_code = status.HTTP_200_OK
    return await _load(session, order.id)


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: str, session: AsyncSession = Depends(get_session)):
    out = await _load(session, order_id)
    if out is None:
        raise HTTPException(status_code=404, detail="order not found")
    return out
