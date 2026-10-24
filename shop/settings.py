from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ShopSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHOP_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./var/shop.db"
    # Where the bad_config fault points the app: a host that does not exist.
    bad_database_url: str = "postgresql+psycopg://shop:x@db-misconfigured.invalid:5432/shop"
    env: Literal["development", "staging", "production"] = "development"
    admin_token: SecretStr = SecretStr("")
    allow_faults: bool = False
    log_level: str = "INFO"
    log_file: str | None = None
    stats_interval_s: float = 10.0
    seed_data: bool = True


@lru_cache
def get_settings() -> ShopSettings:
    return ShopSettings()
