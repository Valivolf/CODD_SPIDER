#!/usr/bin/env bash
# Установка ИИ на сервер БЕЗ интернета: распознавание речи (faster-whisper) и языковая модель (llama.cpp + Qwen2.5-3B) — всё работает на самом
# сервере. Сначала должно быть установлено обновление приложения (lan-calendar-app-….tar).
#     tar xf lan-calendar-ai-*.tar && cd lan-calendar-ai && sudo bash install.sh [папка_проекта]
# Проверить без установки:  sudo bash install.sh --check
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CHECK_ONLY=""
if [ "${1:-}" = "--check" ]; then CHECK_ONLY=1; shift; fi
PROJECT="${1:-/opt/lan_calendar}"
STAMP="$(date +%Y%m%d-%H%M%S)"
# shellcheck source=selftest.sh
. "$HERE/selftest.sh"
# shellcheck source=selftest_ai.sh
. "$HERE/selftest_ai.sh"

if [ ! -f "$PROJECT/docker/docker-compose.yml" ]; then
  echo "Не найдена папка проекта: $PROJECT (там должен быть docker/docker-compose.yml)."
  echo "Если проект лежит в другом месте: sudo bash install.sh /путь/к/проекту"
  exit 1
fi
docker info >/dev/null 2>&1 || { echo "Нет доступа к Docker. Запустите с правами: sudo bash install.sh"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Не найден «docker compose» (нужен Compose v2)."; exit 1; }

if [ -n "$CHECK_ONLY" ]; then
  st_selftest_ai "$PROJECT"
  st_report
  exit $?
fi

# Места должно хватить на модели (они переезжают в папку проекта) и образы.
need_gb=$(( $(du -sm "$HERE/models" 2>/dev/null | cut -f1) / 1024 + 4 ))
free_gb=$(df -Pm "$PROJECT" | awk 'NR==2 {print int($4/1024)}')
if [ "$free_gb" -lt "$need_gb" ]; then
  echo "Мало места на диске проекта: свободно ≈${free_gb} ГБ, нужно ≈${need_gb} ГБ. Освободите место (или расширьте диск: extend_disk.sh) и повторите."
  exit 1
fi

echo "[1/5] Проверяю целостность файлов (модели большие — до нескольких минут)…"
(cd "$HERE" && sha256sum -c SHA256SUMS)

echo "[2/5] Загружаю образы в Docker…"
gunzip -c "$HERE/lan-calendar-llm.tar.gz" | docker load
gunzip -c "$HERE/lan-calendar-asr.tar.gz" | docker load

echo "[3/5] Кладу модели и файлы в проект…"
mkdir -p "$PROJECT/ai-models" "$PROJECT/docker/asr"
for d in "$HERE"/models/*; do
  name="$(basename "$d")"
  if [ -e "$PROJECT/ai-models/$name" ]; then
    echo "      $name уже есть — заменяю"
    rm -rf "$PROJECT/ai-models/$name"
  fi
  mv "$d" "$PROJECT/ai-models/$name"
done
cp "$HERE"/docker/asr/* "$PROJECT/docker/asr/"
if ! grep -q 'lan-calendar-llm' "$PROJECT/docker/docker-compose.yml"; then
  echo "      в docker-compose.yml нет сервисов ИИ — ставлю compose из архива (старый сохранён как .bak-$STAMP)"
  cp "$PROJECT/docker/docker-compose.yml" "$PROJECT/docker/docker-compose.yml.bak-$STAMP"
  cp "$HERE/docker/docker-compose.yml" "$PROJECT/docker/docker-compose.yml"
fi

# Потоки модели: ядра сервера минус 2 (6 ядер → 4). Если отдать модели ВСЕ ядра, её потоки ждут друг друга, пока приложение, робот Яндекса и бот тоже берут процессор, —
# замерено на рабочем сервере: в 2–3 раза медленнее. Своё число — строка AI_THREADS=… в docker/.env (установщик её не трогает).
cores="$(nproc 2>/dev/null || echo 4)"
if [ "$cores" -gt 4 ]; then threads=$((cores - 2)); elif [ "$cores" -gt 2 ]; then threads=$((cores - 1)); else threads="$cores"; fi
if [ ! -f "$PROJECT/docker/.env" ] || ! grep -q '^AI_THREADS=' "$PROJECT/docker/.env"; then
  echo "AI_THREADS=$threads" >> "$PROJECT/docker/.env"
  echo "      потоков модели: $threads (ядер на сервере: $cores) — записано в $PROJECT/docker/.env"
fi
echo "[4/5] Запускаю ИИ…"
cd "$PROJECT/docker"
docker compose --profile ai up -d --no-build --force-recreate llm asr

echo "[5/5] Жду загрузки моделей в память (до нескольких минут)…"
for i in $(seq 1 90); do
  l="$(docker exec lan-calendar python -c "import urllib.request;print(urllib.request.urlopen('http://llm:8080/health',timeout=5).status)" 2>/dev/null || true)"
  a="$(docker exec lan-calendar python -c "import urllib.request;print(urllib.request.urlopen('http://asr:8000/health',timeout=5).status)" 2>/dev/null || true)"
  if [ "$l" = "200" ] && [ "$a" = "200" ]; then break; fi
  sleep 5
done
echo
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'NAMES|lan-calendar' || true

RESULT=0
st_selftest_ai "$PROJECT"
st_report || RESULT=1
if [ "$RESULT" = "0" ]; then
  cat <<EOT

Дальше (администратор календаря, в браузере, один раз):
  Настройки -> «🤖 ИИ: голос и помощник…» -> включить «ИИ в боте ВК» и/или «Помощник в веб-панели»; «Проверить модель на примере» покажет скорость.
Выключить совсем: cd $PROJECT/docker && docker compose --profile ai rm -sf llm asr   (модели остаются в $PROJECT/ai-models)
EOT
fi
exit "$RESULT"
