#!/usr/bin/env bash
# Собирает архивы для установки на сервер БЕЗ интернета. Запускать там, где интернет есть
# (например, на ВМ 192.168.1.143):
#     sudo bash /opt/lan_calendar/scripts/offline/make_bundle.sh [app|robot|all] [куда_положить]
#   app    — lan-calendar-app-<дата-время>.tar : приложение (образ + файлы из папки docker/). Небольшой,
#            возится при каждом обновлении приложения. Имя содержит время сборки — два архива за один
#            день не перепутать (раньше одинаковое имя позволяло поставить старую сборку вместо новой).
#   robot  — yandex-robot-<дата>.tar       : робот карт (образ с браузером). Большой, возится ОДИН раз
#            (и только если менялся сам робот, папка robot/).
#   ai     — lan-calendar-ai-<дата-время>.tar : ИИ (образы распознавания речи и языковой модели + сами модели из папки ai-models,
#            ~7 ГБ). Возится один раз (и при смене модели); приложение без него работает как раньше.
#   all    — приложение и робот (по умолчанию; ИИ — только явно: make_bundle.sh ai)
set -euo pipefail
cd "$(dirname "$0")/../.."
WHAT="${1:-all}"
OUT="${2:-$PWD/offline-out}"
STAMP="$(date +%Y%m%d)"
APP_STAMP="$(date +%Y%m%d-%H%M)"        # метка сборки приложения: в имени архива и в файле BUILD
ROBOT_VERSION="$(date +%Y%m%d%H%M%S)"   # версия кода робота: установщик не затирает более новый код старым
mkdir -p "$OUT"

