#!/usr/bin/env bash
# Ловушка таймаута: поставщик A выдаёт код и «зависает».
# Ожидаемое поведение: fallback на B ЗАПРЕЩЁН, повтор идёт с тем же request_id,
# клиент получает ровно один код - тот самый, который A зафиксировал.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
API=${API:-http://127.0.0.1:8000}
SUP_A=${SUP_A:-http://127.0.0.1:9101}
SUP_B=${SUP_B:-http://127.0.0.1:9102}
SKU=${SKU:-KEY-EFT}

jqp() { $PY -c "import sys,json;d=json.load(sys.stdin);print(json.dumps(d,ensure_ascii=False,indent=2))"; }

echo "1. A: режим timeout_after_issue (выдаёт код, ответ не доходит)"
curl -s -XPOST "$SUP_A/a/_control" -H 'content-type: application/json' \
     -d '{"mode":"timeout_after_issue","hang_seconds":4}' >/dev/null
curl -s -XPOST "$SUP_B/b/_control" -H 'content-type: application/json' -d '{"mode":"ok"}' >/dev/null
curl -s -XPOST "$SUP_A/a/_restock" -H 'content-type: application/json' \
     -d "{\"sku\":\"$SKU\",\"count\":3}" >/dev/null

OID=$(curl -s -XPOST "$API/orders" -H 'content-type: application/json' -d "{\"sku\":\"$SKU\"}" \
      | $PY -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "   заказ: $OID"

echo "2. оплата"
$PY -m scripts.payment_sim --api "$API" pay --order "$OID" >/dev/null

echo "3. ждём, пока система разберётся с неизвестным исходом..."
for i in $(seq 1 60); do
  ST=$(curl -s "$API/orders/$OID" | $PY -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  [ "$ST" = "delivered" ] && break
  sleep 1
done

echo
echo "=== история заказа ==="
curl -s "$API/admin/orders/$OID/timeline" | $PY -c '
import sys, json
d = json.load(sys.stdin)
print("статус:", d["order"]["status"])
print("выдача:", d["issuance"]["code"] if d["issuance"] else None,
      "от поставщика", d["issuance"]["supplier"] if d["issuance"] else None)
print("попытки к поставщикам:")
for a in d["supplier_attempts"]:
    print(f'"'"'  {a["supplier"]} #{a["attempt_no"]:<2} {a["state"]:<10} {a["request_id"]}  {a["reason"] or ""}'"'"')
rid = {a["request_id"] for a in d["supplier_attempts"]}
print("уникальных request_id:", len(rid), "->", sorted(rid))
'
echo
echo "=== что в этот момент лежит у поставщиков ==="
echo -n "A: "; curl -s "$SUP_A/a/issue/req_$OID-a"; echo
echo -n "B: "; curl -s "$SUP_B/b/issue/req_$OID-b"; echo
echo
echo "Ожидание: у A - один код (он же у клиента), у B - 404 (его вообще не звали)."
curl -s -XPOST "$SUP_A/a/_control" -H 'content-type: application/json' -d '{"mode":"ok"}' >/dev/null
