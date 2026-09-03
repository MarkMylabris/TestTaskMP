"""Клиент к поставщикам: таймауты, ретраи с бэкоффом, фолбэк A -> B.

Главная идея всего этапа 3: исходов у запроса не два, а три.

Обычно HTTP-вызов делят на "получилось" и "не получилось". Для выдачи товара
этого мало. Read timeout - это не отказ, это отсутствие информации: запрос ушёл,
поставщик мог его обработать и выдать код, а ответ потерялся по дороге. Если
считать такое отказом и уйти к резервному поставщику, клиент получит два кода.

Отсюда:
  ok               код получен
  definite_failure точно НЕ выдавал (соединение отвергнуто, либо он сам сказал
                   ошибку по этому request_id)
  unknown          read timeout, исход неизвестен

Переключаться на следующего поставщика можно только из definite_failure.
Из unknown - нельзя, пока не выясним, что там на самом деле произошло.
Выясняем статус-запросом GET /{s}/issue/{request_id}: 404 значит "такого
запроса не было" и переводит исход в definite_failure, 200 отдаёт тот самый код.

request_id при этом обязан быть стабильным для пары (заказ, поставщик), иначе
идемпотентность на стороне поставщика ничего не даёт.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import httpx
from sqlalchemy import text

from app.config import settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import SupplierAttempt

log = get_logger("supplier")

Kind = Literal["ok", "definite_failure", "unknown"]

SUPPLIER_URLS = {"a": settings.supplier_a_url, "b": settings.supplier_b_url}
SUPPLIER_ORDER = ("a", "b")


@dataclass(slots=True)
class Outcome:
    kind: Kind
    supplier: str
    request_id: str
    code: str | None = None
    reason: str | None = None
    http_status: int | None = None


@dataclass(slots=True)
class DeliveryOutcome:
    kind: Literal["ok", "out_of_stock", "failed", "unknown"]
    code: str | None = None
    supplier: str | None = None
    request_id: str | None = None
    reason: str | None = None


def request_id_for(order_id: str, supplier: str) -> str:
    """Детерминированный request_id.

    Не UUID: он обязан пережить рестарт процесса и совпасть при повторе,
    иначе идемпотентность поставщика бесполезна.
    """
    return f"req_{order_id}-{supplier}"


async def _record_attempt_start(request_id: str, order_id: str, supplier: str, attempt_no: int):
    """Журнал намерения: пишем ДО запроса и коммитим сразу.

    Если процесс умрёт в момент HTTP-вызова, останется строка `in_flight`,
    и восстановление будет знать, что исход неизвестен.
    """
    async with session_scope() as s:
        s.add(
            SupplierAttempt(
                request_id=request_id,
                order_id=order_id,
                supplier=supplier,
                attempt_no=attempt_no,
                state="in_flight",
            )
        )


async def _record_attempt_end(
    request_id: str,
    attempt_no: int,
    state: str,
    *,
    code: str | None = None,
    reason: str | None = None,
    http_status: int | None = None,
    latency_ms: int | None = None,
):
    async with session_scope() as s:
        await s.execute(
            text(
                """
                UPDATE supplier_attempts
                   SET state=:st, code=:code, reason=:reason, http_status=:hs,
                       latency_ms=:lat, finished_at=now()
                 WHERE request_id=:rid AND attempt_no=:no
                """
            ),
            {
                "st": state, "code": code, "reason": reason, "hs": http_status,
                "lat": latency_ms, "rid": request_id, "no": attempt_no,
            },
        )


class SupplierClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._owned = client is None

    async def __aenter__(self):
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=settings.supplier_connect_timeout,
                    read=settings.supplier_read_timeout,
                    write=settings.supplier_read_timeout,
                    pool=settings.supplier_read_timeout,
                )
            )
        return self

    async def __aexit__(self, *exc):
        if self._owned and self._client is not None:
            await self._client.aclose()

    # --------------------------------------------------------------- #
    async def _call_issue(
        self, supplier: str, order_id: str, sku: str, request_id: str, attempt_no: int
    ) -> Outcome:
        url = f"{SUPPLIER_URLS[supplier]}/{supplier}/issue"
        await _record_attempt_start(request_id, order_id, supplier, attempt_no)
        started = datetime.now(timezone.utc)

        def elapsed_ms() -> int:
            return int((datetime.now(timezone.utc) - started).total_seconds() * 1000)

        try:
            r = await self._client.post(
                url, json={"request_id": request_id, "sku": sku, "order_id": order_id}
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Соединение не установлено -> запрос не дошёл -> код точно не выдан.
            await _record_attempt_end(
                request_id, attempt_no, "failed", reason=f"connect: {exc!r}",
                latency_ms=elapsed_ms(),
            )
            return Outcome("definite_failure", supplier, request_id, reason="unreachable")
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
                httpx.RemoteProtocolError) as exc:
            # ТАЙМАУТ != ОТКАЗ. Поставщик мог успеть выдать код.
            await _record_attempt_end(
                request_id, attempt_no, "unknown", reason=f"timeout: {exc!r}",
                latency_ms=elapsed_ms(),
            )
            log.warning(
                "supplier.timeout", supplier=supplier, order_id=order_id,
                request_id=request_id, attempt=attempt_no,
                note="outcome unknown, code may have been issued",
            )
            return Outcome("unknown", supplier, request_id, reason="timeout")

        latency = elapsed_ms()
        if r.status_code == 200:
            body = r.json()
            code = body.get("code")
            await _record_attempt_end(
                request_id, attempt_no, "ok", code=code, http_status=200, latency_ms=latency
            )
            return Outcome("ok", supplier, request_id, code=code, http_status=200)

        reason = _reason_of(r)
        if r.status_code == 504:
            # Шлюзовой таймаут: исход у поставщика неизвестен.
            await _record_attempt_end(
                request_id, attempt_no, "unknown", reason=reason,
                http_status=r.status_code, latency_ms=latency,
            )
            return Outcome("unknown", supplier, request_id, reason=reason, http_status=504)

        # Явный ответ об ошибке. Поставщик ответил по этому request_id,
        # значит он зафиксировал исход и второй раз код не выдаст.
        await _record_attempt_end(
            request_id, attempt_no, "failed", reason=reason,
            http_status=r.status_code, latency_ms=latency,
        )
        return Outcome(
            "definite_failure", supplier, request_id, reason=reason, http_status=r.status_code
        )

    async def probe(self, supplier: str, request_id: str) -> Outcome:
        """Разрешить неизвестный исход: выдавал ли поставщик код по request_id."""
        url = f"{SUPPLIER_URLS[supplier]}/{supplier}/issue/{request_id}"
        try:
            r = await self._client.get(url)
        except httpx.HTTPError as exc:
            return Outcome("unknown", supplier, request_id, reason=f"probe failed: {exc!r}")
        if r.status_code == 200:
            return Outcome("ok", supplier, request_id, code=r.json().get("code"), http_status=200)
        if r.status_code == 404:
            # Поставщик не знает такого request_id -> он ничего не выдавал.
            return Outcome(
                "definite_failure", supplier, request_id, reason="never_issued", http_status=404
            )
        return Outcome("unknown", supplier, request_id, reason=_reason_of(r))

    async def _try_supplier(self, supplier: str, order_id: str, sku: str) -> Outcome:
        """Повторы с бэкоффом к одному поставщику под одним request_id."""
        request_id = request_id_for(order_id, supplier)
        last = Outcome("definite_failure", supplier, request_id, reason="no attempts")
        base_attempt = await _next_attempt_no(request_id)

        for i in range(settings.supplier_max_attempts):
            attempt_no = base_attempt + i
            last = await self._call_issue(supplier, order_id, sku, request_id, attempt_no)
            if last.kind == "ok":
                return last
            if last.kind == "definite_failure" and last.reason == "out_of_stock":
                return last  # ретраить бессмысленно, нужен другой поставщик
            if i < settings.supplier_max_attempts - 1:
                delay = min(
                    settings.supplier_backoff_max,
                    settings.supplier_backoff_base * (2 ** i),
                )
                await asyncio.sleep(delay * (0.5 + random.random()))  # jitter

        if last.kind == "unknown":
            # Не уходим к следующему поставщику, пока не выясним исход.
            resolved = await self.probe(supplier, request_id)
            log.info(
                "supplier.probe", supplier=supplier, order_id=order_id,
                request_id=request_id, resolved=resolved.kind, reason=resolved.reason,
            )
            if resolved.kind == "ok":
                await _record_attempt_end(
                    request_id, base_attempt + settings.supplier_max_attempts - 1,
                    "ok", code=resolved.code, reason="resolved by probe",
                )
            return resolved
        return last

    # --------------------------------------------------------------- #
    async def acquire_code(self, order_id: str, sku: str) -> DeliveryOutcome:
        """Получить код: A, при ТОЧНОМ отказе - B. Ровно один код на заказ."""
        # Сначала проверяем, не висит ли уже выданный код у кого-то из поставщиков.
        settled = await self._settle_known(order_id)
        if settled is not None:
            return settled

        last_reason = None
        for supplier in SUPPLIER_ORDER:
            outcome = await self._try_supplier(supplier, order_id, sku)
            if outcome.kind == "ok":
                return DeliveryOutcome(
                    "ok", code=outcome.code, supplier=supplier, request_id=outcome.request_id
                )
            if outcome.kind == "unknown":
                # Исход у этого поставщика не выяснен -> fallback запрещён.
                log.warning(
                    "supplier.unresolved", supplier=supplier, order_id=order_id,
                    request_id=outcome.request_id,
                    note="fallback blocked to avoid double issuance",
                )
                return DeliveryOutcome(
                    "unknown", supplier=supplier, request_id=outcome.request_id,
                    reason=outcome.reason,
                )
            last_reason = outcome.reason
            log.info(
                "supplier.failover", supplier=supplier, order_id=order_id,
                reason=outcome.reason, next=("b" if supplier == "a" else None),
            )

        if last_reason == "out_of_stock":
            return DeliveryOutcome("out_of_stock", reason=last_reason)
        return DeliveryOutcome("failed", reason=last_reason or "all suppliers failed")

    async def _settle_known(self, order_id: str) -> DeliveryOutcome | None:
        """Если по заказу уже есть незакрытая попытка - сначала выясняем её исход.

        Это путь восстановления после падения процесса: `in_flight`/`unknown`
        строки означают "возможно, код уже выдан".
        """
        async with session_scope() as s:
            rows = (
                await s.execute(
                    text(
                        """
                        SELECT DISTINCT supplier, request_id
                          FROM supplier_attempts
                         WHERE order_id = :oid AND state IN ('in_flight','unknown','ok')
                        """
                    ),
                    {"oid": order_id},
                )
            ).all()
        for supplier, request_id in rows:
            resolved = await self.probe(supplier, request_id)
            if resolved.kind == "ok":
                log.info(
                    "supplier.recovered_code", supplier=supplier, order_id=order_id,
                    request_id=request_id,
                )
                await _record_attempt_end(
                    request_id, await _last_attempt_no(request_id), "ok",
                    code=resolved.code, reason="resolved before retry",
                )
                return DeliveryOutcome(
                    "ok", code=resolved.code, supplier=supplier, request_id=request_id
                )
            if resolved.kind == "unknown":
                return DeliveryOutcome(
                    "unknown", supplier=supplier, request_id=request_id, reason=resolved.reason
                )
            # definite_failure -> идём дальше по обычному сценарию
        return None


def _reason_of(r: httpx.Response) -> str:
    try:
        body = r.json()
    except Exception:
        return f"http_{r.status_code}"
    detail = body.get("detail", body)
    if isinstance(detail, dict):
        return detail.get("reason") or f"http_{r.status_code}"
    return str(detail)[:200]


async def _next_attempt_no(request_id: str) -> int:
    async with session_scope() as s:
        n = await s.scalar(
            text("SELECT COALESCE(MAX(attempt_no),0) FROM supplier_attempts WHERE request_id=:r"),
            {"r": request_id},
        )
    return int(n or 0) + 1


async def _last_attempt_no(request_id: str) -> int:
    async with session_scope() as s:
        n = await s.scalar(
            text("SELECT COALESCE(MAX(attempt_no),0) FROM supplier_attempts WHERE request_id=:r"),
            {"r": request_id},
        )
    return int(n or 1)
