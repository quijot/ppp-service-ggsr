from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"

    # NRCan CSRS-PPP
    csrs_user_email: str = "santiagonob@gmail.com"
    csrs_get_max: int = 60  # 60 × 10s = 10 minutos máximo de espera
    csrs_mode: str = "Static"
    csrs_ref: str = "ITRF"

    # Paths internos
    ppp_dir: str = "/app/ppp"  # donde viven calc2.py, pickles, etc.
    results_dir: str = "/tmp/ppp_results"

    # PostgreSQL (opcional por ahora, se activa cuando esté disponible)
    database_url: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
