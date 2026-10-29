import mimetypes
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine import get_db
from app.models import DownloadJob, User
from app.security import get_current_user
from app.services.video import (
    DOWNLOAD_DIR,
    VideoDownloadError,
    download_media,
)


router = APIRouter(prefix="/downloads", tags=["Downloads"])


class DownloadRequest(BaseModel):
    url: HttpUrl
    media_type: Literal["video", "mp3"] = "video"
    quality: str = "best"

    @model_validator(mode="after")
    def validate_quality(self):
        allowed = (
            {"360", "480", "720", "1080", "best"}
            if self.media_type == "video"
            else {"128", "192", "320", "best"}
        )

        if self.quality not in allowed:
            raise ValueError(
                f"Invalid quality for {self.media_type}."
            )

        return self


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_download(
    data: DownloadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = DownloadJob(
        user_id=current_user.id,
        video_url=str(data.url),
        status="pending",
        media_type=data.media_type,
        quality=data.quality,
    )

    db.add(job)
    await db.commit()
    await db.refresh(job)

    job_id = job.id

    job.status = "processing"
    await db.commit()

    try:
        result = await download_media(
            url=str(data.url),
            media_type=data.media_type,
            quality=data.quality,
        )

        job.status = "completed"
        job.title = result["title"]
        job.filename = result["filename"]
        job.file_path = result["filepath"]
        job.error_message = None

        await db.commit()
        await db.refresh(job)

        return {
            "message": "Download successful!",
            "job_id": str(job.id),
            "status": job.status,
            "title": job.title,
            "filename": job.filename,
            "media_type": data.media_type,
            "quality_requested": data.quality,
            "duration": result["duration"],
            "file_size": result["file_size"],
            "download_url": f"/downloads/{job.id}/file",
        }

    except VideoDownloadError as exc:
        await db.rollback()

        try:
            failed_job = await db.get(
                DownloadJob, job_id, populate_existing=True
            )
            if failed_job is not None:
                failed_job.status = "failed"
                failed_job.error_message = str(exc)
                await db.commit()
        except Exception:
            await db.rollback()

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        await db.rollback()

        try:
            failed_job = await db.get(
                DownloadJob, job_id, populate_existing=True
            )
            if failed_job is not None:
                failed_job.status = "failed"
                failed_job.error_message = (
                    "An unexpected download error occurred."
                )
                await db.commit()
        except Exception:
            await db.rollback()

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The download could not be completed.",
        ) from exc
@router.get("/{job_id}/file")
async def retrieve_download(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(DownloadJob, job_id)

    # Do not disclose whether another user's job exists.
    if job is None or job.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Download not found.",
        )

    if job.status != "completed" or not job.file_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This download is not available yet.",
        )

    root = DOWNLOAD_DIR.resolve()
    file_path = Path(job.file_path).resolve()

    # Never allow a database path to expose files outside downloads/.
    if not file_path.is_relative_to(root):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Download file not found.",
        )

    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Download file not found.",
        )

    media_type, _ = mimetypes.guess_type(file_path.name)

    return FileResponse(
        path=file_path,
        filename=job.filename or file_path.name,
        media_type=media_type or "application/octet-stream",
        content_disposition_type="attachment",
    )