make_app() {
  local B="$OUT/lan-calendar-app"
  rm -rf "$B"; mkdir -p "$B/docker" "$B/robot"
  echo "[приложение 1/3] собираю образ lan-calendar:latest…"
  docker build -q -f docker/Dockerfile -t lan-calendar:latest .
  echo "[приложение 2/3] сохраняю образ…"
  docker save lan-calendar:latest | gzip -3 > "$B/lan-calendar.tar.gz"
  echo "[приложение 3/3] собираю архив…"
  cp docker/Dockerfile docker/Dockerfile.caddy docker/Caddyfile docker/caddy-entrypoint.sh docker/docker-compose.yml "$B/docker/"
  # Код робота (он подключается к контейнеру из папки проекта) — обновляется вместе с приложением.
  cp robot/Dockerfile robot/requirements.txt robot/server.py robot/constructor.py robot/forms.py robot/pagewait.py robot/README.md "$B/robot/"
  echo "$ROBOT_VERSION" > "$B/robot/VERSION"
  cp scripts/offline/install_app.sh "$B/install.sh"
  cp scripts/offline/selftest.sh "$B/selftest.sh"
  cp scripts/offline/README_app.txt "$B/README.txt"
  # Метка сборки и хэши кода, из которого собран образ: установщик сверяет их с тем, что реально работает в
  # контейнере, и сам говорит, если на сервере остался старый код (а не молча «обновляет» старым).
  echo "$APP_STAMP" > "$B/BUILD"
  find app bot static templates -type f ! -name '*.pyc' ! -path '*/__pycache__/*' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > "$B/CODE.sha256"
  (cd "$B" && sha256sum lan-calendar.tar.gz docker/* robot/* selftest.sh BUILD CODE.sha256 > SHA256SUMS)
  tar -C "$OUT" -cf "$OUT/lan-calendar-app-$APP_STAMP.tar" lan-calendar-app
  ls -lh "$OUT/lan-calendar-app-$APP_STAMP.tar"
}

make_robot() {
  local B="$OUT/yandex-robot"
  rm -rf "$B"; mkdir -p "$B/robot"
  echo "[робот 1/3] собираю образ lan-calendar-robot:latest (скачивает браузер, если образа ещё нет)…"
  docker build -q -t lan-calendar-robot:latest robot
  echo "[робот 2/3] сохраняю образ…"
  docker save lan-calendar-robot:latest | gzip -3 > "$B/yandex-robot.tar.gz"
  echo "[робот 3/3] собираю архив…"
  cp robot/Dockerfile robot/requirements.txt robot/server.py robot/constructor.py robot/forms.py robot/pagewait.py robot/README.md "$B/robot/"
  echo "$ROBOT_VERSION" > "$B/robot/VERSION"
  cp docker/docker-compose.yml "$B/docker-compose.yml"
  cp scripts/offline/install_robot.sh "$B/install.sh"
  cp scripts/offline/selftest.sh "$B/selftest.sh"
  cp scripts/offline/README_robot.txt "$B/README.txt"
  (cd "$B" && sha256sum yandex-robot.tar.gz docker-compose.yml selftest.sh > SHA256SUMS)
  tar -C "$OUT" -cf "$OUT/yandex-robot-$STAMP.tar" yandex-robot
  ls -lh "$OUT/yandex-robot-$STAMP.tar"
}

make_ai() {
  local B="$OUT/lan-calendar-ai" AI_STAMP="$(date +%Y%m%d-%H%M)"
  ls ai-models/llm/*.gguf >/dev/null 2>&1 || { echo "Нет модели языка: положите файл .gguf в ai-models/llm"; exit 1; }
  [ -f ai-models/whisper-large-v3-turbo/model.bin ] || { echo "Нет модели речи: ai-models/whisper-large-v3-turbo/model.bin"; exit 1; }
  rm -rf "$B"; mkdir -p "$B/docker/asr"
  echo "[ИИ 1/4] собираю образ распознавания речи lan-calendar-asr:latest (нужен интернет для pip)…"
  docker build -q -t lan-calendar-asr:latest docker/asr
  if ! docker image inspect lan-calendar-llm:latest >/dev/null 2>&1; then
    echo "      образ языковой модели: скачиваю ghcr.io/ggml-org/llama.cpp:server"
    docker pull -q ghcr.io/ggml-org/llama.cpp:server
    docker tag ghcr.io/ggml-org/llama.cpp:server lan-calendar-llm:latest
  fi
  echo "[ИИ 2/4] сохраняю образы…"
  docker save lan-calendar-asr:latest | gzip -3 > "$B/lan-calendar-asr.tar.gz"
  docker save lan-calendar-llm:latest | gzip -3 > "$B/lan-calendar-llm.tar.gz"
  echo "[ИИ 3/4] считаю контрольные суммы (модели большие — минута-две)…"
  cp docker/docker-compose.yml "$B/docker/"
  cp docker/asr/Dockerfile docker/asr/asr_server.py "$B/docker/asr/"
  cp scripts/offline/install_ai.sh "$B/install.sh"
  cp scripts/offline/selftest.sh scripts/offline/selftest_ai.sh "$B/"
  cp scripts/offline/README_ai.txt "$B/README.txt"
  echo "$AI_STAMP" > "$B/BUILD"
  (cd "$B" && sha256sum lan-calendar-asr.tar.gz lan-calendar-llm.tar.gz docker/docker-compose.yml docker/asr/* selftest.sh selftest_ai.sh install.sh BUILD > SHA256SUMS)
  (cd ai-models && find . -type f | LC_ALL=C sort | sed 's#^\./##' | while read -r f; do sha256sum "$f" | awk -v f="$f" '{print $1 "  models/" f}'; done) >> "$B/SHA256SUMS"
  echo "[ИИ 4/4] собираю архив…"
  tar -cf "$OUT/lan-calendar-ai-$AI_STAMP.tar" --transform 's,^ai-models,lan-calendar-ai/models,' -C "$OUT" lan-calendar-ai -C "$PWD" ai-models
  rm -rf "$B"
  ls -lh "$OUT/lan-calendar-ai-$AI_STAMP.tar"
}

case "$WHAT" in
  ai) make_ai ;;
  app) make_app ;;
  robot) make_robot ;;
  all) make_app; make_robot ;;
  *) echo "Использование: make_bundle.sh [app|robot|ai|all] [папка]"; exit 1 ;;
esac
echo "Готово: $OUT"
