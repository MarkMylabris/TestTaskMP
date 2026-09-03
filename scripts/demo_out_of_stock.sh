#!/usr/bin/env bash
# Пустой остаток: заказ оплачен, кода нет ни у A, ни у B.
# Ожидание: восстановимый статус out_of_stock, сервис жив, сверка это видит,
# после пополнения заказ доводится сам.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
API=${API:-http://127.0.0.1:8000}
SUP_A=${SUP_A:-http://127.0.0.1:9101}
SUP_B=${SUP_B:-http://127.0.0.1:9102}
SKU=${SKU:-SUB-SPOTIFY-1M}

echo "1. объявляем $SKU распроданным у обоих поставщиков"
curl -s -XPOST "$SUP_A/a/_control" -H 'content-type: application/json' \
     -d "{\"mode\":\"random\",\"out_of_stock_skus\":[\"$SKU\"]}" >/dev/null
curl -s -XPOST "$SUP_B/b/_control" -H 'content-type: application/json' \
     -d "{\"mode\":\"random\",\"out_of_stock_skus\":[\"$SKU\"]}" >/dev/null

OID=$(curl -s -XPOST "$API/orders" -H 'content-type: application/json' -d "{\"sku\":\"$SKU\"}" \
      | $PY -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "   заказ: $OID"
$PY -m scripts.payment_sim --api "$API" pay --order "$OID" >/dev/null

for i in $(seq 1 30); do
  ST=$(curl -s "$API/orders/$OID" | $PY -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  [ "$ST" = "out_of_stock" ] && break
  sleep 1
done
echo "2. статус: $(curl -s "$API/orders/$OID" | $PY -c 'import sys,json;print(json.load(sys.stdin)["status"])')  (восстановимый, сервис жив)"
curl -s "$API/health"; echo

echo "3. сверка видит «оплачен, но не выдан»:"
curl -s "$API/admin/reconciliation?grace_seconds=0" | $PY -c '
import sys, json
d = json.load(sys.stdin)
print("   paid_not_delivered:", d["paid_not_delivered"]["count"])
print("   журнал денег сходится:", d["ledger"]["balanced"])
'

echo "4. пополняем остаток"
curl -s -XPOST "$SUP_A/a/_control" -H 'content-type: application/json' -d '{"out_of_stock_skus":[]}' >/dev/null
curl -s -XPOST "$SUP_B/b/_control" -H 'content-type: application/json' -d '{"out_of_stock_skus":[]}' >/dev/null
curl -s -XPOST "$SUP_A/a/_restock" -H 'content-type: application/json' \
     -d "{\"sku\":\"$SKU\",\"count\":3}" >/dev/null

for i in $(seq 1 60); do
  ST=$(curl -s "$API/orders/$OID" | $PY -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  [ "$ST" = "delivered" ] && break
  sleep 1
done
echo "5. итог (без ручного вмешательства):"
curl -s "$API/orders/$OID" | $PY -c '
import sys, json
d = json.load(sys.stdin)
print("   статус:", d["status"], "| код:", (d["issuance"] or {}).get("code"),
      "| попыток выдачи:", d["delivery_attempts"])
'
