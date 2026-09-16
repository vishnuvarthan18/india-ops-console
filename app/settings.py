"""Configuration, entirely from environment variables.

Same rule as the core API: no secret has a usable default. An unset
SESSION_SECRET or ADMIN_PASSWORD_HASH fails at import rather than quietly
starting an admin console with a guessable way in.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "ops_console"
    postgres_password: str
    postgres_db: str = "india_data"
    db_pool_min: int = 1
    db_pool_max: int = 4

    admin_username: str = "admin"
    admin_password_hash: str
    session_secret: str
    session_max_age_hours: int = 12

    metrics_file: str = "/var/ops-metrics/metrics.json"
    repos_dir: str = "/repos"

    ops_console_env: str = "production"
    log_level: str = "INFO"

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
