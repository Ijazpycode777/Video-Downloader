import logging
from collections.abc import AsyncIterator

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings


logger = logging.getLogger(__name__)


class DatabaseUnavailableError(Exception):
    """Raised when an application request cannot complete a DB operation."""

    def __init__(self, database_error: SQLAlchemyError) -> None:
        self.database_error = database_error
        super().__init__("Database session operation failed.")


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)
SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    try:
        async with SessionLocal() as session:
            yield session
    except SQLAlchemyError as exc:
        logger.exception("Database session operation failed.")
        raise DatabaseUnavailableError(exc) from exc