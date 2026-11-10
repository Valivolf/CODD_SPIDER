РОБОТ ЯНДЕКС.КОНСТРУКТОРА КАРТ — УСТАНОВКА НА СЕРВЕР БЕЗ ИНТЕРНЕТА
===================================================================

Это «браузер»: образ Docker с Chromium. Большой файл, но возить его нужно ОДИН раз. Код робота
обновляется вместе с приложением (архив lan-calendar-app-<дата>.tar), образ при этом не меняется.

Сначала установите обновление приложения (lan-calendar-app-….tar) — робот работает только с новой версией.

Что внутри (папка yandex-robot):
  yandex-robot.tar.gz  готовый образ робота (lan-calendar-robot:latest) — браузер уже внутри
  robot/               файлы робота (нужны compose-файлу)
  docker-compose.yml   на случай, если на сервере ещё старый (скрипт заменит его, только если нет сервиса yandex-robot)
  install.sh           установка одной командой

Куда положить и что запустить (на рабочем сервере):

        tar xf yandex-robot-*.tar
        cd yandex-robot
        sudo bash install.sh            # или: sudo bash install.sh /путь/к/проекту

После установки (в браузере, под администратором):
  Настройки → «🤖 Яндекс-аккаунт для карт»
    • «Проверить связь с Яндексом» — какие адреса Яндекса открываются с сервера.
    • «Войти в Яндекс» — вход делается в окне прямо в календаре.

Какие адреса должны открываться с сервера (порт 443, HTTPS) — для администратора сети:
  обязательно:   yandex.ru, passport.yandex.ru, yastatic.net, api-maps.yandex.ru
  желательно:    maps.yastatic.net, core-renderer-tiles.maps.yandex.net, core-renderer-tiles.maps.yandex.ru,
                 api.passport.yandex.ru, mc.yandex.ru, csp.yandex.net
  Без обязательных адресов робот работать не будет (приложение и всё остальное работает как раньше).

Удалить робота:
  cd /opt/lan_calendar/docker && docker compose --profile robot rm -sf yandex-robot

Журнал:  docker logs yandex-robot
