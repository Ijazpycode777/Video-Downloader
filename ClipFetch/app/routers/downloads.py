import asyncio
import logging
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Message, Receive, Scope, Send

from app.engine import get_db
from app.models import DownloadJob, User
from app.security import get_current_user
from app.services.storage import (
    StorageLease,
    acquire_storage_lease,
    delete_delivered_file,
    delete_media_file,
    is_expired,
    resolve_download_path,
)
from app.services.video import VideoDownloadError, download_media


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/downloads", tags=["Downloads"])

_MEDIA_RESPONSE_CONTENT = {
    "video/mp4": {"schema": {"type": "string", "format": "binary"}},
    "video/webm": {"schema": {"type": "string", "format": "binary"}},
    "video/x-matroska": {
        "schema": {"type": "string", "format": "binary"}
    },
    "video/quicktime": {"schema": {"type": "string", "format": "binary"}},
    "video/x-m4v": {"schema": {"type": "string", "format": "binary"}},
    "video/x-msvideo": {"schema": {"type": "string", "format": "binary"}},
    "video/mp2t": {"schema": {"type": "string", "format": "binary"}},
    "video/ogg": {"schema": {"type": "string", "format": "binary"}},
    "video/x-flv": {"schema": {"type": "string", "format": "binary"}},
    "video/3gpp": {"schema": {"type": "string", "format": "binary"}},
    "audio/mpeg": {"schema": {"type": "string", "format": "binary"}},
    "application/octet-stream": {
        "schema": {"type": "string", "format": "binary"}
    },
}


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
            raise ValueError(f"Invalid quality for {self.media_type}.")

        return self


class LeasedFileResponse(FileResponse):
    def __init__(self, *args, lease: StorageLease, **kwargs) -> None:
        self._storage_lease = lease
        super().__init__(*args, **kwargs)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        complete_body = False
        successful_status = False

        async def tracked_send(message: Message) -> None:
            nonlocal complete_body, successful_status
            await send(message)

            if message["type"] == "http.response.start":
                successful_status = message["status"] in {200, 201}
            elif (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                complete_body = True

        response_scope = dict(scope)
        extensions = dict(response_scope.get("extensions", {}))
        extensions.pop("http.response.pathsend", None)
        response_scope["extensions"] = extensions

        try:
            await super().__call__(response_scope, receive, tracked_send)
            if successful_status and complete_body:
                await asyncio.to_thread(
                    delete_delivered_file,
                    self._storage_lease,
                    Path(self.path),
                )
        finally:
            self._storage_lease.release()


def _file_response(
    job: DownloadJob,
    *,
    status_code: int = status.HTTP_200_OK,
) -> FileResponse:
    if not job.file_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This download file has expired or was already delivered.",
        )

    try:
        lease = acquire_storage_lease(shared=True, blocking=False)
    except OSError as exc:
        logger.exception("Could not lock media storage for delivery.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Media storage is temporarily unavailable.",
        ) from exc

    if lease is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Media storage is busy. Please retry the request.",
        )

    file_path = resolve_download_path(job.file_path)
    if file_path is None:
        lease.release()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This download file has expired or was already delivered.",
        )

    try:
        if is_expired(file_path):
            lease.release()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="This download file has expired or was already delivered.",
            )
    except OSError as exc:
        lease.release()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Download file not found.",
        ) from exc

    filename = Path(job.filename or file_path.name).name
    media_type = {
        ".mp3": "audio/mpeg",
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".m4v": "video/x-m4v",
        ".avi": "video/x-msvideo",
        ".ts": "video/mp2t",
        ".ogv": "video/ogg",
        ".flv": "video/x-flv",
        ".3gp": "video/3gpp",
    }.get(file_path.suffix.lower())

    return LeasedFileResponse(
        path=file_path,
        filename=filename,
        media_type=media_type or "application/octet-stream",
        status_code=status_code,
        content_disposition_type="attachment",
        headers={"X-Download-Job-ID": str(job.id)},
        lease=lease,
    )


async def _record_failed_job(
    db: AsyncSession,
    job_id: UUID,
    error_message: str,
) -> None:
    try:
        await db.rollback()
        failed_job = await db.get(
            DownloadJob, job_id, populate_existing=True
        )
        if failed_job is not None:
            failed_job.status = "failed"
            failed_job.error_message = error_message
            await db.commit()
    except Exception:
        logger.exception("Could not record failed download job %s.", job_id)
        try:
            await db.rollback()
        except Exception:
            logger.exception("Could not roll back failed download job update.")


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    response_class=FileResponse,
    responses={
        status.HTTP_201_CREATED: {
            "description": (
                "The completed media file as an attachment. "
                "The job ID is returned in X-Download-Job-ID."
            ),
            "content": _MEDIA_RESPONSE_CONTENT,
            "headers": {
                "Content-Disposition": {
                    "description": "Attachment filename for the media file.",
                    "schema": {"type": "string"},
                },
                "X-Download-Job-ID": {
                    "description": "ID of the persisted download job.",
                    "schema": {"type": "string", "format": "uuid"},
                },
            },
        }
    },
)
async def create_download(
    data: DownloadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
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

    result: dict[str, object] | None = None

    try:
        result = await download_media(
            url=str(data.url),
            media_type=data.media_type,
            quality=data.quality,
        )

        job.status = "completed"
        job.title = str(result["title"])
        job.filename = str(result["filename"])
        job.file_path = str(result["filepath"])
        job.error_message = None

        await db.commit()
        await db.refresh(job)
    except VideoDownloadError as exc:
        await _record_failed_job(db, job_id, str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        await _record_failed_job(
            db,
            job_id,
            "An unexpected download error occurred.",
        )
        if result is not None:
            await asyncio.to_thread(
                delete_media_file,
                Path(str(result["filepath"])),
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The download could not be completed.",
        ) from exc

    return _file_response(
        job,
        status_code=status.HTTP_201_CREATED,
    )


@router.get(
    "/{job_id}/file",
    response_class=FileResponse,
    responses={
        status.HTTP_200_OK: {
            "description": "The completed media file as an attachment.",
            "content": _MEDIA_RESPONSE_CONTENT,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "The job or its expired/delivered file was not found."
        },
        status.HTTP_409_CONFLICT: {
            "description": "The job has not completed."
        },
    },
)
async def retrieve_download(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    job = await db.get(DownloadJob, job_id)

    # Do not disclose whether another user's job exists.
    if job is None or job.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Download not found.",
        )

    if job.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This download is not available yet.",
        )

    return _file_response(job)
