"""Связь агента с сервером «Календаря работ» (HTTPS, адреса /api/agent/*; см. app/api/routes_ai_agents.py).

Агент всегда сам обращается к серверу (исходящее соединение): открывать порты на компьютере и в брандмауэре не нужно. Свободное место агента держит «длинный опрос» /next
открытым до 20 секунд — так сервер знает, что агент на связи и свободен, а задание приходит сразу в ответ. Доверие к серверу: системное хранилище сертификатов Windows
(туда ставится сертификат сервера, как для браузеров) и/или свой файл сертификата (кнопка «Доверять сертификату сервера» скачивает https://СЕРВЕР/ca.crt)."""
from __future__ import annotations

import hashlib
import re
import ssl
from pathlib import Path
from typing import Any, Optional

import httpx

from . import paths


class AuthLost(Exception):
    """Сервер не узнаёт агента (его удалили из списка): нужно подключиться заново по ключу."""


class Rejected(Exception):
    """Сервер отказал с понятной причиной (режим выключен, ключ не подходит…)."""


class NetworkError(Exception):
    """Нет связи с сервером."""


class CertError(NetworkError):
    """Связь есть, но сертификат сервера не проходит проверку (свой сертификат организации, который этому компьютеру ещё не доверен): нужно «Доверять сертификату сервера»."""


_KEY_RE = re.compile(r"lcai-[A-Za-z0-9_-]{8,}")
_CONN_RE = re.compile(r"^lcai://([^/\s]+)/(lcai-[A-Za-z0-9_-]{8,})/?$")


def parse_connection(text: str) -> tuple[str, str]:
    """Строка подключения из веб-панели «lcai://адрес/ключ» → (адрес сервера https://…, ключ). Понимает и привычные записи: «https://адрес ключ» (через пробел, перевод строки,
    запятую или «|»), и один ключ без адреса (адрес тогда пустой: возьмётся уже сохранённый). ValueError с понятным текстом — если ничего похожего на ключ нет."""
    raw = " ".join((text or "").replace(" ", " ").split()).strip().strip("\"'«»<>")
    if not raw:
        raise ValueError("Вставьте строку подключения из веб-панели (Настройки → ИИ → Ключи подключения)")
    m = _CONN_RE.match(raw)
    if m:
        return "https://" + m.group(1), m.group(2)
    key = _KEY_RE.search(raw)
    if not key:
        raise ValueError("Не нашёл в тексте ключ подключения (он начинается с «lcai-»). Скопируйте строку целиком: lcai://адрес/ключ")
    rest = (raw[:key.start()] + " " + raw[key.end():]).replace("|", " ").replace(",", " ").replace(";", " ")
    rest = re.sub(r"lcai://", "", rest)
    addr = next((w for w in rest.split() if re.match(r"^(https?://)?[A-Za-z0-9._-]+(:\d{1,5})?(/.*)?$", w) and "." in w or w.startswith(("http://", "https://"))), "")
    return (normalize_url(addr) if addr else ""), key.group(0)


def needs_trust(base_url: str, ca_file: str = "", timeout: float = 8.0) -> bool:
    """True — сервер отвечает, но его сертификат этому компьютеру не доверен (спросить пользователя про «Доверять сертификату»). Любая другая беда (нет сети…) — False: о ней расскажет обычное подключение."""
    try:
        with httpx.Client(verify=make_ssl_context(ca_file), timeout=timeout, trust_env=True) as c:
            c.get(base_url.rstrip("/") + "/ca.crt")
        return False
    except httpx.ConnectError as exc:
        return "CERTIFICATE_VERIFY_FAILED" in str(exc) or isinstance(exc.__cause__, ssl.SSLCertVerificationError)
    except Exception:  # noqa: BLE001
        return False


def normalize_url(url: str, allow_http: bool = False) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError("Укажите адрес сервера")
    if "://" not in url:
        url = "https://" + url
    if url.startswith("http://") and not allow_http:
        raise ValueError("Адрес должен начинаться с https:// — по http:// запросы с данными уходили бы по сети открытым текстом")
    if not url.startswith(("https://", "http://")):
        raise ValueError("Адрес сервера должен начинаться с https://")
    return url


def make_ssl_context(ca_file: str = "") -> ssl.SSLContext:
    """Системные сертификаты Windows плюс (если задан) свой файл сертификата сервера."""
    ctx = ssl.create_default_context()
    if ca_file and Path(ca_file).exists():
        ctx.load_verify_locations(cafile=ca_file)
    return ctx


