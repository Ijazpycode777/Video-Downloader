import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status

from app.engine import engine
from app.routers import auth, downloads
from app.services.storage import cleanup_expired_files


logger = logging.getLogger(__name__)
CLEANUP_INTERVAL_SECONDS = 30


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.connect() as conn:
        await conn.exec_driver_sql("SELECT 1")

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
    except Exception as exc:
        logger.exception("Health check could not reach the database.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable.",
        ) from exc

    return {"status": "healthy", "database": "connected"}