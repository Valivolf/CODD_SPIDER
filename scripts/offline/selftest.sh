#!/usr/bin/env bash
# Самопроверка после установки (подключается из install.sh приложения и робота; сам по себе не запускается).
# Каждая проверка печатает [ OK ] / [FAIL] / [WARN]; в конце — ИТОГ: «ВСЁ ГОТОВО» либо список «что нужно сделать».
#   [FAIL] — установка не удалась или что-то не работает: надо исправить;
#   [WARN] — приложение работает, но есть то, что стоит поправить (например, закрыт доступ к Яндексу).
# Повторить проверку без установки:  sudo bash install.sh --check

ST_FAIL_TEXT=(); ST_FAIL_FIX=(); ST_WARN_TEXT=(); ST_WARN_FIX=()

st_ok()   { echo "  [ OK ] $1"; }
st_bad()  { echo "  [FAIL] $1"; ST_FAIL_TEXT+=("$1"); ST_FAIL_FIX+=("$2"); }
st_warn() { echo "  [WARN] $1"; ST_WARN_TEXT+=("$1"); ST_WARN_FIX+=("$2"); }

st_http() {   # st_http URL -> код ответа (000 — не отвечает)
  curl -sk -o /dev/null -w '%{http_code}' --max-time 15 "$1" 2>/dev/null || true
}

st_container_running() {   # st_container_running ИМЯ -> 0, если контейнер запущен
  [ "$(docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || echo missing)" = "running" ]
}

# Сколько записей в основных таблицах — из РАБОТАЮЩЕГО контейнера приложения (до и после обновления).
st_snapshot_counts() {
  docker exec -i lan-calendar python - 2>/dev/null <<'PY' || true
import sqlite3
c = sqlite3.connect("/app/data/calendar.db")
try:
    from app.core import field_crypto
    field_crypto.register_sqlite(c)
except Exception:
    pass
def n(t):
    try:
        return c.execute(f"select count(*) from {t}").fetchone()[0]
    except Exception:
        return -1
print(" ".join(f"{t}={n(t)}" for t in ("users", "people", "points", "crews", "tasks", "task_visit_reports", "equipment")))
PY
}

st_report() {   # печатает итог, возвращает 0 (всё готово) или 1 (есть проблемы)
  echo
  echo "=================================================================="
  if [ "${#ST_FAIL_TEXT[@]}" -eq 0 ]; then
    echo " ИТОГ: ВСЁ ГОТОВО — установка прошла, проверки пройдены."
    if [ "${#ST_WARN_TEXT[@]}" -eq 0 ]; then
      echo " Больше на сервере ничего делать не нужно."
    else
      echo " Приложение работает. Есть замечания (не мешают работе), что стоит сделать:"
      local i
      for i in "${!ST_WARN_TEXT[@]}"; do
        echo "   $((i + 1))) ${ST_WARN_TEXT[$i]}"
        echo "      -> ${ST_WARN_FIX[$i]}"
      done
    fi
    echo "=================================================================="
    return 0
  fi
  echo " ИТОГ: ЕСТЬ ПРОБЛЕМЫ (${#ST_FAIL_TEXT[@]}). Что нужно сделать:"
  local i
  for i in "${!ST_FAIL_TEXT[@]}"; do
    echo "   $((i + 1))) ${ST_FAIL_TEXT[$i]}"
    echo "      -> ${ST_FAIL_FIX[$i]}"
  done
  if [ "${#ST_WARN_TEXT[@]}" -gt 0 ]; then
    echo " Замечания:"
    for i in "${!ST_WARN_TEXT[@]}"; do
      echo "   - ${ST_WARN_TEXT[$i]}  -> ${ST_WARN_FIX[$i]}"
    done
  fi
  echo
  echo " Исправили — повторите проверку:  sudo bash install.sh --check"
  echo " Не получилось — пришлите разработчику ВЕСЬ вывод этого скрипта и результат команд:"
  echo "     sudo docker ps"
  echo "     sudo docker logs --tail 100 lan-calendar"
  echo "=================================================================="
  return 1
}

# ------------------------------------------------------------------ приложение

