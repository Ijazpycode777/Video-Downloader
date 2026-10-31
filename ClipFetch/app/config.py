from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


# ClipFetch/
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

MIN_JWT_SECRET_BYTES = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = Field(default="", repr=False)
    JWT_SECRET_KEY: str = Field(default="", repr=False)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    INVITE_CODE: str = Field(default="", repr=False)

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        url = value.strip()
        if not url:
            raise ValueError("DATABASE_URL must not be empty.")

        if url.startswith("postgres://"):
            url = "postgresql+asyncpg://" + url.removeprefix("postgres://")
        elif url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url.removeprefix(
                "postgresql://"
            )

        try:
            parsed_url = make_url(url)
        except ArgumentError as exc:
            raise ValueError(
                "DATABASE_URL must be a valid PostgreSQL URL."
            ) from exc

        if parsed_url.drivername != "postgresql+asyncpg":
            raise ValueError(
                "DATABASE_URL must use the postgresql+asyncpg driver."
            )
        if not parsed_url.database:
            raise ValueError("DATABASE_URL must include a database name.")

        return url

    @field_validator("JWT_SECRET_KEY")
    @classmethod
    def validate_jwt_secret(cls, value: str) -> str:
        secret = value.strip()

        if len(secret.encode("utf-8")) < MIN_JWT_SECRET_BYTES:
            raise ValueError(
                "JWT_SECRET_KEY must contain at least 32 UTF-8 bytes."
            )

        return secret

    @field_validator("JWT_ALGORITHM")
    @classmethod
    def validate_jwt_algorithm(cls, value: str) -> str:
        if value.strip() != "HS256":
            raise ValueError("JWT_ALGORITHM is fixed to HS256.")
        return "HS256"

    @field_validator("ACCESS_TOKEN_EXPIRE_MINUTES")
    @classmethod
    def validate_token_expiry(cls, value: int) -> int:
        if value != 30:
            raise ValueError(
                "ACCESS_TOKEN_EXPIRE_MINUTES is fixed to 30 minutes."
            )
        return value

    @field_validator("INVITE_CODE")
    @classmethod
    def validate_invite_code(cls, value: str) -> str:
        invite_code = value.strip()

        if not invite_code:
            raise ValueError("INVITE_CODE must not be empty.")

        return invite_code


settings = Settings()
