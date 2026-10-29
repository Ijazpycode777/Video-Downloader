import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.engine import SessionLocal, engine
from app.models import DownloadJob
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
    legacy_paths: list[str] = []
    try:
        async with SessionLocal() as db:
            result = await db.execute(
                select(DownloadJob.file_path).where(
                    DownloadJob.status == "completed",
                    DownloadJob.file_path.is_not(None),
                )
            )
            legacy_paths = [
                file_path
                for file_path in result.scalars()
                if file_path is not None
            ]
    except SQLAlchemyError:
        logger.exception(
            "Could not load legacy media paths for cleanup."
        )

    try:
        await asyncio.to_thread(cleanup_expired_files, legacy_paths)
    except Exception:
        logger.exception("Periodic media cleanup failed.")


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)

        await _run_media_cleanup()


app = FastAPI(title= "ClipFetch API", description= "A Private Video Downloader ", version= "0.0.1", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(downloads.router)

@app.get("/")
async def root():
    return {"message": "Welcome to ClipFetch API!", "Status": "Running"}

@app.get ("/health")
async def health_check():
    return {"status": "healthy"}