# st_selftest_app ПРОЕКТ МЕТКА_ВРЕМЕНИ(unix) ЧИСЛА_ДО [ФАЙЛ_КОПИИ_БАЗЫ]
st_selftest_app() {
  local project="$1" since="$2" before="${3:-}" backup="${4:-}"
  echo
  echo "Проверки приложения:"

  local c
  for c in lan-calendar codd-bot lan-calendar-caddy; do
    if st_container_running "$c"; then st_ok "контейнер $c запущен"
    else st_bad "контейнер $c не запущен ($(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo 'нет такого'))" \
      "sudo docker logs --tail 50 $c ; затем: cd $project/docker && sudo docker compose up -d lan-calendar codd-bot"; fi
  done

  local code
  code="$(st_http http://127.0.0.1:8000/login)"
  if [ "$code" = "200" ]; then st_ok "приложение отвечает (страница входа, код 200)"
  else st_bad "приложение не отвечает на 127.0.0.1:8000 (код $code)" "sudo docker logs --tail 80 lan-calendar — причина в последних строках; после исправления: cd $project/docker && sudo docker compose up -d lan-calendar"; fi

  code="$(st_http https://127.0.0.1/login)"
  case "$code" in
    200|301|302|303|307|308) st_ok "вход по HTTPS через Caddy работает (код $code)" ;;
    *) st_warn "Caddy не ответил по HTTPS на самом сервере (код $code)" "sudo docker logs --tail 50 lan-calendar-caddy ; если раньше по https работало — сообщить разработчику" ;;
  esac

  # Код внутри работающего контейнера должен совпадать с тем, из которого собран этот пакет (файл CODE.sha256).
  if [ -f "${HERE:-.}/CODE.sha256" ]; then
    local mism
    mism="$(docker exec -i lan-calendar sh -c 'cd /app && sha256sum -c --quiet -' < "$HERE/CODE.sha256" 2>&1 | head -4 | tr '\n' ';' || true)"
    if [ -z "$mism" ]; then st_ok "в контейнере работает код этого пакета (сборка $(cat "$HERE/BUILD" 2>/dev/null))"
    else st_bad "в контейнере работает НЕ код этого пакета: $mism" \
      "cd $project/docker && sudo docker compose up -d --force-recreate --no-build lan-calendar codd-bot ; затем: sudo bash install.sh --check . Не помогло — убедиться, что запускаете install.sh из свежераспакованного архива (rm -rf ~/lan-calendar-app, tar xf нужного .tar), и прислать вывод"; fi
  fi

  # Корневой сертификат Caddy для установки на компьютеры (ссылка «Сертификат сервера» на странице входа).
  code="$(st_http https://127.0.0.1/ca.crt)"
  if [ "$code" = "200" ]; then st_ok "сертификат сервера для компьютеров отдаётся (https://АДРЕС/ca.crt)"
  else st_warn "сертификат сервера не отдаётся по /ca.crt (код $code) — Caddy работает по старому файлу настроек" "sudo docker exec lan-calendar-caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile ; не помогло — sudo docker restart lan-calendar-caddy"; fi

  local tb
  tb="$(docker logs --since "$since" lan-calendar 2>&1 | grep -c Traceback || true)"
  if [ "${tb:-0}" = "0" ]; then st_ok "в журнале приложения нет ошибок (Traceback)"
  else st_bad "в журнале приложения ошибки ($tb)" "sudo docker logs --since 10m lan-calendar 2>&1 | grep -B2 -A12 Traceback — прислать разработчику"; fi

  local db
  db="$(docker exec -i lan-calendar python - 2>&1 <<'PY' || true
import sqlite3
c = sqlite3.connect("/app/data/calendar.db")
try:                                   # зашифрованные поля — вычисляемые столбцы, проверке базы нужны функции расшифровки (в старой версии приложения их нет — не страшно)
    from app.core import field_crypto
    field_crypto.register_sqlite(c)
except Exception:
    pass
problems = []
if c.execute("pragma integrity_check").fetchone()[0] != "ok":
    problems.append("целостность базы нарушена")
if c.execute("pragma foreign_key_check").fetchall():
    problems.append("нарушены связи между таблицами")
cols = {r[1] for r in c.execute("pragma table_info(equipment)")}
if "place" not in cols:
    problems.append("таблица оборудования не обновилась")
people = c.execute("select sql from sqlite_master where name='people'").fetchone()
if not people or "'warehouse'" not in people[0]:
    problems.append("в персонале нет роли «склад»")
roles = {r[0] for r in c.execute("select role from equipment_statuses")}
if not {"ok", "defect"} <= roles:
    problems.append("нет служебных состояний оборудования")
