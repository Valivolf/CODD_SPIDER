#!/usr/bin/env bash
# Проверки ИИ после установки (подключается из install.sh архива lan-calendar-ai; требует selftest.sh — функции st_ok / st_bad / st_warn).
# st_selftest_ai ПРОЕКТ — контейнеры, готовность моделей, короткий ответ языковой модели.

st_selftest_ai() {
  local project="$1"
  echo
  echo "Проверки ИИ:"
  if st_container_running lan-calendar-llm; then st_ok "контейнер lan-calendar-llm (языковая модель) запущен"
  else st_bad "контейнер lan-calendar-llm не запущен" "sudo docker logs --tail 50 lan-calendar-llm ; затем: cd $project/docker && sudo docker compose --profile ai up -d --no-build llm"; fi
  if st_container_running lan-calendar-asr; then st_ok "контейнер lan-calendar-asr (распознавание речи) запущен"
  else st_bad "контейнер lan-calendar-asr не запущен" "sudo docker logs --tail 50 lan-calendar-asr ; затем: cd $project/docker && sudo docker compose --profile ai up -d --no-build asr"; fi
  if ! st_container_running lan-calendar; then
    st_bad "приложение (lan-calendar) не запущено — ИИ не с кем работать" "сначала установите/запустите приложение: sudo bash install.sh из папки lan-calendar-app"
    return
  fi
  local out
  out="$(docker exec -i lan-calendar python - 2>&1 <<'PY' || true
import asyncio, time
from app.services.ai import client

async def main():
    st = {}
    for _ in range(60):                       # модели загружаются в память до пары минут
        st = await client.status(force=True)
        if st["llm"].get("ok") and st["asr"].get("ok"):
            break
        await asyncio.sleep(5)
    print("LLM_OK" if st["llm"].get("ok") else "LLM_BAD " + str(st["llm"].get("message")), (st["llm"].get("model") or ""))
    print("ASR_OK" if st["asr"].get("ok") else "ASR_BAD " + str(st["asr"].get("message")), (st["asr"].get("model") or ""))
    if st["llm"].get("ok"):
        t = time.time()
        try:
            text = await client.llm_chat("Отвечай одним словом.", "Столица России?", max_tokens=8, priority=client.PRIORITY_BACKGROUND)
            print("LLM_ANSWER", round(time.time() - t, 1), text[:40])
        except Exception as e:
            print("LLM_ERR", type(e).__name__, str(e)[:120])

asyncio.run(main())
PY
)"
  case "$out" in
    *LLM_OK*) st_ok "языковая модель загружена ($(echo "$out" | sed -n 's/^LLM_OK //p' | head -1))" ;;
    *) st_bad "языковая модель не готова: $(echo "$out" | grep -m1 LLM_BAD)" "подождать 2–3 минуты (модель загружается в память) и повторить: sudo bash install.sh --check ; если не проходит — sudo docker logs --tail 80 lan-calendar-llm" ;;
  esac
  case "$out" in
    *ASR_OK*) st_ok "распознавание речи загружено ($(echo "$out" | sed -n 's/^ASR_OK //p' | head -1))" ;;
    *) st_bad "распознавание речи не готово: $(echo "$out" | grep -m1 ASR_BAD)" "подождать минуту и повторить: sudo bash install.sh --check ; если не проходит — sudo docker logs --tail 80 lan-calendar-asr" ;;
  esac
  case "$out" in
    *LLM_ANSWER*) st_ok "языковая модель отвечает ($(echo "$out" | grep -m1 LLM_ANSWER | cut -d' ' -f2) с на короткий ответ)" ;;
    *LLM_ERR*) st_bad "языковая модель не ответила на проверочный вопрос: $(echo "$out" | grep -m1 LLM_ERR)" "sudo docker logs --tail 80 lan-calendar-llm — прислать разработчику" ;;
  esac
  # сколько ядер использует модель: все ядра сервера (AI_THREADS, по умолчанию 6)
  local threads cores
  threads="$(docker inspect -f '{{range .Args}}{{.}} {{end}}' lan-calendar-llm 2>/dev/null | sed -n 's/.* -t \([0-9][0-9]*\) .*/\1/p' | head -1)"
  cores="$(nproc 2>/dev/null || echo 0)"
  if [ -n "$threads" ] && [ "${cores:-0}" -gt 0 ]; then
    if [ "$cores" -gt 4 ]; then want=$((cores - 2)); elif [ "$cores" -gt 2 ]; then want=$((cores - 1)); else want="$cores"; fi
    if [ "$threads" -gt "$want" ] && [ "$cores" -gt 2 ]; then
      st_warn "модель занимает $threads потоков из $cores ядер — остальным программам (приложение, робот Яндекса, бот) не остаётся процессора, и модель работает в 2–3 раза МЕДЛЕННЕЕ (замерено)" "в $project/docker/.env указать AI_THREADS=$want и выполнить: cd $project/docker && sudo docker compose --profile ai up -d --no-build --force-recreate llm asr"
    elif [ "$threads" -lt $((want - 1)) ]; then
      st_warn "модель использует только $threads потоков при $cores ядрах — часть процессора простаивает" "в $project/docker/.env указать AI_THREADS=$want и выполнить: cd $project/docker && sudo docker compose --profile ai up -d --no-build --force-recreate llm asr"
    else
      st_ok "потоки модели подобраны под процессор: $threads из $cores ядер (остальные — приложению, роботу и боту)"
    fi
  fi
  if ! grep -q -m1 -w avx2 /proc/cpuinfo 2>/dev/null; then
    st_warn "процессор не показывает расширение AVX2 — языковая модель будет работать в 3–5 раз медленнее" "если сервер — виртуальная машина: в её настройках выберите тип процессора «host» (Proxmox/KVM), отключите «режим совместимости процессора» (Hyper-V) или маскирование функций (EVC, VMware) и перезапустите машину"
  fi
  local mem
  mem="$(awk '/MemAvailable/ {printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 0)"
  if [ "${mem:-0}" -lt 2 ]; then
    st_warn "свободной оперативной памяти мало (≈${mem} ГБ) — модель может вытесняться и работать очень медленно" "освободить память или добавить её виртуальной машине"
  fi
  return 0
}
