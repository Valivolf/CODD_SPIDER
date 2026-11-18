"""Loopback-only UI fixture using the same temporary DB isolation as test_accounting.
Not a production launcher. No app.main, background jobs, .env, or production DB.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_accounting import AccountingTests, fields_repo
from fastapi.staticfiles import StaticFiles
import uvicorn


async def main():
    fixture = AccountingTests()
    await fixture.asyncSetUp()
    from app.web import routes_apk_page, routes_accounting_page
    from app.api import routes_point_categories, routes_point_statuses, routes_work_types, routes_apk_groups, routes_point_plans

    async def page_user(request, *keys):
        return fixture.user, None

    routes_apk_page.page_user = page_user
    routes_accounting_page.page_user = page_user
    for router in (routes_apk_page.router, routes_accounting_page.router, routes_point_categories.router,
                   routes_point_statuses.router, routes_work_types.router, routes_apk_groups.router, routes_point_plans.router):
        fixture.app.include_router(router)
    fixture.app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parents[1] / "static"), name="static")

    @fixture.app.get("/api/auth/me")
    async def me():
        return fixture.user

    @fixture.app.get("/api/security/policy")
    async def security_policy():
        return {"idle_minutes": 0}

    @fixture.app.get("/__test/role")
    async def set_role(key: str):
        if key not in ("accounting", "user", "admin", "viewer"):
            raise ValueError(key)
        fixture.user = fixture.role(key)
        return fixture.user

    @fixture.app.get("/__test/fields")
    async def field_ids():
        return fixture.fields

    @fixture.app.get("/__test/count")
    async def point_count():
        return {"count": await fixture.scalar("select count(*) from points")}

    @fixture.app.post("/__test/conflict")
    async def concurrent_edit():
        current = await fixture.scalar("select accounting_revision from points where id='B'")
        await fields_repo.save_values("B", {fixture.fields["Контракт"]: "Изменено другим бухгалтером"},
                                     expected_revision=current, allow_accounting=True)
        return {"ok": True}

    server = uvicorn.Server(uvicorn.Config(fixture.app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning", access_log=False))

    @fixture.app.post("/__test/stop")
    async def stop():
        server.should_exit = True
        return {"ok": True}

    try:
        await server.serve()
    finally:
        await fixture.asyncTearDown()


if __name__ == "__main__":
    asyncio.run(main())