print("OK" if not problems else "; ".join(problems))
PY
)"
  if [ "$db" = "OK" ]; then st_ok "база данных в порядке и обновлена (целостность, связи, склад)"
  else st_bad "база данных: $db" "НЕ запускать дальше. Откатить: sudo docker tag lan-calendar:previous lan-calendar:latest && cd $project/docker && sudo docker compose up -d --no-build lan-calendar codd-bot ; базу вернуть из ${backup:-$project/backups/before-update-*.db} и прислать разработчику вывод"; fi

  if [ -n "$before" ]; then
    local after t b a lost=""
    after="$(st_snapshot_counts)"
    for t in users people points crews tasks task_visit_reports equipment; do
      b="$(echo "$before" | tr ' ' '\n' | sed -n "s/^$t=//p")"
      a="$(echo "$after" | tr ' ' '\n' | sed -n "s/^$t=//p")"
      if [ -n "$b" ] && [ -n "$a" ] && [ "$b" != "-1" ] && [ "$a" -lt "$b" ]; then lost="$lost $t: было $b, стало $a;"; fi
    done
    if [ -z "$lost" ]; then st_ok "данные на месте (число записей не уменьшилось)"
    else st_bad "после обновления записей стало меньше:$lost" "откатить обновление (команда ниже) и вернуть базу из ${backup:-$project/backups/before-update-*.db}; прислать разработчику вывод"; fi
  fi

  code="$(st_http http://127.0.0.1:8000/api/equipment/meta)"
  if [ "$code" = "401" ]; then st_ok "склад закрыт без входа (код 401)"; else st_bad "раздел склада не закрыт без входа (код $code)" "сообщить разработчику"; fi
  code="$(st_http 'http://127.0.0.1:8000/api/bot/equipment/held?vk_id=1')"
  if [ "$code" = "401" ]; then st_ok "API бота закрыт без ключа (код 401)"; else st_bad "API бота не закрыт без ключа (код $code)" "сообщить разработчику"; fi
  code="$(st_http http://127.0.0.1:8000/static/js/equipment.js)"
  if [ "$code" = "200" ]; then st_ok "страницы склада отдаются"; else st_bad "файлы страницы склада не отдаются (код $code)" "образ приложения старый: повторить установку (sudo bash install.sh)"; fi

  # Безопасность: схема API закрыта, секретные файлы доступны только владельцу, заголовки защиты отдаются, ИИ — в закрытой сети.
  code="$(st_http http://127.0.0.1:8000/openapi.json)"
  if [ "$code" = "404" ]; then st_ok "схема API закрыта без входа (код 404)"; else st_bad "схема API открыта без входа (код $code)" "образ приложения старый: повторить установку (sudo bash install.sh)"; fi
  local f perm bad=""
  for f in "$project/.env" "$project/bot.env" "$project/data" "$project/backups"; do
    [ -e "$f" ] || continue
    perm="$(stat -c %a "$f" 2>/dev/null || echo '?')"
    case "$f" in
      */data|*/backups) [ "$perm" = "700" ] || bad="$bad $(basename "$f")=$perm" ;;
      *) [ "$perm" = "600" ] || bad="$bad $(basename "$f")=$perm" ;;
    esac
  done
  if [ -z "$bad" ]; then st_ok "права на секретные файлы и папки ограничены (.env, bot.env, data, backups)"
  else st_warn "права на секреты шире положенных:$bad" "sudo chmod 600 $project/.env $project/bot.env && sudo chmod 700 $project/data $project/backups"; fi
  local hdrs
  hdrs="$(curl -skI --max-time 15 https://127.0.0.1/login 2>/dev/null | tr -d '\r' | tr 'A-Z' 'a-z' || true)"
  if echo "$hdrs" | grep -q '^x-content-type-options: nosniff' && echo "$hdrs" | grep -q '^x-frame-options' && ! echo "$hdrs" | grep -q '^server:'; then
    st_ok "заголовки защиты ответов включены, версия сервера не раскрывается"
  else st_warn "заголовки защиты ответов не включены или раскрывается версия сервера" "sudo docker exec lan-calendar-caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile ; не помогло — sudo docker restart lan-calendar-caddy"; fi
  if docker ps -a --format '{{.Names}}' | grep -qx lan-calendar-llm; then
    local net open=""
    for net in $(docker inspect lan-calendar-llm lan-calendar-asr --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null); do
      [ "$(docker network inspect "$net" --format '{{.Internal}}' 2>/dev/null || echo true)" = "true" ] || open="$open $net"
    done
    if [ -z "$open" ]; then st_ok "ИИ работает в закрытой сети без выхода наружу"
    else st_warn "контейнеры ИИ подключены к сети с выходом наружу:$open" "cd $project/docker && sudo docker compose --profile ai up -d --no-build --force-recreate llm asr (после этого ИИ перезапустится примерно на минуту)"; fi
  fi

  # Бот поднялся и опрашивает ВК (ждём до 30 секунд) — если сам контейнер запущен (иначе уже есть [FAIL] выше).
  if st_container_running codd-bot; then
    local polling=0 n
    for n in 1 2 3 4 5 6; do
      if docker logs codd-bot 2>&1 | grep -q "Starting BotPolling"; then polling=1; break; fi
      sleep 5
    done
    if [ "$polling" = "1" ]; then st_ok "бот запущен и опрашивает ВКонтакте"
    else st_warn "бот не сообщил о запуске опроса ВКонтакте" "sudo docker logs --tail 40 codd-bot ; проверить файл $project/bot.env и доступ сервера к api.vk.com"; fi
  fi

  if [ -n "$backup" ]; then
    if [ -s "$backup" ]; then st_ok "копия базы перед обновлением есть ($backup)"
    else st_bad "копия базы перед обновлением не создана" "сделать вручную: sudo cp $project/data/calendar.db $project/backups/manual-$(date +%Y%m%d).db"; fi
  fi
  return 0
}

