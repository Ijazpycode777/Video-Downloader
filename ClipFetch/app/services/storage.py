import errno
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import BinaryIO, Iterable


logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path(
    os.getenv("CLIPFETCH_DOWNLOAD_DIR", "downloads")
).resolve()
MEDIA_DIR = DOWNLOAD_DIR / ".clipfetch-media"
LOCK_PATH = MEDIA_DIR / ".storage.lock"
WORKING_DIRECTORY_PREFIX = ".clipfetch-working-"
RETENTION_SECONDS = 30 * 60

_MEDIA_FILENAME = re.compile(
    r"^[0-9a-f]{32}\.(?:mp3|mp4|mkv|webm|mov|m4v|avi|ts|ogv|flv|3gp)$"
)
_WORKING_DIRECTORY = re.compile(
    rf"^{re.escape(WORKING_DIRECTORY_PREFIX)}[0-9a-f]{{32}}$"
)
_LEGACY_EXTENSIONS = {
    ".mp3",
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".m4v",
    ".avi",
    ".ts",
    ".ogv",
    ".flv",
    ".3gp",
}


class StorageLease:
    """Hold a process-safe lock while media is being written or served."""

    def __init__(self, handle: BinaryIO, shared: bool) -> None:
        self._handle = handle
        self._shared = shared
        self._released = False

    def release(self) -> None:
        if self._released:
            return

        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(
                    self._handle.fileno(), msvcrt.LK_UNLCK, 1
                )
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._released = True

    def __enter__(self) -> "StorageLease":
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def acquire_storage_lease(
    *,
    shared: bool,
    blocking: bool = True,
) -> StorageLease | None:
    """Acquire a shared or exclusive lock for the managed storage directory."""

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+b")

    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            # msvcrt exposes exclusive byte-range locks only.
            msvcrt.locking(handle.fileno(), mode, 1)
            shared = False
        else:
            import fcntl

            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            if not blocking:
                mode |= fcntl.LOCK_NB
            fcntl.flock(handle.fileno(), mode)
    except OSError as exc:
        handle.close()
        if not blocking and exc.errno in {
            errno.EACCES,
            errno.EAGAIN,
            errno.EDEADLK,
            36,
        }:
            return None
        raise

    return StorageLease(handle, shared)


def resolve_download_path(raw_path: str | Path) -> Path | None:
    """Resolve a database path only if it names an app-managed media file."""

    path = Path(raw_path)
    if path.is_symlink():
        return None

    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    if not resolved.is_file():
        return None

    media_root = MEDIA_DIR.resolve()
    if (
        resolved.parent == media_root
        and _MEDIA_FILENAME.fullmatch(resolved.name)
    ):
        return resolved

    # Continue serving files created by earlier ClipFetch versions. They are
    # only eligible when a completed database job explicitly references them.
    download_root = DOWNLOAD_DIR.resolve()
    if (
        resolved.parent == download_root
        and resolved.suffix.lower() in _LEGACY_EXTENSIONS
    ):
        return resolved

    return None


def is_expired(path: Path, now: float | None = None) -> bool:
    current_time = time.time() if now is None else now
    return current_time - path.stat().st_mtime >= RETENTION_SECONDS


def delete_delivered_file(lease: StorageLease, path: Path) -> None:
    """Delete a delivered file after all active storage operations finish."""

    lease.release()

    delete_media_file(path)


def delete_media_file(path: Path) -> None:
    """Delete one validated app-managed file under the storage lock."""

    try:
        cleanup_lease = acquire_storage_lease(shared=False)
        if cleanup_lease is None:
            return

        with cleanup_lease:
            managed_path = resolve_download_path(path)
            if managed_path is not None:
                managed_path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        logger.exception("Could not remove a delivered media file.")


def cleanup_expired_files(
    legacy_paths: Iterable[str | Path] = (),
    *,
    now: float | None = None,
) -> int:
    """Remove expired app-managed media and abandoned temporary directories."""

    current_time = time.time() if now is None else now
    cutoff = current_time - RETENTION_SECONDS
    cleanup_lease = acquire_storage_lease(shared=False, blocking=False)
    if cleanup_lease is None:
        return 0

    removed = 0
    with cleanup_lease:
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)

        try:
            entries = tuple(MEDIA_DIR.iterdir())
        except OSError:
            logger.exception("Could not inspect the managed media directory.")
            entries = ()

        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if (
                    _MEDIA_FILENAME.fullmatch(entry.name)
                    and entry.is_file()
                    and entry.stat().st_mtime <= cutoff
                ):
                    entry.unlink()
                    removed += 1
                elif (
                    _WORKING_DIRECTORY.fullmatch(entry.name)
                    and entry.is_dir()
                    and entry.stat().st_mtime <= cutoff
                ):
                    shutil.rmtree(entry)
                    removed += 1
            except FileNotFoundError:
                continue
            except OSError:
                logger.exception("Could not remove an expired media entry.")

        for raw_path in legacy_paths:
            managed_path = resolve_download_path(raw_path)
            if (
                managed_path is None
                or managed_path.parent != DOWNLOAD_DIR.resolve()
            ):
                continue

            try:
                if managed_path.stat().st_mtime <= cutoff:
                    managed_path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
            except OSError:
                logger.exception("Could not remove an expired legacy media file.")

    return removed
