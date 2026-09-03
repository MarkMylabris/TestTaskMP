from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- core ---
    database_url: str = "postgresql+asyncpg://gamestore:gamestore@127.0.0.1:5432/gamestore_core"
    db_pool_size: int = 20
    db_max_overflow: int = 20
    log_level: str = "INFO"
    log_json: bool = True

    # --- suppliers (stubs) ---
    supplier_database_url: str = (
        "postgresql+asyncpg://gamestore:gamestore@127.0.0.1:5432/gamestore_suppliers"
    )
    supplier_a_url: str = "http://127.0.0.1:9101"
    supplier_b_url: str = "http://127.0.0.1:9102"

    # Клиентские таймауты к поставщику.
    supplier_connect_timeout: float = 1.0
    supplier_read_timeout: float = 2.0
    supplier_max_attempts: int = 3          # попыток на одного поставщика
    supplier_backoff_base: float = 0.2      # секунды
    supplier_backoff_max: float = 2.0

    # --- worker ---
    worker_enabled: bool = True             # встроенный воркер в процессе API
    worker_poll_interval: float = 0.2
    worker_batch: int = 10
    worker_concurrency: int = 8

    # Заказ считается "зависшим", если он не финализирован дольше этого времени.
    stuck_order_seconds: int = 30
    sweeper_interval: float = 5.0

    # Максимум попыток доставки в фоне, дальше - ручной разбор.
    delivery_max_attempts: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