# ------------------------------------------------------------------ робот карт

# st_selftest_robot ПРОЕКТ
st_selftest_robot() {
  local project="$1"
  echo
  echo "Проверки робота карт:"

  if st_container_running yandex-robot; then st_ok "контейнер yandex-robot запущен"
  else st_bad "контейнер yandex-robot не запущен" "sudo docker logs --tail 50 yandex-robot ; затем: cd $project/docker && sudo docker compose --profile robot up -d --no-build yandex-robot"; fi

  if ! st_container_running lan-calendar; then
    st_bad "приложение (lan-calendar) не запущено — роботу не с кем работать" "сначала установите/запустите приложение: sudo bash install.sh из папки lan-calendar-app"
    return
  fi

  local out
  out="$(docker exec -i lan-calendar python - 2>&1 <<'PY' || true
import json
import httpx
from app.config import get_settings
s = get_settings()
h = {"X-Robot-Key": s.yandex_robot_api_key}
try:
    r = httpx.get(s.yandex_robot_url + "/status", headers=h, timeout=120)
except Exception as e:
    print("STATUS_ERR " + type(e).__name__); raise SystemExit
if r.status_code != 200:
    print(f"STATUS_BAD {r.status_code}"); raise SystemExit
print("STATUS_OK " + str(r.json().get("chrome", "")))
n = httpx.get(s.yandex_robot_url + "/netcheck", headers=h, timeout=90).json()
for i in n["items"]:
    if not i["ok"]:
        print(("NET_REQ " if i["required"] else "NET_OPT ") + i["host"] + " | " + i["error"])
print("NET_DONE")
PY
)"
  case "$out" in
    *STATUS_OK*) st_ok "приложение видит робота, браузер Chromium запускается ($(echo "$out" | sed -n 's/^STATUS_OK //p' | head -1))" ;;
    *STATUS_BAD\ 401*) st_bad "ключ робота не совпал с ключом приложения" "обновить приложение (шаг 1: lan-calendar-app) и перезапустить робота: sudo docker restart yandex-robot" ;;
    *STATUS_BAD*) st_bad "робот ответил ошибкой: $(echo "$out" | grep STATUS_BAD | head -1)" "sudo docker logs --tail 80 yandex-robot — прислать разработчику" ;;
    *STATUS_ERR*) st_bad "приложение не достучалось до робота ($(echo "$out" | grep STATUS_ERR | head -1))" "sudo docker logs --tail 50 yandex-robot ; sudo docker restart yandex-robot ; повторить проверку" ;;
    *) st_bad "не удалось проверить робота: $(echo "$out" | tail -2 | tr '\n' ' ')" "sudo docker logs --tail 80 lan-calendar — прислать разработчику" ;;
  esac

  if echo "$out" | grep -q "^NET_DONE"; then
    local req opt
    req="$(echo "$out" | sed -n 's/^NET_REQ //p')"
    opt="$(echo "$out" | sed -n 's/^NET_OPT //p')"
    if [ -z "$req" ]; then st_ok "сервер достукивается до нужных адресов Яндекса"
    else st_warn "с сервера НЕ открываются обязательные адреса Яндекса (порт 443): $(echo "$req" | cut -d'|' -f1 | tr -d ' ' | tr '\n' ' ')" "передать сетевому администратору: открыть исходящий HTTPS (443) с этого сервера на yandex.ru, passport.yandex.ru, yastatic.net, api-maps.yandex.ru; до этого карты вестись не будут (всё остальное работает); затем повторить: sudo bash install.sh --check"; fi
    if [ -n "$opt" ]; then
      echo "  [info] необязательные адреса недоступны (карта может грузиться без фона): $(echo "$opt" | cut -d'|' -f1 | tr -d ' ' | tr '\n' ' ')"
    fi
  fi
  return 0
}
