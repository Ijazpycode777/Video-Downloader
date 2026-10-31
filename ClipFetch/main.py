import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from starlette.requests import Request
from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    OperationalError,
    SQLAlchemyError,
)

from app.engine import DatabaseUnavailableError, engine
from app.routers import auth, downloads
from app.services.storage import cleanup_expired_files


logger = logging.getLogger(__name__)
CLEANUP_INTERVAL_SECONDS = 30


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
    except SQLAlchemyError as exc:
        logger.exception("Database connection failed during startup.")
        raise RuntimeError(
            "ClipFetch could not start because the database is unavailable. "
            "Check DATABASE_URL and confirm PostgreSQL is accepting "
            "connections."
        ) from exc

    await _run_media_cleanup()
    cleanup_task = asyncio.create_task(_cleanup_loop())
    app.state.media_cleanup_task = cleanup_task
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        await engine.dispose()


async def _run_media_cleanup() -> None:
    try:
        await asyncio.to_thread(cleanup_expired_files)
    except Exception:
        logger.exception("Periodic media cleanup failed.")


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)

        await _run_media_cleanup()


app = FastAPI(
    title="ClipFetch API",
    description="An authenticated video and audio downloader API.",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(auth.router)
app.include_router(downloads.router)


def _database_error_response(exc: SQLAlchemyError) -> JSONResponse:
    if isinstance(exc, IntegrityError):
        response_status = status.HTTP_409_CONFLICT
        detail = (
            "The database rejected this operation because it conflicts "
            "with existing data."
        )
    elif isinstance(exc, DataError):
        response_status = status.HTTP_422_UNPROCESSABLE_ENTITY
        detail = (
            "The database rejected a value because it has an invalid "
            "format or is outside the allowed range."
        )
    elif isinstance(exc, OperationalError):
        response_status = status.HTTP_503_SERVICE_UNAVAILABLE
        detail = (
            "The database is temporarily unavailable. Check the database "
            "connection and retry."
        )
    else:
        response_status = status.HTTP_500_INTERNAL_SERVER_ERROR
        detail = (
            "The database could not complete this operation. Please retry "
            "or contact the service administrator."
        )

    return JSONResponse(
        status_code=response_status,
        content={"detail": detail},
    )


@app.exception_handler(DatabaseUnavailableError)
async def handle_database_unavailable(
    request: Request,
    exc: DatabaseUnavailableError,
) -> JSONResponse:
    logger.error(
        "Database session operation failed for %s (%s).",
        request.url.path,
        type(exc.database_error).__name__,
        exc_info=exc.database_error,
    )
    return _database_error_response(exc.database_error)


@app.exception_handler(SQLAlchemyError)
async def handle_database_error(
    request: Request,
    exc: SQLAlchemyError,
) -> JSONResponse:
    logger.error(
        "Unhandled database operation failed for %s.",
        request.url.path,
        exc_info=exc,
    )
    return _database_error_response(exc)


@app.exception_handler(Exception)
async def handle_unexpected_error(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    logger.error(
        "Unhandled application error for %s.",
        request.url.path,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": (
                "An unexpected server error occurred. Please retry the "
                "request; if it continues, contact the service administrator."
            )
        },
    )


@app.get("/")
async def root():
    return {
        "message": "Welcome to ClipFetch API.",
        "docs": "/docs",
    }


@app.get("/health")
async def health_check():
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")
    except OperationalError as exc:
        logger.exception("Health check could not reach the database.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Health check failed: PostgreSQL is not reachable. "
                "Verify the database service and DATABASE_URL."
            ),
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("Health check database query failed.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Health check failed: PostgreSQL rejected the connection "
                "or health query."
            ),
        ) from exc

    return {"status": "healthy", "database": "connected"}