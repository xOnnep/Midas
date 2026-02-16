"""Application configuration."""
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    bot_token: str = ""  # Токен от @BotFather
    api_id: int = 0
    api_hash: str = ""
    default_daily_limit: int = 200
    sessions_dir: Path = Path("sessions")
    data_dir: Path = Path("data")

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///./{self.data_dir}/bot.db"

    class Config:
        env_file = ".env"
        extra = "ignore"


def get_settings() -> Settings:
    return Settings()


def ensure_dirs():
    """Create sessions and data directories."""
    Settings().sessions_dir.mkdir(parents=True, exist_ok=True)
    Settings().data_dir.mkdir(parents=True, exist_ok=True)
