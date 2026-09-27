"""Private, durable journal for one-video Yoto jobs."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import stat
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .youtube_time import parse_source_range, validate_source_range_ms

_JOB_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_CARD_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_MUTABLE_FIELDS = frozenset(
    {
        "stage",
        "status",
        "resume_from",
        "audio_status",
        "audio_source",
        "icon_status",
        "track_key",
        "chapter_key",
        "media_hash",
        "icon_media_id",
        "error",
        "warnings",
        "metadata",
        "duplicate_sources",
        "duplicate_source_intervals",
        "duplicate_existing_track_keys",
        "duplicate_approved",
        "mp3_path",
        "avatar_path",
        "remote_verified",
        "write_intent",
        "diagnostic",
        "audio_added_at",
        "icon_assigned",
    }
)
_TERMINAL_STATUSES = frozenset({"complete", "completed", "succeeded", "cancelled"})
# Resetting a job that never wrote to Yoto so its worker redoes it from source.
REQUEUE_FIELDS: dict[str, Any] = {
    "status": "queued", "stage": "queued", "resume_from": "source",
    "error": None, "diagnostic": None, "write_intent": None, "warnings": [],
}
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


def nothing_written(job: dict[str, Any]) -> bool:
    """True when this job cannot have changed a Yoto card.

    ``add_mp3`` journals the reserved track before any upload or card POST, so a
    job without one never reached Yoto. A job that may still be running also needs
    no pending write intent: its reservation could be moments away.
    """
    if any(job.get(field) for field in ("track_key", "chapter_key", "media_hash")):
        return False
    return job.get("status") in {"failed", "cancelled"} or not job.get("write_intent")


class JobStoreError(RuntimeError):
    """The private job journal is unsafe or contains an invalid record."""


class JobStore:
    """Atomic, mode-restricted JSON job journal rooted outside the source tree.

    ``submit(card_id, video_id, dry_run, start_time, end_time)`` returns the
    durable job dictionary; its ``(card_id, video_id, dry_run, start_ms, end_ms)``
    identity is canonical and range-sensitive. ``get`` reads by job ID, ``update(job_id, **fields)``
    atomically replaces mutable fields, and ``recoverable_jobs()`` returns
    unfinished jobs in creation order.
    """

    def __init__(self, root: str | Path) -> None:
        if not isinstance(root, (str, Path)) or not os.fspath(root):
            raise ValueError("A job-store root path is required")
        self.root = Path(os.path.abspath(os.fspath(root)))
        if self.root == Path("/"):
            raise ValueError("The job-store root must not be the filesystem root")
        project_root = Path(__file__).resolve().parents[2]
        if self.root == project_root or project_root in self.root.parents:
            raise ValueError("The job-store root must be outside the public repository")
        self._lock = threading.RLock()
        self._root_fd = self._open_root(self.root)

    def submit(
        self, card_id: str, video_id: str, dry_run: bool,
        *, artist: str | None = None, song_name: str | None = None,
        start_time: str | None = None, end_time: str | None = None,
    ) -> dict[str, Any]:
        """Admit or return the job for an exact card/video/mode/source interval."""
        card_id = self._validate_identifier(card_id, _CARD_ID_RE, "card_id")
        video_id = self._validate_identifier(video_id, _VIDEO_ID_RE, "video_id")
        if not isinstance(dry_run, bool):
            raise TypeError("dry_run must be a bool")
        start_ms, end_ms = parse_source_range(start_time, end_time)
        if (artist is None) != (song_name is None):
            raise ValueError("artist and song_name must be provided together")
        if artist is not None and song_name is not None:
            if any(
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 200
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                for value in (artist, song_name)
            ):
                raise ValueError("artist and song_name must be nonempty, printable, and at most 200 characters")
            artist, song_name = artist.strip(), song_name.strip()

        with self._serialized():
            for existing_id in self._job_ids():
                existing = self._read_job(existing_id)
                if (
                    existing["card_id"] == card_id
                    and existing["video_id"] == video_id
                    and existing["dry_run"] is dry_run
                    and (existing.get("start_ms"), existing.get("end_ms")) == (start_ms, end_ms)
                    and existing["status"] != "cancelled"
                ):
                    if (existing.get("artist"), existing.get("song_name")) != (artist, song_name):
                        raise ValueError(
                            "Existing job has different artist/song_name overrides; "
                            "cancel_youtube_job can release it if nothing was written to Yoto"
                        )
                    if existing["status"] == "failed" and nothing_written(existing):
                        existing.update(copy.deepcopy(REQUEUE_FIELDS), updated_at=self._now())
                        self._write_job(existing)
                    return existing

            now = self._now()
            job = {
                "job_id": uuid.uuid4().hex,
                "card_id": card_id,
                "video_id": video_id,
                "dry_run": dry_run,
                "artist": artist,
                "song_name": song_name,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "stage": "queued",
                "status": "queued",
                "resume_from": "source",
                "audio_status": "pending",
                "icon_status": "pending",
                "created_at": now,
                "updated_at": now,
            }
            self._write_job(job)
            return copy.deepcopy(job)

    def get(self, job_id: str) -> dict[str, Any]:
        """Return a detached copy of one job, raising KeyError if it is absent."""
        job_id = self._validate_job_id(job_id)
        with self._serialized():
            return self._read_job(job_id)

    def update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        """Atomically persist mutable state fields and return the updated job."""
        updated = self.update_if(job_id, lambda _job: True, **fields)
        assert updated is not None
        return updated

    def update_if(
        self, job_id: str, condition: Callable[[dict[str, Any]], bool], **fields: Any,
    ) -> dict[str, Any] | None:
        """Like ``update``, but only when ``condition(current_job)`` holds; else ``None``.

        The check and write are one serialized step, so a cancel and a worker's
        write intent cannot interleave.
        """
        job_id = self._validate_job_id(job_id)
        if not fields:
            raise ValueError("At least one job field must be updated")
        forbidden = set(fields) - _MUTABLE_FIELDS
        if forbidden:
            raise ValueError(f"Unsupported or immutable job fields: {', '.join(sorted(forbidden))}")
        try:
            json.dumps(fields, allow_nan=False)
        except (TypeError, ValueError):
            raise TypeError("Job updates must contain finite JSON values") from None

        with self._serialized():
            job = self._read_job(job_id)
            if not condition(job):
                return None
            job.update(copy.deepcopy(fields))
            job["updated_at"] = self._now()
            self._write_job(job)
            return copy.deepcopy(job)

    def all_jobs(self) -> list[dict[str, Any]]:
        """Return all private provenance records, including completed jobs."""
        with self._serialized():
            jobs = [self._read_job(job_id) for job_id in self._job_ids()]
        return sorted(
            jobs,
            key=lambda job: (job["created_at"], job["job_id"]),
        )

    def recoverable_jobs(self) -> list[dict[str, Any]]:
        """Return every non-terminal job, including partial audio/icon outcomes."""
        return [
            job for job in self.all_jobs() if job.get("status") not in _TERMINAL_STATUSES
        ]

    @contextmanager
    def _serialized(self) -> Iterator[None]:
        with self._lock:
            try:
                lock_fd = os.open(
                    ".lock",
                    os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=self._root_fd,
                )
            except OSError as exc:
                raise JobStoreError("Could not safely open the job-store lock") from exc
            try:
                info = os.fstat(lock_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise JobStoreError("The job-store lock is not a private regular file")
                os.fchmod(lock_fd, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)

    @staticmethod
    def _open_root(path: Path) -> int:
        """Walk an absolute path with directory descriptors and no-follow opens."""
        fd = os.open("/", _DIR_FLAGS)
        components = path.parts[1:]
        try:
            for index, component in enumerate(components):
                created = False
                try:
                    child_fd = os.open(component, _DIR_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    try:
                        os.mkdir(component, 0o700, dir_fd=fd)
                        created = True
                    except FileExistsError:
                        pass
                    child_fd = os.open(component, _DIR_FLAGS, dir_fd=fd)
                if created or index == len(components) - 1:
                    os.fchmod(child_fd, 0o700)
                os.close(fd)
                fd = child_fd
            return fd
        except OSError as exc:
            os.close(fd)
            raise JobStoreError("The job-store path contains an unsafe or invalid directory") from exc

    def _job_ids(self) -> list[str]:
        try:
            names = os.listdir(self._root_fd)
        except OSError as exc:
            raise JobStoreError("Could not list the private job store") from exc
        job_ids = []
        for name in names:
            if name.endswith(".json"):
                candidate = name[:-5]
                if not _JOB_ID_RE.fullmatch(candidate):
                    raise JobStoreError("The job store contains an invalid record filename")
                job_ids.append(candidate)
        return sorted(job_ids)

    def _read_job(self, job_id: str) -> dict[str, Any]:
        filename = f"{job_id}.json"
        try:
            fd = os.open(filename, _FILE_FLAGS, dir_fd=self._root_fd)
        except FileNotFoundError:
            raise KeyError(job_id) from None
        except OSError as exc:
            raise JobStoreError("Could not safely open a job record") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise JobStoreError("A job record is not a regular private file")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
                try:
                    job = json.load(stream)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise JobStoreError("A job record contains invalid JSON") from None
            self._validate_record(job, job_id)
            return job
        finally:
            os.close(fd)

    def _write_job(self, job: dict[str, Any]) -> None:
        filename = f"{job['job_id']}.json"
        temporary = f".tmp-{uuid.uuid4().hex}"
        try:
            encoded = json.dumps(job, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            data = (encoded + "\n").encode("utf-8")
        except (TypeError, ValueError):
            raise TypeError("Job record must contain finite JSON values") from None
        fd = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self._root_fd,
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
            os.replace(temporary, filename, src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
            os.fsync(self._root_fd)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self._root_fd)
            except FileNotFoundError:
                pass

    @staticmethod
    def _validate_identifier(value: str, pattern: re.Pattern[str], label: str) -> str:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError(f"{label} has an invalid format")
        return value

    @staticmethod
    def _validate_job_id(job_id: str) -> str:
        if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
            raise ValueError("job_id has an invalid format")
        return job_id

    @staticmethod
    def _validate_record(job: Any, expected_id: str) -> None:
        required = {
            "job_id",
            "card_id",
            "video_id",
            "dry_run",
            "stage",
            "status",
            "resume_from",
            "audio_status",
            "icon_status",
            "created_at",
            "updated_at",
        }
        if (
            not isinstance(job, dict)
            or not required.issubset(job)
            or job.get("job_id") != expected_id
            or not isinstance(job.get("card_id"), str)
            or not isinstance(job.get("video_id"), str)
            or not isinstance(job.get("dry_run"), bool)
            or (job.get("artist") is None) != (job.get("song_name") is None)
            or any(
                not isinstance(job.get(field), str) or not job[field] or len(job[field]) > 200
                for field in ("artist", "song_name") if job.get(field) is not None
            )
            or any(not isinstance(job.get(field), str) for field in (
                "stage", "status", "resume_from", "audio_status", "icon_status", "created_at", "updated_at"
            ))
        ):
            raise JobStoreError("A job record does not match the expected schema")
        try:
            validate_source_range_ms(job.get("start_ms"), job.get("end_ms"))
        except ValueError:
            raise JobStoreError("A job record has an invalid source interval") from None

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
