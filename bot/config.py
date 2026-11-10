from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

from bot.paths import DATA_DIR, ENV_FILE

# pydantic-settings парсит .env только для полей Settings и не кладёт
# значения в os.environ — часть кода читает переменные через os.getenv()
# напрямую, без этого они вне docker-compose/явного экспорта были бы None.
# Путь берём явно (ENV_FILE), а не относительный '.env' — иначе при запуске
# из .exe он резолвился бы от текущей рабочей директории, а не от каталога exe.
load_dotenv(ENV_FILE)

DATA_DIR.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    vk_token: str
    # lan_calendar — единственный источник данных (задания, точки, отчёты,
    # фото); у бота больше нет собственной БД. bot_api_key должен совпадать
    # с тем, что lan_calendar сгенерировал в data/.bot_api_key на сервере
    # (см. app/config.py::_load_or_create_bot_api_key в lan_calendar).
    lan_calendar_api_url: str = "http://127.0.0.1:8000"
    bot_api_key: str

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding='utf-8',
        extra='ignore',
    )


settings = Settings()
