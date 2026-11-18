"""Isolated integration tests. Never load .env or app.main / its background jobs.

Run: .venv/Scripts/python.exe -m unittest discover -s tests -v
"""
import asyncio
import base64
import datetime as dt
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
import httpx
from openpyxl import load_workbook

import app.config as config

# Install the isolated settings provider BEFORE importing repositories/routes.
# No import in this module may resolve the production settings provider.
_settings = None


def isolated_settings():
    if _settings is None:
        raise RuntimeError("Test settings are not initialized; production access forbidden")
    return _settings


config.get_settings = isolated_settings

from app.db import database
from app.core import permissions as P, perm_guard, accounting
from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.api import routes_accounting, routes_point_fields, routes_point_files, routes_monitoring, routes_points
from app.repositories import point_category_fields_repository as fields_repo, points_repository
from app.services import point_merge
from app.web import routes_apk_page, routes_accounting_page


class AccountingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        global _settings
        self.tmp = tempfile.TemporaryDirectory(prefix="codd-accounting-test-")
        root = Path(self.tmp.name)
        _settings = config.Settings(_env_file=None, data_dir=root, backup_dir=root / "backups",
            secret_key="isolated-test-key-for-accounting-2026", bot_api_key="isolated-bot", yandex_robot_api_key="isolated-robot",
            field_encryption_key=base64.urlsafe_b64encode(b"a" * 32).decode(), photos_dir=None)
        self.assertTrue(_settings.db_path.resolve().is_relative_to(root.resolve()))
        await database.init_db()  # only the new temporary DB; no application lifespan
        self.user = self.role("accounting")

        async def session(request):
            return self.user

        async def dependency():
            return self.user

        self.session_patch = patch.object(perm_guard, "get_session_user", session)
        self.session_patch.start()
        self.log_patch = patch.object(perm_guard.security_log, "record_denied", return_value=None)
        self.log_patch.start()
        self.app = FastAPI()
        self.app.dependency_overrides[require_password_ok] = dependency
        self.app.middleware("http")(perm_guard.permission_guard)
        for router in (routes_accounting.router, routes_point_fields.router, routes_point_files.router,
                       routes_monitoring.router, routes_points.router):
            self.app.include_router(router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.fields = {f["name"]: f["id"] for f in await fields_repo.list_fields() if f["accounting_only"]}
        self.assertEqual(len(self.fields), 10)
        async with database.get_connection() as conn:
            for pid, inv in (("A", "00123"), ("B", "00999"), ("C", "00123")):
                await conn.execute("insert into points(id, inventory_number, address, lat, lon, ip_address, port, sim_number) values (?, ?, ?, '55.5', '37.5', '10.1.2.3', '8080', 'secret-sim')", (pid, inv, "Адрес " + pid))
            await conn.commit()

    async def asyncTearDown(self):
        await self.client.aclose()
        self.session_patch.stop()
        self.log_patch.stop()
        await database.close_pool()
        self.tmp.cleanup()

    def role(self, key, extra=()):
        return UserOut(id=1, username=key, role=key, must_change_password=False, created_at="2026-01-01",
                       permissions=sorted(P.default_perms(key) | set(extra)))

    async def save(self, pid="A", values=None, revision=0):
        return await self.client.put(f"/api/point-category-fields/point/{pid}", json={
            "values": {str(self.fields[k]): v for k, v in (values or {"Контракт": "Договор"}).items()},
            "expected_revision": revision})

    async def scalar(self, sql, args=()):
        async with database.get_connection() as conn:
            cur = await conn.execute(sql, args)
            return (await cur.fetchone())[0]

    async def test_list_criterion_search_and_no_physical_creation(self):
        self.assertEqual((await self.client.get("/api/accounting/points")).json(), [])
        before = await self.scalar("select count(*) from points")
        r = await self.save(values={"Инвентарный номер": "000045", "Наименование в бухгалтерском учете": "Камера Север"})
        self.assertEqual(r.status_code, 200, r.text)
        for q in ("A", "Камера Север", "00123", "000045"):
            result = await self.client.get("/api/accounting/points", params={"q": q})
            self.assertEqual([p["id"] for p in result.json()], ["A"])
        self.assertEqual((await self.client.get("/api/accounting/points", params={"q": "10.1.2.3"})).json(), [])
        self.assertEqual((await self.client.get("/api/accounting/points", params={"q": "%"})).json(), [])
        self.assertEqual(await self.scalar("select count(*) from points"), before)
        self.assertEqual(await self.scalar("select inventory_number from points where id='A'"), "00123")
        r = await self.save(values={"Инвентарный номер": "", "Наименование в бухгалтерском учете": ""}, revision=1)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((await self.client.get("/api/accounting/points")).json(), [])

    async def test_candidates_include_unregistered_and_both_number_sources(self):
        await self.save("B", {"Инвентарный номер": "00123"})
        r = await self.client.get("/api/accounting/candidates", params={"inventory": "00123"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([p["id"] for p in r.json()], ["A", "B", "C"])
        self.assertEqual(r.headers["x-total-count"], "3")
        self.assertNotIn("accounting_fields", r.json()[0])
        for number in ("123", "A", "missing"):
            self.assertEqual((await self.client.get("/api/accounting/candidates", params={"inventory": number})).json(), [])
        self.assertEqual((await self.client.get("/api/accounting/candidates", params={"inventory": " "})).status_code, 400)

    async def test_excel_export_selection_values_and_safe_cells(self):
        values = {name: "Значение" for name in self.fields}
        values.update({"Инвентарный номер": "000000000000000042", "Заводской номер": "00001",
                       "Наименование в бухгалтерском учете": '=HYPERLINK("https://example.invalid", "link")',
                       "Балансовая стоимость": "12345678901234567890.12", "Год выпуска": "2020",
                       "Дата ввода в эксплуатацию": "2024-02-29", "Контракт": "#N/A"})
        self.assertEqual((await self.save("A", values)).status_code, 200)
        self.assertEqual((await self.save("B", {"Балансовая стоимость": "12345.67"})).status_code, 200)
        await self.save("C")
        before = await self.scalar("select sum(accounting_revision) from points")
        response = await self.client.post("/api/accounting/export", json={"point_ids": ["B", "A", "A"]})
        self.assertEqual(response.status_code, 200, response.text[:100] if response.status_code != 200 else "")
        self.assertEqual(response.headers["x-points-exported"], "2")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn("no-store", response.headers["cache-control"])
        wb = load_workbook(io.BytesIO(response.content))
        ws = wb.active
        headers = [cell.value for cell in ws[1]]
        self.assertEqual(headers[:4], ["Наименование в бухгалтерском учете", "Адрес", "Широта", "Долгота"])
        self.assertNotIn("Номер АПК", headers)
        self.assertEqual(headers.count("Наименование в бухгалтерском учете"), 1)
        self.assertEqual(set(headers[4:]), set(self.fields) - {"Наименование в бухгалтерском учете"})
        self.assertEqual(ws.max_row, 3)
        self.assertEqual(ws.cell(2, 1).value, values["Наименование в бухгалтерском учете"])
        cells = dict(zip(headers, ws[2]))
        self.assertEqual(cells["Инвентарный номер"].value, values["Инвентарный номер"])
        self.assertEqual(cells["Заводской номер"].value, "00001")
        self.assertEqual(cells["Балансовая стоимость"].value, values["Балансовая стоимость"])
        self.assertEqual(cells["Балансовая стоимость"].data_type, "s")
        self.assertEqual(cells["Наименование в бухгалтерском учете"].data_type, "s")
        self.assertEqual(cells["Контракт"].data_type, "s")
        self.assertEqual(cells["Год выпуска"].value, 2020)
        self.assertEqual(cells["Дата ввода в эксплуатацию"].value.date(), dt.date(2024, 2, 29))
        money = ws.cell(3, headers.index("Балансовая стоимость") + 1)
        self.assertEqual(money.value, 12345.67)
        self.assertEqual(money.data_type, "n")
        self.assertEqual(ws.freeze_panes, "B2")
        self.assertEqual(ws.auto_filter.ref, ws.dimensions)
        self.assertFalse(any(c.data_type == "f" for row in ws for c in row))
        self.assertNotIn("secret-sim", str(list(ws.values)))
        self.assertNotIn("10.1.2.3", str(list(ws.values)))
        wb.close()
        self.assertEqual(await self.scalar("select sum(accounting_revision) from points"), before)

    async def test_excel_export_permissions_and_stale_selection(self):
        await self.save("A")
        for payload in ({"point_ids": []}, {"point_ids": ["A"] * 5001}):
            self.assertEqual((await self.client.post("/api/accounting/export", json=payload)).status_code, 422)
        for ids in (["A", "MISSING"], ["A", "B"], ["A' OR 1=1 --"]):
            self.assertEqual((await self.client.post("/api/accounting/export", json={"point_ids": ids})).status_code, 409)
        for role in ("user", "viewer"):
            self.user = self.role(role)
            self.assertEqual((await self.client.post("/api/accounting/export", json={"point_ids": ["A"]})).status_code, 403)
        for user in (self.role("admin"), self.role("user", extra=("accounting.view",))):
            self.user = user
            self.assertEqual((await self.client.post("/api/accounting/export", json={"point_ids": ["A"]})).status_code, 200)

    async def test_all_ten_fields_round_trip_and_exact_currency(self):
        values = {k: "Строка" for k in self.fields}
        values.update({"Заводской номер": "00000001", "Инвентарный номер": "00000002",
                       "Балансовая стоимость": "12345678901234567890,12", "Год выпуска": "2024", "Дата ввода в эксплуатацию": "2024-02-29"})
        r = await self.save(values=values)
        self.assertEqual(r.status_code, 200, r.text)
        point = (await self.client.get("/api/accounting/point", params={"point_id": "A"})).json()
        stored = {f["name"]: f["value"] for f in point["accounting_fields"]}
        self.assertEqual(stored["Балансовая стоимость"], "12345678901234567890.12")
        self.assertEqual(stored["Инвентарный номер"], "00000002")
        self.assertEqual(len(stored), 10)
        self.assertEqual(point["accounting_revision"], 1)
        self.assertEqual((await self.save(values={"Балансовая стоимость": "NaN"}, revision=1)).status_code, 400)
        self.assertEqual((await self.save(values={"Балансовая стоимость": "1e9999999"}, revision=1)).status_code, 400)

    async def test_stale_missing_and_concurrent_versions(self):
        first, second = await asyncio.gather(self.save(values={"Контракт": "Первый"}), self.save(values={"Контракт": "Второй"}))
        self.assertEqual(sorted([first.status_code, second.status_code]), [200, 409])
        self.assertEqual((await self.save()).status_code, 409)
        r = await self.client.put("/api/point-category-fields/point/A", json={"values": {str(self.fields["Контракт"]): "Без версии"}})
        self.assertEqual(r.status_code, 409)
        # Clearing all values still advances the revision, preventing stale creation.
        self.assertEqual((await self.save(values={"Контракт": ""}, revision=1)).status_code, 200)
        self.assertEqual((await self.save()).status_code, 409)

    async def test_role_read_write_matrix_and_general_fields(self):
        extra = await fields_repo.create_field(None, "Обычное поле", "text", False)
        self.assertEqual((await self.save()).status_code, 200)
        for role in ("user", "viewer", "warehouse"):
            self.user = self.role(role, ("accounting.edit",))  # accidental grant must not enable editing
            r = await self.client.get("/api/point-category-fields/point/A")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(any(f["name"] == "Контракт" and f["value"] == "Договор" for f in r.json()))
            self.assertEqual((await self.save(revision=1)).status_code, 403)
        self.user = self.role("user")
        r = await self.client.put("/api/point-category-fields/point/A", json={"values": {str(extra["id"]): "Техническое поле"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.user = self.role("accounting", ("points.edit",))
        r = await self.client.put("/api/point-category-fields/point/A", json={"values": {str(extra["id"]): "Запрещено"}})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(any(f["id"] == extra["id"] for f in (await self.client.get("/api/point-category-fields/point/A")).json()))
        self.user = self.role("admin")
        self.assertEqual((await self.save(revision=1)).status_code, 200)

    async def test_accounting_projections_and_technical_api_denials(self):
        await self.save()
        forbidden = {"sim_number", "ip_address", "port", "login", "password", "status", "equipment", "history", "groups", "plan"}
        for path in ("/api/accounting/points", "/api/accounting/point?point_id=A", "/api/points", "/api/points/A", "/api/point-card?point_id=A"):
            r = await self.client.get(path)
            self.assertEqual(r.status_code, 200, r.text)
            items = r.json() if isinstance(r.json(), list) else [r.json()]
            for item in items:
                self.assertFalse(forbidden & item.keys(), (path, item))
        self.user = self.role("accounting", ("points.edit", "points.network", "points.secrets"))
        for path in ("/api/points/A/ping", "/api/points/A/check-port", "/api/points/A/sim-links"):
            self.assertEqual((await self.client.get(path)).status_code, 403)
        for method, path, body in (("POST", "/api/points", {"id": "NEW"}), ("PUT", "/api/points/A", {"id": "A", "address": "changed"}), ("DELETE", "/api/points/A", None)):
            self.assertEqual((await self.client.request(method, path, json=body)).status_code, 403)
        self.assertEqual(await self.scalar("select count(*) from points"), 3)
        self.assertTrue(P.allowed("POST", "/api/equipment/points/A/attach", self.role("accounting").permissions)[0])

    async def test_port_redaction_keeps_other_roles_semantics(self):
        sample = {"id": "A", "ip_address": "10.1.2.3", "port": "8080", "login": "secret", "password": "secret"}
        viewer = routes_points._strip_credentials(sample, self.role("viewer"))
        self.assertEqual([viewer[k] for k in ("ip_address", "port", "login", "password")], [""] * 4)
        self.assertEqual(routes_points._strip_credentials(sample, self.role("user")), sample)
        self.assertEqual(routes_points._strip_credentials(sample, self.role("admin")), sample)

    async def test_operator_create_update_search_and_accounting_values_survive(self):
        await self.save("A", {"Контракт": "Сохранить"})
        self.user = self.role("user")
        payload = {"id": "NEW", "address": "Новый адрес", "inventory_number": "00009", "ip_address": "192.0.2.1", "port": "80"}
        r = await self.client.post("/api/points", json=payload)
        self.assertEqual(r.status_code, 201, r.text)
        payload["address"] = "Исправленный адрес"
        self.assertEqual((await self.client.put("/api/points/NEW", json=payload)).status_code, 200)
        r = await self.client.get("/api/points", params={"search": "00009", "limit": 50})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([p["id"] for p in r.json()], ["NEW"])
        self.assertEqual(r.json()[0]["ip_address"], "192.0.2.1")
        self.assertEqual(await self.scalar("select value from point_category_field_values where point_id='A' and field_id=?", (self.fields["Контракт"],)), "Сохранить")

    async def test_schema_extension_repeatable_without_rewriting_values_or_roles(self):
        await self.save("A", {"Балансовая стоимость": "999999999999999999.01", "Инвентарный номер": "00005"})
        async with database.get_connection() as conn:
            cur = await conn.execute("select key, name, permissions from roles order by key")
            roles_before = await cur.fetchall()
        await database.init_db()
        await database.init_db()
        self.assertEqual(await self.scalar("select accounting_revision from points where id='A'"), 1)
        self.assertEqual(await self.scalar("select inventory_number from points where id='A'"), "00123")
        self.assertEqual(await self.scalar("select value from point_category_field_values where point_id='A' and field_id=?", (self.fields["Балансовая стоимость"],)), "999999999999999999.01")
        async with database.get_connection() as conn:
            cur = await conn.execute("select key, name, permissions from roles order by key")
            self.assertEqual(await cur.fetchall(), roles_before)

    async def test_accountant_login_lands_on_apk_other_roles_unchanged(self):
        from app.api import routes_auth
        from app.services import auth_service
        self.app.include_router(routes_auth.router)
        for role in ("accounting", "user", "admin"):
            await auth_service.create_user("login-" + role, "Test-password-2026", role)
            r = await self.client.post("/api/auth/login", json={"username": "login-" + role, "password": "Test-password-2026"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["landing"], "/apk" if role == "accounting" else "/")

    async def test_upgrade_from_schema_without_accounting_revision(self):
        # Simulate the previous schema only inside this test's temporary database.
        async with database.get_connection() as conn:
            await conn.execute("alter table points drop column accounting_revision")
            await conn.execute("insert into point_category_field_values(point_id, field_id, value, updated_at) values ('A', ?, '000007', '2025-01-01')", (self.fields["Инвентарный номер"],))
            await conn.commit()
        await database.init_db()
        point = (await self.client.get("/api/accounting/point", params={"point_id": "A"})).json()
        self.assertEqual(point["accounting_revision"], 0)
        self.assertEqual(point["inventory_number"], "00123")
        self.assertEqual(point["accounting_inventory"], "000007")
        self.assertEqual(await self.scalar("select updated_at from point_category_field_values where point_id='A' and field_id=?", (self.fields["Инвентарный номер"],)), "2025-01-01")
        self.assertEqual((await self.save()).status_code, 200)

    async def test_merge_transfers_values_and_conflicts_rollback(self):
        await self.save("B", {"Контракт": "Перенести"})
        await point_merge.merge("A", ["B"])
        self.assertEqual(await self.scalar("select value from point_category_field_values where point_id='A' and field_id=?", (self.fields["Контракт"],)), "Перенести")
        self.assertEqual(await self.scalar("select accounting_revision from points where id='A'"), 1)
        await self.save("C", {"Контракт": "Конфликт", "Местонахождение": "Не терять"})
        with self.assertRaisesRegex(ValueError, "Контракт"):
            await point_merge.merge("A", ["C"])
        self.assertEqual(await self.scalar("select count(*) from points where id='C'"), 1)
        self.assertEqual(await self.scalar("select value from point_category_field_values where point_id='C' and field_id=?", (self.fields["Местонахождение"],)), "Не терять")
        self.assertEqual(await self.scalar("select accounting_revision from points where id='A'"), 1)

    async def test_merge_late_conflict_rolls_back_entire_group(self):
        await self.save("B", {"Контракт": "Первый"})
        await self.save("C", {"Контракт": "Другой"})
        with self.assertRaises(ValueError):
            await point_merge.merge("A", ["B", "C"])
        self.assertEqual(await self.scalar("select count(*) from points"), 3)
        self.assertEqual(await self.scalar("select count(*) from point_category_field_values where point_id='A'"), 0)

    async def test_files_upload_read_and_delete_denied(self):
        r = await self.client.post("/api/point-files", data={"point_id": "A"}, files={"file": ("акт.txt", b"accounting document", "text/plain")})
        self.assertEqual(r.status_code, 201, r.text)
        fid = r.json()["id"]
        self.assertEqual((await self.client.get(f"/api/point-files/{fid}/file")).content, b"accounting document")
        self.assertEqual((await self.client.get(f"/api/point-files/{fid}/preview")).status_code, 200)
        self.assertEqual((await self.client.delete(f"/api/point-files/{fid}")).status_code, 403)
        self.assertEqual((await self.client.post("/api/point-files", data={"point_id": "MISSING"}, files={"file": ("a.txt", b"x")})).status_code, 404)
        self.user = self.role("accounting")
        self.user.permissions = ["points.view"]
        self.assertEqual((await self.client.get(f"/api/point-files/{fid}/file")).status_code, 403)

    async def test_photos_upload_read_and_delete_denied(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aQ1sAAAAASUVORK5CYII=")
        r = await self.client.post("/api/monitoring/photos/upload", data={"point_id": "A", "date": "2024-01-01"}, files={"file": ("a.png", png, "image/png")})
        self.assertEqual(r.status_code, 200, r.text)
        fid = r.json()["id"]
        self.assertEqual((await self.client.get(f"/api/monitoring/photos/{fid}/file")).content, png)
        r = await self.client.get("/api/monitoring/photos/point-items?point_id=A")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("uploaded_by", r.json()["days"][0]["photos"][0])
        self.assertEqual((await self.client.delete(f"/api/monitoring/photos/{fid}")).status_code, 403)
        self.assertEqual((await self.client.post("/api/monitoring/photos/upload", data={"point_id": "MISSING", "date": "2024-01-01"}, files={"file": ("a.png", png)})).status_code, 404)

    async def test_templates_keep_legacy_and_accounting_avoids_technical_scripts(self):
        self.app.include_router(routes_apk_page.router)
        self.app.include_router(routes_accounting_page.router)
        async def page_user(request, *keys):
            return self.user, None
        with patch.object(routes_apk_page, "page_user", page_user), patch.object(routes_accounting_page, "page_user", page_user):
            r = await self.client.get("/apk")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("static/js/accounting.js", r.text)
            self.assertIn("static/js/point_card.js", r.text)
            for name in ("apk_groups.js", "apk_check.js", "live_maps.js", "chat.js"):
                self.assertNotIn("static/js/" + name, r.text)
            self.assertEqual((await self.client.get("/accounting")).status_code, 200)
            self.user = self.role("user")
            r = await self.client.get("/apk")
            self.assertIn("static/js/point_card.js", r.text)
            self.assertIn("static/js/apk_check.js", r.text)
            self.assertNotIn("static/js/accounting.js", r.text)


if __name__ == "__main__":
    unittest.main()
