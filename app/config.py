from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, overridable through environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    hospital_api_base_url: str = "https://hospital-directory.onrender.com"

    # CSV limits
    max_csv_rows: int = 20
    max_csv_bytes: int = 256 * 1024

    # Outbound calls to the Hospital Directory API
    # Upstream creates take ~5s each but run fully in parallel, so allow one
    # call per row: a 20-row file then finishes in about one round trip.
    max_concurrency: int = 20
    request_timeout_seconds: float = 30.0
    max_retries: int = 3
    retry_backoff_seconds: float = 0.5
    # The upstream runs on Render's free tier and sleeps when idle; the first
    # request after a sleep can take close to a minute.
    warmup_timeout_seconds: float = 90.0

    # In-memory batch store
    max_stored_batches: int = 500


@lru_cache
def get_settings() -> Settings:
    return Settings()