def fetch_server_ca(base_url: str) -> tuple[Path, str]:
    """Скачать сертификат сервера (https://СЕРВЕР/ca.crt) БЕЗ проверки — доверие ещё не установлено — и вернуть (путь, отпечаток SHA-256). Отпечаток нужно сверить с тем, что показывает
    администратор (в браузере: замок → сертификат); сам файл сертификата не секрет."""
    with httpx.Client(verify=False, timeout=10.0, trust_env=False) as c:                # noqa: S501 — единственный запрос без проверки, ради получения сертификата
        r = c.get(base_url.rstrip("/") + "/ca.crt")
    if r.status_code != 200 or b"BEGIN CERTIFICATE" not in r.content:
        raise RuntimeError(f"Сервер не отдал сертификат (ответ {r.status_code})")
    path = paths.ca_file()
    path.write_bytes(r.content)
    der = ssl.PEM_cert_to_DER_cert(r.content.decode("ascii", errors="ignore").split("-----END CERTIFICATE-----")[0] + "-----END CERTIFICATE-----\n")
    return path, hashlib.sha256(der).hexdigest()


class ServerClient:
    def __init__(self, base_url: str, token: str = "", ca_file: str = "", allow_http: bool = False, version: str = "", hw_id: str = "") -> None:
        self.base = normalize_url(base_url, allow_http)
        self.token = token
        self.hw_id = hw_id                         # отпечаток железа: уходит с каждым запросом, сервер сверяет с привязкой компьютера
        self.ca_file = ca_file
        self.allow_http = allow_http
        self.version = version
        self._client = httpx.Client(base_url=self.base, verify=make_ssl_context(ca_file), timeout=httpx.Timeout(15.0, read=40.0), trust_env=True)

    def clone(self) -> "ServerClient":
        return ServerClient(self.base, self.token, self.ca_file, self.allow_http, self.version, self.hw_id)

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, path: str, *, json_body: Optional[dict] = None, params: Optional[dict] = None, timeout: Optional[float] = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if self.hw_id:
            headers["X-Agent-Hw"] = self.hw_id
        try:
            r = self._client.request(method, path, json=json_body, params=params, headers=headers, timeout=timeout or httpx.USE_CLIENT_DEFAULT)
        except httpx.ConnectError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc) or isinstance(exc.__cause__, ssl.SSLCertVerificationError):
                raise CertError("сертификат сервера не проверен — нажмите «Доверять сертификату сервера…»") from exc
            raise NetworkError(f"нет связи с сервером ({exc.__class__.__name__})") from exc
        except httpx.TimeoutException as exc:
            raise NetworkError("сервер долго не отвечает") from exc
        except httpx.HTTPError as exc:
            raise NetworkError(f"ошибка связи: {exc.__class__.__name__}") from exc
        if r.status_code == 401:
            raise AuthLost("сервер не узнаёт этот компьютер")
        if r.status_code in (403, 429):
            try:
                msg = r.json().get("detail", "")
            except ValueError:
                msg = ""
            raise Rejected(msg or f"сервер отказал ({r.status_code})")
        if r.status_code >= 400:
            raise NetworkError(f"сервер ответил {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise NetworkError("сервер вернул не JSON (возможно, адрес указан неверно)") from None

    def register(self, enroll_key: str, name: str, hardware: dict[str, Any]) -> dict[str, Any]:
        res = self._call("POST", "/api/agent/register", json_body={"enroll_key": enroll_key, "name": name, "hardware": hardware, "version": self.version, "hw_id": self.hw_id})
        self.token = res["token"]
        return res

    def models(self) -> list[dict[str, Any]]:
        """Файлы моделей, которые лежат на сервере и доступны для скачивания: [{file, size, sha256, title, …}]."""
        return self._call("GET", "/api/agent/models").get("models", [])

    def model_download_args(self, file: str) -> tuple[str, dict[str, str], ssl.SSLContext]:
        """Что нужно скачиванию файла модели с сервера: адрес, заголовки доступа (токен и железо этого компьютера) и настройки доверия к сертификату."""
        headers = {"Authorization": f"Bearer {self.token}"}
        if self.hw_id:
            headers["X-Agent-Hw"] = self.hw_id
        return f"{self.base}/api/agent/models/{file}", headers, make_ssl_context(self.ca_file)

    def heartbeat(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/api/agent/heartbeat", json_body={"state": state})

    def next_job(self, wait: float = 20.0) -> Optional[dict[str, Any]]:
        res = self._call("GET", "/api/agent/next", params={"wait": wait}, timeout=wait + 15.0)
        if res.get("paused"):
            raise Rejected("сервер просит не брать задания (режим выключен или компьютер отключён администратором)")
        return res.get("job")

    def result(self, job_id: str, result: dict[str, Any]) -> None:
        self._call("POST", f"/api/agent/result/{job_id}", json_body=result, timeout=60.0)
