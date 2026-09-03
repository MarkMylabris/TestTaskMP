from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import CreateOrderRequest, IssuanceOut, OrderOut
from app.services import orders as orders_svc

router = APIRouter(prefix="/orders", tags=["orders"])


def _to_out(order_row, issuance_row) -> OrderOut:
    return OrderOut(
        id=order_row["id"],
        sku=order_row["sku"],
        amount=order_row["amount_minor"] / 100,
        amount_minor=order_row["amount_minor"],
        currency=order_row["currency"],
        status=order_row["status"],
        delivery_attempts=order_row["delivery_attempts"],
        last_error=order_row["last_error"],
        created_at=order_row["created_at"],
        paid_at=order_row["paid_at"],
        delivered_at=order_row["delivered_at"],
        issuance=IssuanceOut(**issuance_row) if issuance_row else None,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=OrderOut)
async def create_order(
    body: CreateOrderRequest, response: Response, session: AsyncSession = Depends(get_session)
):
    try:
        order, created = await orders_svc.create_order(
            session, body.sku, body.customer_email, body.order_id
        )
    except LookupError:
        raise HTTPException(status_code=404, detail=f"unknown sku: {body.sku}")
    except orders_svc.OrderConflict:
        raise HTTPException(status_code=409, detail="order_id already used with another sku")
    await session.flush()
    if not created:
        response.status_code = status.HTTP_200_OK
    return OrderOut(
        id=order.id,
        sku=order.sku,
        amount=order.amount_minor / 100,
        amount_minor=order.amount_minor,
        currency=order.currency,
        status=order.status,
        delivery_attempts=order.delivery_attempts,
        last_error=order.last_error,
        created_at=order.created_at,
        paid_at=order.paid_at,
        delivered_at=order.delivered_at,
        issuance=None,
    )


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: str, session: AsyncSession = Depends(get_session)):
    row = (
        await session.execute(text("SELECT * FROM orders WHERE id=:id"), {"id": order_id})
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")
    issuance = (
        await session.execute(
            text(
                "SELECT code, supplier, request_id, created_at FROM issuances WHERE order_id=:id"
            ),
            {"id": order_id},
        )
    ).mappings().first()
    return _to_out(row, dict(issuance) if issuance else None)
