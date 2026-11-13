"""FastAPI application factory — wires config, db, routers and background tasks."""
from __future__ import annotations

from app.core import runtime_state, tz

tz.apply()          # время сервера — московское (см. app/core/tz.py): «сегодня», имена копий, журнал

import asyncio
import logging
import secrets
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    routes_absences,
    routes_access_issues,
    routes_auth,
    routes_backup,
    routes_check,
    routes_bot,
    routes_bot_chat,
    routes_ai,
    routes_apk_groups,
    routes_point_files,
    routes_point_fields,
    routes_stat_counters,
    routes_ai_agents,
    routes_assistant,
    routes_bot_ai,
    routes_bot_settings,
    routes_bot_warehouse,
    routes_chat,
    routes_account_chat,
    routes_chat_broadcast,
    routes_crews,
    routes_consumables,
    routes_demontage_requests,
    routes_encryption,
    routes_equipment,
    routes_equipment_import,
    routes_excel,
    routes_forms_auto,
    routes_forms_import,
    routes_join_requests,
    routes_kml,
    routes_live_maps,
    routes_monitoring,
    routes_notifications,
    routes_people,
    routes_point_categories,
    routes_point_plans,
    routes_point_statuses,
    routes_work_types,
    routes_points,
    routes_report_designs,
    routes_report_templates,
    routes_reports,
    routes_roles,
    routes_security,
    routes_sklad_import,
    routes_solar_panels,
    routes_state,
    routes_stats,
    routes_system,
    routes_task_visit_reports,
    routes_games,
    routes_users,
    routes_verification_requests,
    routes_pautina,
    routes_weather,
    routes_visit_history,
    routes_photo_storage,
    routes_warehouse,
    routes_yandex_robot,
)
from app.config import ROOT_DIR, get_settings
from app.core.audit_mw import audit_and_cache_headers
from app.core.body_limit import MaxBodySizeMiddleware
from app.core.perm_guard import permission_guard
from app.core.viewer_guard import origin_guard
from app.db.bootstrap import bootstrap_admin
from app.db.database import init_db
from app.repositories import chat_repository, work_types_repository
from app.services import equipment_service, photo_storage, yandex_map_sync
from app.services.ai import jobs as ai_jobs
from app.services.ai import agents as ai_agents, forms_fault, warmup as ai_warmup
from app.services import apk_group_auto, forms_auto, security_log, system_health
from app.services.backup_service import daily_backup_loop
from app.services.point_health_service import point_health_loop
from app.web import (
    routes_absences_page,
    routes_apk_page,
    routes_assistant_page,
    routes_games_page,
    routes_monitoring_page,
    routes_notifications_page,
    routes_pages,
    routes_plan_print,
    routes_reports_page,
    routes_stats_page,
    routes_warehouse_page,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Библиотека сетевой папки (SMB) пишет строку INFO на КАЖДОЕ открытие файла или папки — при поиске фото это ≈100 строк в секунду (по замеру на рабочем
# сервере 62 тыс. из 66 тыс. строк журнала за 10 минут): лишняя работа процессора и диска. Нужны только предупреждения и ошибки.
for _noisy_logger in ("smbprotocol", "smbclient", "spnego"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

# Comfortably above the largest legitimate body (a full calendar-state PUT
# or a points-import .xlsx), but bounded — enforced before routing/auth
# ever see the request, see app/core/body_limit.py.
MAX_REQUEST_BODY_SIZE = 20 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await work_types_repository.load()
    await bootstrap_admin()
    try:
        await equipment_service.backfill_point_sims()   # SIM комплексов — единицы в учёте оборудования
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("Не удалось сверить SIM-карты комплексов с учётом оборудования")
    backup_task = asyncio.create_task(daily_backup_loop())
    health_task = asyncio.create_task(point_health_loop())
    maps_task = asyncio.create_task(yandex_map_sync.sync_loop())  # карты бригад в Яндекс.Конструкторе
    chat_task = asyncio.create_task(chat_repository.retention_loop())  # переписка в чате хранится неделю
    account_chat_task = asyncio.create_task(routes_account_chat.attachment_retention_loop())
    ai_task = asyncio.create_task(ai_jobs.retention_loop())            # задачи и файлы ИИ — несколько дней; прерванные перезапуском закрываются ошибкой
    warm_task = asyncio.create_task(ai_warmup.startup_warm())
    watch_task = asyncio.create_task(ai_warmup.watch_model())          # модель перезапустили — подсказки греются заново сами
    forms_task = asyncio.create_task(forms_auto.loop())                # отчёты из Яндекс Форм сами (если включено в настройках)          # если ИИ включён — «прочитать» постоянную часть подсказок заранее
    integrity_task = asyncio.create_task(system_health.integrity_loop())   # проверка целостности базы: через минуту после запуска и раз в 12 часов
    seclog_task = asyncio.create_task(security_log.retention_loop())       # журнал безопасности: раз в сутки убирает события старше срока хранения
    groups_task = asyncio.create_task(apk_group_auto.loop())               # подгруппа «Требуют повторного выезда»: состав ведёт программа
    agents_task = asyncio.create_task(ai_agents.loop())                    # проверка связи с ИИ-агентами на компьютерах; давно молчащих убирает из списка
    faults_task = asyncio.create_task(forms_fault.loop())                  # ИИ разбирает итоги неисправных отчётов из Яндекс Форм (если включено в настройках ИИ)
    for name, task in (("журнал безопасности", seclog_task), ("копии базы", backup_task), ("проверка связи с комплексами", health_task), ("карты в Яндексе", maps_task), ("чат", chat_task), ("задачи ИИ", ai_task),
                       ("прогрев ИИ", warm_task), ("слежение за моделью", watch_task), ("отчёты из Форм", forms_task), ("проверка целостности", integrity_task), ("подгруппа «Требуют повторного выезда»", groups_task), ("ИИ: неисправности по отчётам форм", faults_task), ("ИИ-агенты: проверка связи", agents_task)):
        runtime_state.register_task(name, task, oneshot=task is warm_task)       # прогрев модели — разовая задача: окончание — норма
    runtime_state.register_task("вложения личных сообщений", account_chat_task)
    try:
        await photo_storage.load_config()                   # куда писать фото: сетевая папка из настроек / PHOTOS_DIR / диск сервера
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("Не удалось загрузить настройки хранилища фото — фото пишутся на диск сервера")
    photos_task = asyncio.create_task(photo_storage.storage_loop())  # буфер фото на сервере → сетевой диск
    try:
        yield
    finally:
        backup_task.cancel()
        health_task.cancel()
        maps_task.cancel()
        photos_task.cancel()
        chat_task.cancel()
        account_chat_task.cancel()
        ai_task.cancel()
        warm_task.cancel()
        watch_task.cancel()
        forms_task.cancel()
        integrity_task.cancel()
        seclog_task.cancel()
        groups_task.cancel()
        faults_task.cancel()
        agents_task.cancel()
        for task in (backup_task, health_task, maps_task, photos_task, chat_task, account_chat_task, ai_task, warm_task, watch_task, forms_task, integrity_task, seclog_task, groups_task, faults_task, agents_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        from app.db.database import close_pool  # noqa: PLC0415

        await close_pool()


class CachedStaticFiles(StaticFiles):
    """Статика с заголовками кэша. Адреса из шаблонов — «…?v=<время изменения файла>» (app/web/asset_url.py): при правке файла
    адрес меняется, поэтому браузер может хранить такой файл «вечно» и вообще не спрашивать сервер (иначе каждая страница
    при каждом открытии проверяла бы десяток файлов). Адреса без версии (шрифты, значки) — на час."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            versioned = b"v=" in scope.get("query_string", b"")
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if versioned else "public, max-age=3600"
        return response


_FIELD_WORDS = {"missing": "обязательное поле не заполнено", "string_too_short": "слишком короткое значение", "string_too_long": "слишком длинное значение",
                "int_parsing": "нужно целое число", "float_parsing": "нужно число", "bool_parsing": "нужно «да» или «нет»", "json_invalid": "неверный формат данных",
                "greater_than_equal": "значение слишком маленькое", "less_than_equal": "значение слишком большое", "list_type": "нужен список", "enum": "недопустимое значение"}


def _validation_text(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors()[:4]:
        where = " → ".join(str(x) for x in err.get("loc", ()) if x not in ("body", "query", "path"))
        msg = str(err.get("msg") or "")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        else:
            msg = _FIELD_WORDS.get(err.get("type", ""), msg)
        parts.append(f"«{where}»: {msg}" if where else msg)
    return "Неверные данные: " + "; ".join(parts) if parts else "Неверные данные в запросе"


def _install_error_handlers(app: FastAPI) -> None:
    """Понятные ответы вместо сухих «Internal Server Error» и списков ошибок проверки: человек видит, что случилось и что делать, а в журнале остаётся код ошибки."""
    log = logging.getLogger("app.errors")

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": _validation_text(exc)}, status_code=422)

    @app.exception_handler(sqlite3.OperationalError)
    async def _db_error(request: Request, exc: sqlite3.OperationalError):
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            return JSONResponse({"detail": "База данных занята другой операцией — повторите через несколько секунд."}, status_code=503, headers={"Retry-After": "3"})
        if "full" in text:
            log.error("Диск заполнен при записи в базу: %s", exc)
            return JSONResponse({"detail": "На сервере закончилось место на диске, данные не сохранены. Освободите место (Настройки → Состояние системы подскажет, что занимает диск)."}, status_code=507)
        return await _unhandled(request, exc)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        code = secrets.token_hex(3).upper()
        log.error("Необработанная ошибка %s %s [код %s]", request.method, request.url.path, code, exc_info=exc)
        return JSONResponse({"detail": f"Внутренняя ошибка сервера (код {code}). Повторите действие; если повторяется — сообщите администратору, назвав код."}, status_code=500)


def create_app() -> FastAPI:
    settings = get_settings()
    # /docs, /redoc и /openapi.json отключены: схема всех адресов сервера не должна отдаваться без входа (администратору — /api/system/openapi)
    app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(MaxBodySizeMiddleware, max_body_size=MAX_REQUEST_BODY_SIZE)
    app.middleware("http")(permission_guard)         # права роли на каждый адрес (app/core/permissions.py::RULES)
    app.middleware("http")(origin_guard)
    app.middleware("http")(audit_and_cache_headers)

    app.mount("/static", CachedStaticFiles(directory=str(ROOT_DIR / "static")), name="static")
    _install_error_handlers(app)

    app.include_router(routes_system.router)
    app.include_router(routes_security.router)
    app.include_router(routes_pages.router)
    app.include_router(routes_warehouse_page.router)
    app.include_router(routes_apk_page.router)
    app.include_router(routes_absences_page.router)
    app.include_router(routes_assistant_page.router)
    app.include_router(routes_games_page.router)
    app.include_router(routes_stats_page.router)
    app.include_router(routes_reports_page.router)
    app.include_router(routes_monitoring_page.router)
    app.include_router(routes_notifications_page.router)
    app.include_router(routes_plan_print.router)
    app.include_router(routes_auth.router)
    app.include_router(routes_users.router)
    app.include_router(routes_games.router)
    app.include_router(routes_games.admin_router)
    app.include_router(routes_roles.router)
    app.include_router(routes_state.router)
    app.include_router(routes_excel.router)
    app.include_router(routes_kml.router)
    app.include_router(routes_backup.router)
    app.include_router(routes_encryption.router)
    app.include_router(routes_people.router)
    app.include_router(routes_points.router)
    app.include_router(routes_point_categories.router)
    app.include_router(routes_point_fields.router)
    app.include_router(routes_point_plans.router)
    app.include_router(routes_apk_groups.router)
    app.include_router(routes_point_files.router)
    app.include_router(routes_stat_counters.router)
    app.include_router(routes_ai_agents.agent_router)
    app.include_router(routes_ai_agents.admin_router)
    app.include_router(routes_check.router)
    app.include_router(routes_point_statuses.router)
    app.include_router(routes_work_types.router)
    app.include_router(routes_crews.router)
    app.include_router(routes_warehouse.router)
    app.include_router(routes_equipment.router)
    app.include_router(routes_consumables.router)
    app.include_router(routes_equipment_import.router)
    app.include_router(routes_sklad_import.router)
    app.include_router(routes_solar_panels.router)
    app.include_router(routes_task_visit_reports.router)
    app.include_router(routes_stats.router)
    app.include_router(routes_reports.router)
    app.include_router(routes_report_templates.router)
    app.include_router(routes_report_designs.router)
    app.include_router(routes_notifications.router)
    app.include_router(routes_chat_broadcast.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_account_chat.router)
    app.include_router(routes_bot_chat.router)
    app.include_router(routes_ai.router)
    app.include_router(routes_forms_auto.router)
    app.include_router(routes_assistant.router)
    app.include_router(routes_bot_ai.router)
    app.include_router(routes_bot.router)
    app.include_router(routes_bot_settings.router)
    app.include_router(routes_bot_warehouse.router)
    app.include_router(routes_verification_requests.router)
    app.include_router(routes_access_issues.router)
    app.include_router(routes_demontage_requests.router)
    app.include_router(routes_join_requests.router)
    app.include_router(routes_monitoring.router)
    app.include_router(routes_yandex_robot.router)
    app.include_router(routes_live_maps.router)
    app.include_router(routes_absences.router)
    app.include_router(routes_visit_history.router)
    app.include_router(routes_forms_import.router)
    app.include_router(routes_pautina.router)
    app.include_router(routes_weather.router)
    app.include_router(routes_photo_storage.router)

    return app


app = create_app()
