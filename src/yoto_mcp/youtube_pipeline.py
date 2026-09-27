"""Background YouTube preparation and durable MCP job status."""

from __future__ import annotations

import inspect
import re
import unicodedata
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any

import httpx

from .yoto import YotoAPIError
from .youtube_jobs import JobStore
from .youtube_source import YouTubeSourceError, parse_youtube_id, prepare_youtube

_DIAGNOSTIC_DETAILS = {
    "local_preflight": (
        "local_preflight_failed", "Prepared audio files failed local validation.",
    ),
    "upload_url": ("upload_url_failed", "Audio upload URL request failed."),
    "upload_put": ("upload_put_failed", "Audio file upload failed."),
    "transcode": ("transcode_failed", "Audio transcoding failed."),
}


class YouTubeCoordinator:
    """Launch bounded source work without blocking the stdio MCP dispatch loop."""

    def __init__(
        self,
        client: Any,
        store: JobStore,
        upload_root: Path,
        *,
        allow_writes: bool,
        prepare: Callable[..., dict[str, Any]] = prepare_youtube,
    ) -> None:
        self.client = client
        self.store = store
        self.upload_root = Path(upload_root)
        self.allow_writes = allow_writes
        self.prepare = prepare
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="yoto-youtube")
        self._lock = Lock()
        self._card_locks: dict[str, Lock] = {}
        self._futures: dict[str, Future[None]] = {}

    def submit(
        self, card_id: str, video_id: str, *, dry_run: bool = True,
        artist: str | None = None, song_name: str | None = None,
        start_time: str | None = None, end_time: str | None = None,
    ) -> dict[str, Any]:
        if not dry_run and not self.allow_writes:
            raise ValueError("YOTO_ALLOW_WRITES=1 is required for a YouTube write job")
        video_id = parse_youtube_id(video_id)
        with self._lock:
            job = self.store.submit(
                card_id, video_id, dry_run, artist=artist, song_name=song_name,
                start_time=start_time, end_time=end_time,
            )
            if job["status"] == "queued" and job["job_id"] not in self._futures:
                self._futures[job["job_id"]] = self._pool.submit(self._run, job)
        return job

    def get(self, job_id: str) -> dict[str, Any]:
        return self.store.get(job_id)

    def resume(
        self, job_id: str, *, approve_duplicate: bool = False,
        icon_media_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(approve_duplicate, bool):
            raise TypeError("approve_duplicate must be a bool")
        if icon_media_id is not None and (
            not isinstance(icon_media_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", icon_media_id)
        ):
            raise ValueError("icon_media_id has an invalid format")
        with self._lock:
            job = self.store.get(job_id)
            if not job["dry_run"] and not self.allow_writes:
                raise ValueError("YOTO_ALLOW_WRITES=1 is required to resume a YouTube write job")
            if icon_media_id is not None:
                if (
                    job["status"] != "audio_added_icon_pending"
                    or job.get("audio_status") != "verified"
                    or job.get("icon_media_id")
                    or job.get("write_intent") != "upload_icon"
                ):
                    raise ValueError("icon_media_id can resolve only an uncertain icon upload")
                job = self.store.update(
                    job_id, icon_media_id=icon_media_id, icon_status="uploaded",
                    write_intent=None, error=None,
                )
            if job["status"] == "complete":
                return job
            source_only = (
                job["status"] in {"queued", "running"}
                and job.get("stage") in {"queued", "source"}
                and job.get("resume_from") == "source"
                and not any(job.get(field) for field in (
                    "track_key", "chapter_key", "media_hash", "write_intent",
                ))
            )
            if source_only:
                worker = self._run_resume_source
            elif job["dry_run"]:
                raise ValueError("Preview jobs can resume only before Yoto mutation")
            elif job["status"] in {"audio_uncertain", "running"} and all(
                job.get(field) for field in ("track_key", "chapter_key", "media_hash")
            ):
                worker = self._run_resume_reconcile
            elif (
                job["status"] in {"audio_uncertain", "running"}
                and job.get("write_intent") == "add_mp3"
                and job.get("track_key") and job.get("chapter_key")
                and not job.get("media_hash")
            ):
                worker = self._run_resume_before_post
            elif job["status"] == "audio_added_icon_pending" and job.get("audio_status") == "verified":
                if job.get("write_intent") == "upload_icon" and not job.get("icon_media_id"):
                    raise ValueError("Uncertain icon upload: supply its exact icon_media_id to resume")
                worker = self._run_resume_icon
            elif job["status"] == "needs_duplicate_review":
                if not approve_duplicate and not job.get("duplicate_approved"):
                    raise ValueError("Explicit approve_duplicate=true is required for this job")
                if approve_duplicate and not job.get("duplicate_approved"):
                    job = self.store.update(job_id, duplicate_approved=True)
                worker = self._run_resume_duplicate
            else:
                raise ValueError("Job lacks a safely reconcilable audio or icon state")
            future = self._futures.get(job_id)
            if future is None or future.done():
                self._futures[job_id] = self._pool.submit(worker, job_id)
            return job

    def wait(self, job_id: str, timeout: float = 60) -> dict[str, Any]:
        with self._lock:
            future = self._futures.get(job_id)
        if future is not None:
            future.result(timeout=timeout)
        return self.get(job_id)

    def close(self) -> None:
        self._pool.shutdown(wait=True)

    def _run_resume_source(self, job_id: str) -> None:
        self._run(self.store.get(job_id), attempt="resume")

    def _run_resume_before_post(self, job_id: str) -> None:
        job = self.store.get(job_id)
        try:
            with self._lock:
                card_lock = self._card_locks.setdefault(job["card_id"], Lock())
            with card_lock:
                card = self.client.get_playlist(job["card_id"])
                matches = [
                    track for chapter in card.get("content", {}).get("chapters", [])
                    if isinstance(chapter, dict)
                    for track in chapter.get("tracks", []) if isinstance(track, dict)
                    and track.get("key") == job["track_key"]
                ]
                if matches:
                    raise RuntimeError("Reserved track exists without a media journal; refusing retry")
            metadata = job["metadata"]
            prepared = {
                **metadata, "mp3_path": job["mp3_path"], "avatar_path": job["avatar_path"],
            }
            fields = {
                "metadata": metadata, "warnings": job.get("warnings", []),
                "mp3_path": job["mp3_path"], "avatar_path": job["avatar_path"],
            }
            # Keep the old reservation until the next synchronous callback replaces it.
            # A crash before that callback must leave this pre-POST state resumable.
            self._publish(job_id, job, prepared, fields, attempt="resume")
        except Exception:  # noqa: BLE001 - upstream failures can contain credentials
            self._handle_write_error(job_id, job)

    def _run_resume_icon(self, job_id: str) -> None:
        try:
            job = self.store.get(job_id)
            with self._lock:
                card_lock = self._card_locks.setdefault(job["card_id"], Lock())
            with card_lock:
                card = self.client.get_playlist(job["card_id"])
                track = self._exact_track(card, job["track_key"], job["chapter_key"])
                if track.get("trackUrl") != f"yoto:#{job['media_hash']}":
                    raise RuntimeError("New audio media did not match the journal")
                self._finish_icon(job_id)
        except Exception:  # noqa: BLE001 - auth/upstream errors may contain secrets
            self.store.update(
                job_id, status="audio_added_icon_pending", stage="icon",
                resume_from="icon", error="Icon assignment could not be verified",
            )

    def _run_resume_reconcile(self, job_id: str) -> None:
        job = self.store.get(job_id)
        try:
            with self._lock:
                card_lock = self._card_locks.setdefault(job["card_id"], Lock())
            with card_lock:
                self._confirm_audio(job_id)
                self._finish_icon(job_id)
        except Exception:  # noqa: BLE001 - only exact readback may advance uncertain write state
            state = self.store.get(job_id)
            if state.get("audio_status") == "verified":
                self.store.update(
                    job_id, status="audio_added_icon_pending", stage="icon",
                    resume_from="icon", error="Icon assignment could not be verified",
                )
            else:
                self.store.update(
                    job_id, status="audio_uncertain", stage="reconcile",
                    resume_from="reconcile", error="Audio write outcome is uncertain; no automatic retry",
                )

    def _run_resume_duplicate(self, job_id: str) -> None:
        job = self.store.get(job_id)
        try:
            metadata = job["metadata"]
            prepared = {
                **metadata,
                "mp3_path": job["mp3_path"],
                "avatar_path": job["avatar_path"],
            }
            fields = {
                "metadata": metadata, "warnings": job.get("warnings", []),
                "mp3_path": job["mp3_path"], "avatar_path": job["avatar_path"],
            }
            self._publish(job_id, job, prepared, fields, attempt="resume")
        except Exception:  # noqa: BLE001 - upstream failures can contain credentials
            self._handle_write_error(job_id, job)

    def _run(self, job: dict[str, Any], *, attempt: str = "initial") -> None:
        job_id = job["job_id"]
        try:
            self.store.update(job_id, status="running", stage="source")
            prepare_kwargs: dict[str, Any] = {
                "artist": job.get("artist"), "song_name": job.get("song_name"),
            }
            if job.get("start_ms") is not None or job.get("end_ms") is not None:
                prepare_kwargs.update(start_ms=job.get("start_ms"), end_ms=job.get("end_ms"))
            prepared = self.prepare(
                job["video_id"], self.upload_root, **prepare_kwargs,
            )
            metadata_fields = [
                "source_title", "channel_id", "channel_name", "artist", "title",
                "album", "title_label", "metadata_source", "metadata_score",
            ]
            metadata = {field: prepared.get(field) for field in metadata_fields}
            if job.get("start_ms") is not None or job.get("end_ms") is not None:
                metadata["source_interval"] = prepared.get("source_interval") or {
                    "start_ms": job.get("start_ms"), "end_ms": job.get("end_ms"),
                }
            fields = {
                "metadata": metadata,
                "warnings": prepared.get("warnings", []),
                "mp3_path": prepared.get("mp3_path"),
                "avatar_path": prepared.get("avatar_path"),
            }
            if job["dry_run"]:
                self.store.update(
                    job_id, **fields, status="complete", stage="preview",
                    audio_status="not_uploaded", icon_status="not_uploaded",
                    remote_verified=False,
                )
            else:
                self._publish(job_id, job, prepared, fields, attempt=attempt)
        except YouTubeSourceError as exc:
            self.store.update(job_id, status="failed", stage="source", error=str(exc))
        except Exception:  # noqa: BLE001 - upstream exceptions may contain credentials or signed URLs
            self._handle_write_error(job_id, job)

    def _handle_write_error(self, job_id: str, job: dict[str, Any]) -> None:
        state = self.store.get(job_id)
        if state.get("track_key") and state.get("media_hash") and state.get("audio_status") != "verified":
            try:
                with self._lock:
                    card_lock = self._card_locks.setdefault(job["card_id"], Lock())
                with card_lock:
                    self._confirm_audio(job_id)
                    self._finish_icon(job_id)
                return
            except Exception:  # noqa: BLE001 - only exact readback establishes remote success
                state = self.store.get(job_id)
        if state.get("audio_status") == "verified":
            upload_uncertain = (
                state.get("write_intent") == "upload_icon" and not state.get("icon_media_id")
            )
            self.store.update(
                job_id, status="audio_added_icon_pending", stage="icon",
                resume_from="icon",
                icon_status="upload_uncertain" if upload_uncertain else state.get("icon_status", "pending"),
                error=("Icon upload outcome is uncertain; supply exact icon_media_id"
                       if upload_uncertain else "Icon assignment could not be verified"),
            )
        elif state.get("track_key"):
            self.store.update(
                job_id, status="audio_uncertain", stage="reconcile",
                resume_from="reconcile", error="Audio write outcome is uncertain; no automatic retry",
            )
        else:
            self.store.update(job_id, status="failed", stage="source", error="YouTube job failed")

    def _record_upload_failure(
        self, job_id: str, operation: str, exc: Exception, *, attempt: str,
    ) -> None:
        details = _DIAGNOSTIC_DETAILS.get(operation)
        if details is None:
            return
        code, message = details
        if isinstance(exc, YotoAPIError):
            exception_category = exc.exception_category
            http_status = exc.http_status
        elif isinstance(exc, (TimeoutError, httpx.TimeoutException)):
            exception_category = "timeout"
            http_status = None
        elif isinstance(exc, (ConnectionError, httpx.TransportError)):
            exception_category = "transport"
            http_status = None
        elif isinstance(exc, OSError):
            exception_category = "local_io"
            http_status = None
        elif isinstance(exc, ValueError):
            exception_category = "invalid_input"
            http_status = None
        else:
            exception_category = "unexpected"
            http_status = None
        if not isinstance(http_status, int) or isinstance(http_status, bool):
            http_status = None
        self.store.update(
            job_id,
            diagnostic={
                "stage": "audio_upload",
                "operation": operation,
                "http_status": http_status,
                "exception_category": exception_category,
                "code": code,
                "attempt": attempt,
                "message": message,
            },
        )

    @staticmethod
    def _exact_track(card: dict[str, Any], track_key: str, chapter_key: str) -> dict[str, Any]:
        chapters = card.get("content", {}).get("chapters", [])
        matches = [
            track for chapter in chapters
            if isinstance(chapter, dict) and chapter.get("key") == chapter_key
            for track in chapter.get("tracks", [])
            if isinstance(track, dict) and track.get("key") == track_key
        ]
        if len(matches) != 1:
            raise RuntimeError("Exact new track was not confirmed by Yoto")
        return matches[0]

    @staticmethod
    def _normal_label(value: str) -> str:
        return re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()

    def _duplicate_sources(
        self, job: dict[str, Any], label: str, card: dict[str, Any],
    ) -> tuple[list[str], list[dict[str, Any]], list[str]]:
        normalized = self._normal_label(label)
        other_jobs = [
            other for other in self.store.all_jobs()
            if other["card_id"] == job["card_id"]
            and not other["dry_run"]
            and other["job_id"] != job["job_id"]
            and (
                other["video_id"] != job["video_id"]
                or (other.get("start_ms"), other.get("end_ms"))
                != (job.get("start_ms"), job.get("end_ms"))
            )
            and isinstance(other.get("metadata"), dict)
            and isinstance(other["metadata"].get("title_label"), str)
            and self._normal_label(other["metadata"]["title_label"]) == normalized
        ]
        other_videos = sorted({other["video_id"] for other in other_jobs})
        other_intervals = [
            {
                "video_id": other["video_id"],
                "start_ms": other.get("start_ms"),
                "end_ms": other.get("end_ms"),
            }
            for other in other_jobs
        ]
        other_intervals.sort(
            key=lambda item: (
                item["video_id"],
                -1 if item["start_ms"] is None else item["start_ms"],
                -1 if item["end_ms"] is None else item["end_ms"],
            )
        )
        chapters = card.get("content", {}).get("chapters", [])
        existing_tracks = sorted({
            track["key"] for chapter in chapters if isinstance(chapter, dict)
            for track in chapter.get("tracks", []) if isinstance(track, dict)
            and isinstance(track.get("title"), str)
            and self._normal_label(track["title"]) == normalized
            and isinstance(track.get("key"), str)
        })
        return other_videos, other_intervals, existing_tracks

    def _publish(
        self, job_id: str, job: dict[str, Any], prepared: dict[str, Any],
        fields: dict[str, Any], *, attempt: str = "initial",
    ) -> None:
        try:
            if not isinstance(prepared.get("avatar_path"), str) or not prepared["avatar_path"]:
                raise YouTubeSourceError("Verified channel avatar unavailable; audio was not uploaded")
            if not isinstance(prepared.get("mp3_path"), str) or not prepared["mp3_path"]:
                raise YouTubeSourceError("Verified MP3 unavailable; audio was not uploaded")
            root = self.upload_root.resolve(strict=True)
            mp3 = Path(prepared["mp3_path"]).resolve(strict=True)
            avatar = Path(prepared["avatar_path"]).resolve(strict=True)
            if (
                not mp3.is_file() or not avatar.is_file()
                or not mp3.is_relative_to(root) or not avatar.is_relative_to(root)
            ):
                raise ValueError("Prepared source files escaped YOTO_UPLOAD_ROOT")
        except Exception as exc:
            self._record_upload_failure(job_id, "local_preflight", exc, attempt=attempt)
            raise
        with self._lock:
            card_lock = self._card_locks.setdefault(job["card_id"], Lock())
        with card_lock:
            card = self.client.get_playlist(job["card_id"])
            other_videos, other_intervals, existing_tracks = self._duplicate_sources(
                job, prepared["title_label"], card,
            )
            review_changed = bool(job.get("duplicate_approved")) and (
                other_videos != job.get("duplicate_sources", [])
                or other_intervals != job.get("duplicate_source_intervals", [])
                or existing_tracks != job.get("duplicate_existing_track_keys", [])
            )
            if review_changed or ((other_videos or existing_tracks) and not job.get("duplicate_approved")):
                self.store.update(
                    job_id, **fields, status="needs_duplicate_review", stage="duplicate_review",
                    resume_from="duplicate", duplicate_sources=other_videos,
                    duplicate_source_intervals=other_intervals,
                    duplicate_existing_track_keys=existing_tracks, duplicate_approved=False,
                    write_intent=None,
                )
                return
            self.store.update(
                job_id, **fields, stage="audio", status="running", resume_from="audio",
                write_intent="add_mp3",
            )

            def reserved(track_key: str, chapter_key: str) -> None:
                self.store.update(job_id, track_key=track_key, chapter_key=chapter_key)

            def media(media_hash: str) -> None:
                self.store.update(job_id, media_hash=media_hash)

            def upload_failure(operation: str, exc: Exception) -> None:
                self._record_upload_failure(job_id, operation, exc, attempt=attempt)

            def audio_source(source: str) -> None:
                if source not in {"existing_yoto_media", "uploaded"}:
                    raise RuntimeError("Audio source callback returned an invalid value")
                self.store.update(job_id, audio_source=source)

            callbacks = {
                "on_reserved": reserved,
                "on_media_hash": media,
                "on_failure": upload_failure,
            }
            if self._supports_keyword(self.client.add_mp3, "on_audio_source"):
                callbacks["on_audio_source"] = audio_source
            self.client.add_mp3(
                job["card_id"], "new", str(mp3), dry_run=False,
                title=prepared["title_label"], **callbacks,
            )
            self._confirm_audio(job_id)
            self._finish_icon(job_id)

    def _confirm_audio(self, job_id: str) -> None:
        state = self.store.get(job_id)
        if not all(state.get(field) for field in ("track_key", "chapter_key", "media_hash")):
            raise RuntimeError("Missing journaled audio identity; cannot confirm remote write")
        card = self.client.get_playlist(state["card_id"])
        track = self._exact_track(card, state["track_key"], state["chapter_key"])
        if track.get("trackUrl") != f"yoto:#{state['media_hash']}":
            raise RuntimeError("New audio media did not match the journal")
        self.store.update(
            job_id, audio_status="verified", status="audio_added_icon_pending",
            stage="icon", resume_from="icon", write_intent=None, diagnostic=None,
        )

    @staticmethod
    def _supports_keyword(function: Callable[..., Any], keyword: str) -> bool:
        try:
            parameters = inspect.signature(function).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.name == keyword or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    def _finish_icon(self, job_id: str) -> None:
        job = self.store.get(job_id)
        card = self.client.get_playlist(job["card_id"])
        self._exact_track(card, job["track_key"], job["chapter_key"])
        icon_id = job.get("icon_media_id")
        if not icon_id:
            avatar = Path(job["avatar_path"]).resolve(strict=True)
            root = self.upload_root.resolve(strict=True)
            if not avatar.is_file() or not avatar.is_relative_to(root):
                raise RuntimeError("Verified avatar is missing")
            self.store.update(job_id, write_intent="upload_icon", icon_status="uploading")
            uploaded = self.client.upload_icon(str(avatar), auto_convert=True, dry_run=False)
            icon_id = uploaded["mediaId"]
            self.store.update(job_id, icon_media_id=icon_id, write_intent=None, icon_status="uploaded")
        chapters = card.get("content", {}).get("chapters", [])
        chapter = next(item for item in chapters if item.get("key") == job["chapter_key"])
        track = self._exact_track(card, job["track_key"], job["chapter_key"])
        icon_ref = f"yoto:#{icon_id}"
        if (
            (track.get("display") or {}).get("icon16x16") != icon_ref
            or (chapter.get("display") or {}).get("icon16x16") != icon_ref
        ):
            self.store.update(job_id, write_intent="set_track_icon")
            self.client.set_track_icon(
                job["card_id"], job["track_key"], icon_id, dry_run=False,
            )
        verified = self.client.get_playlist(job["card_id"])
        track = self._exact_track(verified, job["track_key"], job["chapter_key"])
        chapter = next(
            item for item in verified["content"]["chapters"]
            if item.get("key") == job["chapter_key"]
        )
        if (
            (track.get("display") or {}).get("icon16x16") != icon_ref
            or (chapter.get("display") or {}).get("icon16x16") != icon_ref
        ):
            raise RuntimeError("New track/chapter icon was not confirmed by Yoto")
        self.store.update(
            job_id, status="complete", stage="complete", resume_from="complete",
            audio_status="verified", icon_status="assigned", remote_verified=True,
            write_intent=None, error=None,
        )
        mp3 = Path(job["mp3_path"]).resolve(strict=False)
        if mp3.is_relative_to(self.upload_root.resolve(strict=True)) and mp3.is_file():
            mp3.unlink()
