from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import PaymentWebhook, WebhookAck
from app.services.payments import handle_payment_event

router = APIRouter(tags=["webhooks"])


@router.post("/webhook/payment", response_model=WebhookAck)
async def payment_webhook(body: PaymentWebhook, session: AsyncSession = Depends(get_session)):
    """Вебхук платёжной системы.

    Отвечает быстро: внутри только БД, никаких походов к поставщикам.
    Всегда 200, если событие принято - иначе платёжка будет ретраить то,
    что мы уже сохранили. 5xx отдаём только при реальном сбое БД (тогда
    ретрай платёжки - именно то, что нужно).
    """
    if body.status not in ("paid", "failed"):
        raise HTTPException(status_code=422, detail="status must be 'paid' or 'failed'")

    result = await handle_payment_event(
        session,
        event_id=body.event_id,
        order_id=body.order_id,
        status=body.status,
        amount_minor=int(round(body.amount * 100)),
        currency=body.currency,
        event_created_at=body.created_at,
        payload=body.model_dump(mode="json"),
    )
    return WebhookAck(
        accepted=result.accepted,
        event_id=body.event_id,
        order_id=body.order_id,
        result=result.state,
        order_status=result.order_status,
        note=result.note,
    )
