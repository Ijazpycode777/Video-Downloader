from pathlib import Path
import warnings

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Project root: Video-Downloader/ClipFetch/
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

SUPPORTED_JWT_ALGORITHMS = ("HS256", "HS384", "HS512")
MIN_JWT_SECRET_BYTES = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    INVITE_CODE: str

    @field_validator("JWT_SECRET_KEY")
    @classmethod
    def validate_jwt_secret(cls, value: str) -> str:
        secret = value.strip()

        if not secret:
            raise ValueError("JWT_SECRET_KEY must not be empty.")

        if len(secret.encode("utf-8")) < MIN_JWT_SECRET_BYTES:
            warnings.warn(
                "JWT_SECRET_KEY is shorter than 32 bytes. "
                "Use a cryptographically random secret of at least "
                "32 bytes.",
                UserWarning,
                stacklevel=2,
            )

        return secret

    @field_validator("JWT_ALGORITHM")
    @classmethod
    def validate_jwt_algorithm(cls, value: str) -> str:
        algorithm = value.strip().upper()

        if algorithm not in SUPPORTED_JWT_ALGORITHMS:
            raise ValueError(
                "JWT_ALGORITHM must be one of: "
                + ", ".join(SUPPORTED_JWT_ALGORITHMS)
            )

        return algorithm

    @field_validator("ACCESS_TOKEN_EXPIRE_MINUTES")
    @classmethod
    def validate_token_expiry(cls, value: int) -> int:
        if value < 1:
            raise ValueError(
                "ACCESS_TOKEN_EXPIRE_MINUTES must be at least 1."
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
