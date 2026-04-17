from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BUGZILLA_URL = "https://bugs.koha-community.org/bugzilla3"
KOHA_GITHUB_MIRROR = "Koha-community/Koha"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="KOHA_TRIAGE_",
        extra="ignore",
    )

    anthropic_api_key: str | None = None
    github_token: str | None = None
    db_path: Path = Path("./data/koha-triage.db")
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    classification_model: str = "claude-opus-4-6"

    # Google OAuth (optional)
    google_client_id: str | None = None
    google_client_secret: str | None = None
    session_secret: str = "change-me-in-production"
    allowed_domains: str = "bywatersolutions.com"


settings = Settings()
