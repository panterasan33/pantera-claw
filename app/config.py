from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import List


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str
    
    # Database (will be converted to asyncpg in database.py)
    database_url: str = "postgresql://pantera:pantera@localhost:5432/pantera"
    
    # AI APIs
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    
    # App settings
    timezone: str = "Europe/London"
    morning_briefing_time: str = "08:00"
    default_snooze_minutes: int = 120
    
    # Nudge times (24h format)
    nudge_times: List[str] = ["09:00", "13:00", "18:00"]
    
    class Config:
        env_file = ".config/secrets.env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
