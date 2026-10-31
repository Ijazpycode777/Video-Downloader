import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl, model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Message, Receive, Scope, Send

from app.engine import get_db
from app.models import DownloadRecord, User
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


class DownloadHistoryItem(BaseModel):
    filename: str
    downloaded_at: datetime


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
    raw_path: Path,
    filename: str,
    *,
    status_code: int = status.HTTP_200_OK,
) -> LeasedFileResponse:
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

    lease_transferred = False
    try:
        file_path = resolve_download_path(raw_path)
        if file_path is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="This download file has expired or was already delivered.",
            )
        try:
            if is_expired(file_path):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        "This download file has expired or was already "
                        "delivered."
                    ),
                )
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Download file not found.",
            ) from exc

        filename = Path(filename).name
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

        response = LeasedFileResponse(
            path=file_path,
            filename=filename,
            media_type=media_type or "application/octet-stream",
            status_code=status_code,
            content_disposition_type="attachment",
            lease=lease,
        )
        lease_transferred = True
        return response
    finally:
        if not lease_transferred:
            lease.release()


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    response_class=FileResponse,
    responses={
        status.HTTP_201_CREATED: {
            "description": "The completed media file as an attachment.",
            "content": _MEDIA_RESPONSE_CONTENT,
            "headers": {
                "Content-Disposition": {
                    "description": "Attachment filename for the media file.",
                    "schema": {"type": "string"},
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
    try:
        result = await download_media(
            url=str(data.url),
            media_type=data.media_type,
            quality=data.quality,
        )
    except VideoDownloadError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected error while creating a download.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The download could not be completed.",
        ) from exc

    media_path = Path(str(result["filepath"]))
    filename = Path(str(result["filename"])).name
    if not filename or filename in {".", ".."}:
        await asyncio.to_thread(delete_media_file, media_path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The downloader returned an invalid filename.",
        )

    try:
        response = _file_response(
            media_path,
            filename,
            status_code=status.HTTP_201_CREATED,
        )
    except Exception:
        await asyncio.to_thread(delete_media_file, media_path)
        raise

    try:
        db.add(
            DownloadRecord(
                user_id=current_user.id,
                filename=filename,
            )
        )
        await db.commit()
    except SQLAlchemyError as exc:
        logger.exception("Could not record the completed download.")
        try:
            await db.rollback()
        except SQLAlchemyError:
            logger.exception("Could not roll back download history.")
        response._storage_lease.release()
        await asyncio.to_thread(delete_media_file, media_path)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The download could not be recorded. Please retry.",
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected error while recording download history.")
        try:
            await db.rollback()
        except Exception:
            logger.exception("Could not roll back download history.")
        response._storage_lease.release()
        await asyncio.to_thread(delete_media_file, media_path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The download could not be recorded.",
        ) from exc

    return response


@router.get(
    "/history",
    response_model=list[DownloadHistoryItem],
)
async def list_download_history(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DownloadHistoryItem]:
    result = await db.execute(
        select(DownloadRecord)
        .where(DownloadRecord.user_id == current_user.id)
        .order_by(
            DownloadRecord.downloaded_at.desc(),
            DownloadRecord.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return [
        DownloadHistoryItem(
            filename=record.filename,
            downloaded_at=record.downloaded_at,
        )
        for record in result.scalars().all()
    ]
