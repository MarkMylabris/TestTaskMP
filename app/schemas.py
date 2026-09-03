from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CreateOrderRequest(BaseModel):
    sku: str = Field(..., min_length=1, max_length=64)
    customer_email: str | None = None
    # Клиент может предложить свой id заказа. Это даёт идемпотентность
    # создания (повтор запроса не плодит заказы) и позволяет воспроизвести
    # сценарий "вебхук пришёл раньше заказа".
    order_id: str | None = Field(default=None, min_length=3, max_length=40,
                                 pattern=r"^[A-Za-z0-9_\-]+$")


class IssuanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    supplier: str
    request_id: str
    created_at: datetime


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sku: str
    amount: float
    amount_minor: int
    currency: str
    status: str
    delivery_attempts: int
    last_error: str | None = None
    created_at: datetime
    paid_at: datetime | None = None
    delivered_at: datetime | None = None
    issuance: IssuanceOut | None = None


class PaymentWebhook(BaseModel):
    """Контракт вебхука платёжной системы (из задания)."""

    event_id: str = Field(..., min_length=1, max_length=128)
    order_id: str = Field(..., min_length=1, max_length=40)
    status: str
    amount: float
    currency: str = Field(..., min_length=3, max_length=3)
    created_at: datetime


class WebhookAck(BaseModel):
    accepted: bool
    event_id: str
    order_id: str
    result: str
    order_status: str | None = None
    note: str | None = None


class ProductOut(BaseModel):
    sku: str
    name: str
    type: str
    price: float
    currency: str
    image: str | None
    available: int
    in_stock: bool


class StorefrontPage(BaseModel):
    items: list[ProductOut]
    next_cursor: str | None = None
    took_ms: float
