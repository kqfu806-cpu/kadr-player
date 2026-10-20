#!/bin/bash
# Проверки на реальных процессах: двойной запуск, частота запросов, автозакрытие.
#   bash tests/manual/test_overlay_processes.sh
cd "$(dirname "$0")/../.." || exit 1
PY=.venv/bin/python
export LD_LIBRARY_PATH=/tmp QT_QPA_PLATFORM=vnc
rm -f cache/overlay.lock

echo "=== поднимаем сервер ==="
setsid $PY -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 >/tmp/uv_manual.log 2>&1 &
SRV=$!
sleep 8
curl -s -o /dev/null -w "  сервер: HTTP %{http_code}\n" http://127.0.0.1:8000/api/health

echo "=== A. защита от двойного запуска ==="
setsid $PY overlay.py >/tmp/ov_m1.log 2>&1 &
P1=$!
sleep 4
timeout 20 $PY overlay.py >/tmp/ov_m2.log 2>&1
CODE2=$?
echo "  код выхода второго экземпляра: $CODE2"
sed 's/^/    /' /tmp/ov_m2.log | head -2
if [ "$CODE2" -eq 0 ] && grep -qi "уже запущен" /tmp/ov_m2.log; then
  echo "  [OK ] второй экземпляр отказался работать (exit 0, сообщение есть)"
else
  echo "  [FAIL] второй экземпляр НЕ отказался"
fi
if kill -0 $P1 2>/dev/null; then
  echo "  [OK ] первый экземпляр продолжает работать"
else
  echo "  [FAIL] первый экземпляр умер"
fi

echo
echo "=== B. частота запросов (3 c) ==="
CNT1=$(grep -c "GET /api/now-playing" /tmp/uv_manual.log)
sleep 3
CNT2=$(grep -c "GET /api/now-playing" /tmp/uv_manual.log)
echo "  запросов /api/now-playing за 3 c: $((CNT2-CNT1)) (норма ≈6, спам был бы сотни)"

echo
echo "=== C. автозакрытие после остановки сервера ==="
T0=$(date +%s%N)
kill $SRV 2>/dev/null
pkill -f '[u]vicorn backend.main' 2>/dev/null
for i in $(seq 1 80); do
  if ! kill -0 $P1 2>/dev/null; then break; fi
  sleep 0.1
done
T1=$(date +%s%N)
MS=$(( (T1 - T0) / 1000000 ))
if kill -0 $P1 2>/dev/null; then
  echo "  [FAIL] оверлей НЕ закрылся за 8 секунд"
  kill -9 $P1 2>/dev/null
else
  echo "  [OK ] оверлей закрылся через ${MS} мс после остановки сервера (ожидаем ~2500)"
fi
echo "  лог оверлея:"; tail -2 /tmp/ov_m1.log | sed 's/^/    /'
pkill -f '[o]verlay.py' 2>/dev/null
exit 0
