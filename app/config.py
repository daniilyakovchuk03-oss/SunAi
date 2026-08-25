from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    MODE: str = "mock"

    WAZZUP_API_KEY: str = ""
    WAZZUP_CHANNEL_ID: str = ""
    WAZZUP_BASE_URL: str = "https://api.wazzup24.com/v3"
    AI_CRM_USER_ID: str = "ai-agent"

    AMO_SUBDOMAIN: str = ""
    AMO_ACCESS_TOKEN: str = ""

    LLM_PROVIDER: str = "stub"
    ANTHROPIC_API_KEY: str = ""
    LLM_MODEL: str = "claude-haiku-4-5-20251001"

    DB_PATH: str = "./data/agent.db"
    RULES_PATH: str = "./rules.yaml"
    REPLY_DELAY_SECONDS: int = 8
    WEBHOOK_SECRET: str = "change-me"
    TZ: str = "Asia/Almaty"

    @property
    def is_mock(self) -> bool:
        return self.MODE.lower() == "mock"

    def ensure_dirs(self) -> None:
        Path(self.DB_PATH).parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
