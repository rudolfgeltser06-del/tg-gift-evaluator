from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    BOT_TOKEN: str
    GEMINI_API_KEY: str
    DATABASE_URL: str
    ADMIN_IDS: str = ""  # ID админов через запятую, например: "123456789,987654321"
    TONAPI_KEY: str = ""  # Опционально, для расширенных лимитов TonAPI

    # Лимиты по умолчанию (меняются из админки)
    DEFAULT_DAILY_EVAL_LIMIT: int = 5
    DEFAULT_DAILY_CHAT_LIMIT: int = 15

    # Настройки Webhook (для бесплатного хостинга Render / Koyeb)
    WEBHOOK_URL: str = ""
    PORT: int = 8080

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def admin_id_list(self) -> list[int]:
        if not self.ADMIN_IDS.strip():
            return []
        return [int(x.strip()) for x in self.ADMIN_IDS.split(",") if x.strip().isdigit()]

    @property
    def async_database_url(self) -> str:
        url = self.DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://") and "+asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


settings = Settings()
