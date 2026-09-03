#!/usr/bin/env bash
# Поставщик A недоступен (процесс убит) -> connection refused -> это ТОЧНЫЙ отказ,
# значит переключение на B безопасно. Товар выдаётся ровно один раз.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
API=${API:-http://127.0.0.1:8000}
SUP_A=${SUP_A:-http://127.0.0.1:9101}
SUP_B=${SUP_B:-http://127.0.0.1:9102}
SKU=${SKU:-KEY-GTA5}

curl -s -XPOST "$SUP_B/b/_control" -H 'content-type: application/json' -d '{"mode":"ok"}' >/dev/null
curl -s -XPOST "$SUP_B/b/_restock" -H 'content-type: application/json' \
     -d "{\"sku\":\"$SKU\",\"count\":3}" >/dev/null

echo "1. гасим поставщика A"
if [ -f .run/sup_a.pid ]; then kill -9 "$(cat .run/sup_a.pid)" 2>/dev/null || true; sleep 1; fi

OID=$(curl -s -XPOST "$API/orders" -H 'content-type: application/json' -d "{\"sku\":\"$SKU\"}" \
      | $PY -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "   заказ: $OID"

echo "2. оплата"
$PY -m scripts.payment_sim --api "$API" pay --order "$OID" >/dev/null

for i in $(seq 1 40); do
  ST=$(curl -s "$API/orders/$OID" | $PY -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  [ "$ST" = "delivered" ] && break
  sleep 1
done

echo
curl -s "$API/admin/orders/$OID/timeline" | $PY -c '
import sys, json
d = json.load(sys.stdin)
print("статус:", d["order"]["status"])
i = d["issuance"]
print("выдача:", (i or {}).get("code"), "от поставщика", (i or {}).get("supplier"))
for a in d["supplier_attempts"]:
    print(f'"'"'  {a["supplier"]} #{a["attempt_no"]:<2} {a["state"]:<10} {a["reason"] or ""}'"'"')
'
echo
echo "3. поднимаем A обратно"
nohup $PY -m uvicorn suppliers.main:app --host 127.0.0.1 --port 9101 --log-level warning \
     > .run/sup_a.log 2>&1 & echo $! > .run/sup_a.pid
sleep 3; curl -s "$SUP_A/health"; echo
