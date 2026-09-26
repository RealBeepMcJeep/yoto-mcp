"""Pure-Python client for the Yoto content API and local media uploads."""

from __future__ import annotations

import copy
import math
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .media import (
    ascii_header_filename,
    file_sha256,
    resolve_image,
    resolve_mp3,
    sanitize_filename_stem,
)
from .metadata import MetadataLookupError, format_track_title, read_mp3_tags

API_BASE_URL = "https://api.yotoplay.com"
UPLOAD_HOST = "yoto-media-api-prod-uploads.s3.eu-west-2.amazonaws.com"
ICON_UPLOAD_PATH = "/media/displayIcons/user/me/upload"
EXPORT_HOST = "yoto-card-api-prod-media.s3.eu-west-2.amazonaws.com"
MAX_EXPORT_BYTES = 150 * 1024 * 1024


class YotoAPIError(RuntimeError):
    """Sanitized upstream error that never includes credentials or signed URLs."""

    _SAFE_CATEGORIES = frozenset({
        "yoto_api", "timeout", "transport", "local_io", "invalid_input", "unexpected",
    })

    def __init__(
        self, message: str, *, http_status: int | None = None,
        exception_category: str = "yoto_api",
    ) -> None:
        super().__init__(message)
        self.http_status = http_status if (
            isinstance(http_status, int) and not isinstance(http_status, bool)
            and 100 <= http_status <= 599
        ) else None
        self.exception_category = (
            exception_category
            if isinstance(exception_category, str) and exception_category in self._SAFE_CATEGORIES
            else "unexpected"
        )


def _safe_exception_category(exc: Exception) -> str:
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, (ConnectionError, httpx.TransportError)):
        return "transport"
    if isinstance(exc, OSError):
        return "local_io"
    if isinstance(exc, ValueError):
        return "invalid_input"
    return "unexpected"


class YotoClient:
    """Synchronous Yoto client; auth/token refresh is supplied by the parent app."""

    def __init__(
        self,
        token_provider: Callable[[], str],
        transport: httpx.BaseTransport | None = None,
        *,
        upload_root: str | Path | None = None,
        allow_writes: bool = False,
        dry_run: bool = True,
        base_url: str = API_BASE_URL,
        poll_attempts: int = 10,
        poll_interval: float = 4.0,
    ) -> None:
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        self._token_provider = token_provider
        self._http = httpx.Client(transport=transport, timeout=30.0)
        self._base_url = base_url.rstrip("/")
        self.upload_root = upload_root
        self.allow_writes = allow_writes
        self.dry_run = dry_run
        self.poll_attempts = poll_attempts
        self.poll_interval = poll_interval
        self._pending: dict[tuple[str, str], dict[str, Any]] = {}
        self._pending_lock = threading.RLock()


    def close(self) -> None:
        self._http.close()

    def list_playlists(self) -> list[dict[str, Any]]:
        data = self._api_json("GET", "/content/mine")
        cards = data.get("cards")
        if not isinstance(cards, list):
            raise YotoAPIError("Yoto returned an invalid playlist list")
        summaries = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            chapters = card.get("content", {}).get("chapters")
            if not isinstance(chapters, list):
                detail = self._fetch_playlist(str(card.get("cardId", "")), include_pending=False)
                chapters = detail.get("content", {}).get("chapters", [])
            track_count = sum(
                len(chapter.get("tracks", []))
                for chapter in chapters
                if isinstance(chapter, dict) and isinstance(chapter.get("tracks", []), list)
            ) if isinstance(chapters, list) else 0
            summaries.append(
                {
                    key: card[key]
                    for key in ("cardId", "title", "slug", "updatedAt")
                    if key in card
                }
                | {
                    "track_count": track_count,
                    "pending_tracks": self._pending_for_card(str(card.get("cardId", ""))),
                }
            )
        return summaries

    def get_playlist(self, card_id: str) -> dict[str, Any]:
        return self._fetch_playlist(card_id, include_pending=True)

    def _fetch_playlist(self, card_id: str, *, include_pending: bool) -> dict[str, Any]:
        card_id = self._required_id(card_id, "card id")
        data = self._api_json("GET", f"/content/{quote(card_id, safe='')}")
        card = data.get("card")
        if not isinstance(card, dict):
            raise YotoAPIError("Yoto returned an invalid playlist")
        result = copy.deepcopy(card)
        chapters = result.get("content", {}).get("chapters")
        if include_pending and isinstance(chapters, list):
            for pending in self._pending_for_card(card_id):
                chapter = next(
                    (item for item in chapters if isinstance(item, dict)
                     and item.get("key") == pending["chapter_key"]), None
                )
                if chapter is None and pending.get("new_chapter"):
                    chapter = {
                        "key": pending["chapter_key"],
                        "title": pending["title"],
                        "display": {"icon16x16": None},
                        "tracks": [],
                    }
                    chapters.append(chapter)
                if chapter is None:
                    continue
                tracks = chapter.setdefault("tracks", [])
                if not any(
                    isinstance(track, dict) and track.get("key") == pending["track_key"]
                    for track in tracks
                ):
                    tracks.append({
                        "key": pending["track_key"],
                        "title": pending["title"],
                        "trackUrl": "",
                        "type": "audio",
                        "_status": {"status": pending["status"], "percentage": 0},
                    })
        return result

    def add_mp3(
        self,
        card_id: str,
        chapter_key: str,
        file_path: str,
        *,
        dry_run: bool | None = None,
        title: str | None = None,
        on_reserved: Callable[[str, str], None] | None = None,
        on_media_hash: Callable[[str], None] | None = None,
        on_audio_source: Callable[[str], None] | None = None,
        on_failure: Callable[[str, Exception], None] | None = None,
    ) -> dict[str, Any]:
        """Upload an MP3, optionally journaling its exact remote identity.

        ``on_reserved(track_key, chapter_key)`` runs synchronously after the
        playlist read/reservation and before upload network requests. After
        transcode and whole-card assembly, ``on_media_hash(media_hash)`` runs
        synchronously immediately before the full-card POST.
        ``on_audio_source(source)`` runs after the exact added track is read
        back, with ``existing_yoto_media`` or ``uploaded``.
        ``on_failure(operation, exception)`` reports one of the allowlisted
        upload boundaries; callback exceptions propagate like journal callback
        exceptions. A reservation failure prevents upload; a media-journal
        failure prevents the card POST (the staged media upload may already
        exist).
        """
        card_id = self._required_id(card_id, "card id")
        chapter_key = self._required_id(chapter_key, "chapter key")
        if on_reserved is not None and not callable(on_reserved):
            raise TypeError("on_reserved must be callable")
        if on_media_hash is not None and not callable(on_media_hash):
            raise TypeError("on_media_hash must be callable")
        if on_audio_source is not None and not callable(on_audio_source):
            raise TypeError("on_audio_source must be callable")
        if on_failure is not None and not callable(on_failure):
            raise TypeError("on_failure must be callable")

        def report_failure(operation: str, exc: Exception) -> None:
            if on_failure is not None:
                on_failure(operation, exc)

        try:
            if self.upload_root is None:
                raise ValueError("YOTO_UPLOAD_ROOT must be configured before uploading")
            source = resolve_mp3(Path(self.upload_root), file_path)
        except Exception as exc:
            report_failure("local_preflight", exc)
            raise
        suggested_title = ""
        if title is None:
            try:
                tags = read_mp3_tags(source)
            except MetadataLookupError:
                tags = {}
            if tags.get("artist") and tags.get("title"):
                suggested_title = format_track_title(tags["artist"], tags["title"])
        is_dry_run = self.dry_run if dry_run is None else dry_run
        if not is_dry_run:
            self._require_writes_enabled()
        card = self.get_playlist(card_id)
        chapters = card.get("content", {}).get("chapters")
        create_chapter = chapter_key == "new"
        if not isinstance(chapters, list) or (not create_chapter and not any(
            isinstance(chapter, dict) and chapter.get("key") == chapter_key
            for chapter in chapters
        )):
            raise ValueError("Chapter key was not found in the playlist")
        if is_dry_run:
            return {
                "dry_run": True,
                "action": "add_mp3",
                "cardId": card_id,
                "chapter_key": chapter_key,
                "file": source.name,
                "proposed_title": (title.strip() if isinstance(title, str) and title.strip()
                                   else suggested_title or source.stem),
            }
        owner_id = card.get("userId")
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("Playlist owner is missing; refusing to upload")
        track_key = self._new_track_key(card)
        if create_chapter:
            chapter_keys = {
                chapter.get("key") for chapter in chapters if isinstance(chapter, dict)
            }
            chapter_key = self._new_track_key(card)
            while chapter_key == track_key or chapter_key in chapter_keys:
                chapter_key = self._new_track_key(card)
        if on_reserved is not None:
            on_reserved(track_key, chapter_key)
        try:
            digest = file_sha256(source)
        except Exception as exc:
            report_failure("local_preflight", exc)
            raise
        pending = {
            "cardId": card_id,
            "chapter_key": chapter_key,
            "track_key": track_key,
            "title": title or suggested_title or source.stem,
            "new_chapter": create_chapter,
            "status": "uploading",
        }
        pending_key = (card_id, track_key)
        with self._pending_lock:
            self._pending[pending_key] = pending
        try:
            try:
                upload_response = self._api_json(
                    "GET",
                    "/media/transcode/audio/uploadUrl",
                    params={
                        "sha256": digest,
                        "filename": source.name,
                        "mediaAccount": owner_id,
                    },
                )
                upload = upload_response.get("upload")
                if not isinstance(upload, dict):
                    raise YotoAPIError("Yoto returned an invalid upload request")
                upload_url = upload.get("uploadUrl")
                upload_id = upload.get("uploadId")
                if (
                    "uploadUrl" not in upload
                    or not isinstance(upload_id, str)
                    or not upload_id.strip()
                ):
                    raise YotoAPIError("Yoto returned an invalid upload request")
                if upload_url is not None:
                    if not isinstance(upload_url, str):
                        raise YotoAPIError("Yoto returned an invalid upload URL")
                    try:
                        parsed_upload_url = httpx.URL(upload_url)
                    except Exception:  # noqa: BLE001 - URL parser errors can echo signed URLs
                        raise YotoAPIError("Yoto returned an invalid upload URL") from None
                    if (
                        parsed_upload_url.scheme != "https"
                        or parsed_upload_url.host != UPLOAD_HOST
                        or parsed_upload_url.port not in (None, 443)
                        or parsed_upload_url.username
                        or parsed_upload_url.password
                    ):
                        raise YotoAPIError("Yoto returned an invalid upload URL")
            except Exception as exc:
                report_failure("upload_url", exc)
                raise

            if upload_url is not None:
                # HTTP header values must be ASCII; fold diacritics/non-Latin filenames.
                disposition_name = ascii_header_filename(source.name).replace("\\", "\\\\").replace('"', '\\"')
                try:
                    with source.open("rb") as stream:
                        uploaded = self._http.put(
                            upload_url,
                            content=stream,
                            headers={
                                "Content-Disposition": f'attachment; filename="{disposition_name}"',
                                "Content-Type": "audio/mpeg",
                            },
                        )
                    if uploaded.is_error:
                        raise YotoAPIError("Yoto media upload failed", http_status=uploaded.status_code)
                except YotoAPIError as exc:
                    report_failure("upload_put", exc)
                    raise
                except Exception as exc:  # noqa: BLE001 - transport errors may contain signed URLs
                    report_failure("upload_put", exc)
                    raise YotoAPIError("Yoto media upload failed") from None

            try:
                self._set_pending_status(pending_key, "transcoding")
                transcode = self._wait_for_transcode(upload_id)
                media_hash = transcode.get("transcodedSha256")
                if not isinstance(media_hash, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", media_hash):
                    raise YotoAPIError("Yoto returned an invalid transcoded media id")
                info = transcode.get("transcodedInfo")
                if (
                    not isinstance(info, dict)
                    or not self._valid_nonnegative_number(info.get("duration"))
                    or not self._valid_nonnegative_number(info.get("fileSize"))
                ):
                    raise YotoAPIError("Yoto returned incomplete transcode metadata")
            except Exception as exc:
                report_failure("transcode", exc)
                raise
            metadata = info.get("metadata")
            transcode_title = metadata.get("title") if isinstance(metadata, dict) else None
            if not isinstance(transcode_title, str) or not transcode_title.strip() or transcode_title.lower().startswith("new recording"):
                transcode_title = source.stem
            elif transcode_title.lower().endswith(".mp3"):
                transcode_title = transcode_title[:-4]
            track_title = (title.strip() if isinstance(title, str) and title.strip()
                           else suggested_title or transcode_title)

            # Refresh after upload to preserve edits observed during transcoding.
            # This narrows, but cannot close, the race with external writers: Yoto
            # has no conditional whole-card update/CAS boundary.
            latest = self._fetch_playlist(card_id, include_pending=False)
            latest_chapters = latest.get("content", {}).get("chapters")
            if not isinstance(latest_chapters, list):
                raise YotoAPIError("Playlist has no valid chapter list")
            chapter = next(
                (item for item in latest_chapters if isinstance(item, dict) and item.get("key") == chapter_key),
                None,
            )
            if not create_chapter and (chapter is None or not isinstance(chapter.get("tracks"), list)):
                raise ValueError("Chapter key no longer exists in the playlist")
            track = {
                "key": track_key,
                "title": track_title,
                "trackUrl": f"yoto:#{media_hash}",
                "type": "audio",
                "duration": self._number(info.get("duration")),
                "fileSize": self._number(info.get("fileSize")),
            }
            for field in ("channels", "format"):
                value = info.get(field)
                if isinstance(value, str) and value:
                    track[field] = value
            if create_chapter:
                if chapter is not None:
                    raise ValueError("New chapter key collides with another chapter")
                label = str(len(latest_chapters) + 1)
                track["overlayLabel"] = label
                latest_chapters.append({
                    "key": chapter_key,
                    "title": track_title,
                    "tracks": [track],
                    "display": {"icon16x16": None},
                    "overlayLabel": label,
                })
            else:
                assert chapter is not None
                chapter["tracks"].append(track)
            self._update_media_totals(latest)
            if on_media_hash is not None:
                on_media_hash(media_hash)
            self._api_json("POST", "/content", json=latest)
            verified = self.get_playlist(card_id)
            verified_chapters = verified.get("content", {}).get("chapters", [])
            verified_chapter = next(
                (
                    item for item in verified_chapters
                    if isinstance(item, dict) and item.get("key") == chapter_key
                ),
                None,
            )
            verified_tracks = verified_chapter.get("tracks") if verified_chapter is not None else None
            if not isinstance(verified_tracks, list) or not any(
                item.get("key") == track_key and item.get("trackUrl") == track["trackUrl"]
                for item in verified_tracks if isinstance(item, dict)
            ):
                raise YotoAPIError("Yoto did not confirm the added track")
            if on_audio_source is not None:
                on_audio_source("existing_yoto_media" if upload_url is None else "uploaded")
            return verified
        finally:
            with self._pending_lock:
                self._pending.pop(pending_key, None)

    def rename_track(
        self,
        card_id: str,
        track_key: str,
        artist: str,
        title: str,
        *,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        card_id = self._required_id(card_id, "card id")
        track_key = self._required_id(track_key, "track key")
        if not isinstance(artist, str) or not isinstance(title, str):
            raise TypeError("Artist and title are required")
        artist, title = artist.strip(), title.strip()
        if not artist or not title or len(artist) > 100 or len(title) > 100:
            raise ValueError("Artist and title must be 1–100 characters")
        if any(ord(char) < 32 or ord(char) == 127 for char in artist + title):
            raise ValueError("Artist and title cannot contain control characters")
        new_title = f"{artist} — {title}"
        is_dry_run = self.dry_run if dry_run is None else dry_run
        if not is_dry_run:
            self._require_writes_enabled()
        card = self._fetch_playlist(card_id, include_pending=False)
        matches = [
            (chapter, track)
            for chapter in card.get("content", {}).get("chapters", [])
            if isinstance(chapter, dict) and isinstance(chapter.get("tracks"), list)
            for track in chapter["tracks"]
            if isinstance(track, dict) and track.get("key") == track_key
        ]
        if len(matches) != 1:
            raise ValueError("Track key was not found uniquely in the playlist")
        chapter, track = matches[0]
        old_title = track.get("title")
        old_chapter_title = chapter.get("title")
        chapter_title = new_title if len(chapter["tracks"]) == 1 else old_chapter_title
        if is_dry_run:
            return {
                "dry_run": True, "cardId": card_id, "track_key": track_key,
                "old_title": old_title, "new_title": new_title,
                "old_chapter_title": old_chapter_title, "new_chapter_title": chapter_title,
            }
        if old_title == new_title and old_chapter_title == chapter_title:
            return card
        track["title"] = new_title
        if chapter_title != old_chapter_title:
            chapter["title"] = chapter_title
        self._api_json("POST", "/content", json=card)
        verified = self._fetch_playlist(card_id, include_pending=False)
        seen = [
            (c, t)
            for c in verified.get("content", {}).get("chapters", [])
            if isinstance(c, dict)
            for t in c.get("tracks", [])
            if isinstance(t, dict) and t.get("key") == track_key
        ]
        if len(seen) != 1 or seen[0][1].get("title") != new_title or seen[0][0].get("title") != chapter_title:
            raise YotoAPIError("Yoto did not confirm the track rename")
        return verified

    def remove_track(
        self,
        card_id: str,
        track_key: str,
        *,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        card_id = self._required_id(card_id, "card id")
        track_key = self._required_id(track_key, "track key")
        is_dry_run = self.dry_run if dry_run is None else dry_run
        if not is_dry_run:
            self._require_writes_enabled()
        card = self._fetch_playlist(card_id, include_pending=False)
        chapters = card.get("content", {}).get("chapters")
        if not isinstance(chapters, list):
            raise YotoAPIError("Playlist has no valid chapter list")
        matches = [
            (chapter, track)
            for chapter in chapters
            if isinstance(chapter, dict) and isinstance(chapter.get("tracks"), list)
            for track in chapter["tracks"]
            if isinstance(track, dict) and track.get("key") == track_key
        ]
        if not matches:
            raise ValueError("Track key was not found in the playlist")
        if len(matches) != 1:
            raise ValueError("Track key is not unique in the playlist; refusing removal")
        chapter, target = matches[0]
        if is_dry_run:
            return {
                "dry_run": True,
                "action": "remove_track",
                "cardId": card_id,
                "track_key": track_key,
                "title": target.get("title"),
            }
        chapter["tracks"] = [track for track in chapter["tracks"] if track.get("key") != track_key]
        self._update_media_totals(card)
        self._api_json("POST", "/content", json=card)
        verified = self.get_playlist(card_id)
        if any(
            isinstance(track, dict) and track.get("key") == track_key
            for verified_chapter in verified.get("content", {}).get("chapters", [])
            if isinstance(verified_chapter, dict)
            for track in verified_chapter.get("tracks", [])
        ):
            raise YotoAPIError("Yoto did not confirm the removed track")
        return verified

    def upload_icon(
        self,
        file_path: str,
        *,
        auto_convert: bool = True,
        filename: str | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        if self.upload_root is None:
            raise ValueError("YOTO_UPLOAD_ROOT must be configured before uploading")
        source, mime = resolve_image(Path(self.upload_root), file_path)
        is_dry_run = self.dry_run if dry_run is None else dry_run
        if not is_dry_run:
            self._require_writes_enabled()
        if is_dry_run:
            return {
                "dry_run": True,
                "action": "upload_icon",
                "file": source.name,
                "mime_type": mime,
                "auto_convert": auto_convert,
            }
        params: dict[str, str] = {"autoConvert": "true" if auto_convert else "false"}
        if filename:
            params["filename"] = filename
        try:
            token = self._token_provider()
            if not isinstance(token, str) or not token:
                raise YotoAPIError("No Yoto access token is available")
            with source.open("rb") as stream:
                response = self._http.post(
                    f"{self._base_url}{ICON_UPLOAD_PATH}",
                    params=params,
                    content=stream,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": mime},
                )
            if response.is_error:
                raise YotoAPIError(f"Yoto icon upload failed (HTTP {response.status_code})")
            data = response.json()
        except YotoAPIError:
            raise
        except Exception:  # noqa: BLE001 - transport/auth exceptions may contain secrets
            raise YotoAPIError("Yoto icon upload failed") from None
        display_icon = data.get("displayIcon") if isinstance(data, dict) else None
        media_id = display_icon.get("mediaId") if isinstance(display_icon, dict) else None
        if not isinstance(media_id, str) or not media_id:
            raise YotoAPIError("Yoto returned an invalid icon upload response")
        url = display_icon.get("url")
        return {
            "mediaId": media_id,
            "url": url if isinstance(url, str) else None,
            "new": bool(display_icon.get("new", False)),
        }

    def set_track_icon(
        self,
        card_id: str,
        track_key: str,
        media_id: str,
        *,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        card_id = self._required_id(card_id, "card id")
        track_key = self._required_id(track_key, "track key")
        media_id = self._required_id(media_id, "media id")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", media_id):
            raise ValueError("Media id has an unexpected format")
        icon_ref = f"yoto:#{media_id}"
        is_dry_run = self.dry_run if dry_run is None else dry_run
        if not is_dry_run:
            self._require_writes_enabled()
        card = self._fetch_playlist(card_id, include_pending=False)
        matches = [
            (chapter, track)
            for chapter in card.get("content", {}).get("chapters", [])
            if isinstance(chapter, dict) and isinstance(chapter.get("tracks"), list)
            for track in chapter["tracks"]
            if isinstance(track, dict) and track.get("key") == track_key
        ]
        if len(matches) != 1:
            raise ValueError("Track key was not found uniquely in the playlist")
        chapter, track = matches[0]
        old_icon = (track.get("display") or {}).get("icon16x16")
        old_chapter_icon = (chapter.get("display") or {}).get("icon16x16")
        # Mirror rename_track's convention: only cascade to the chapter when
        # it has exactly one track, so a per-track icon never silently
        # relabels sibling tracks that share the chapter.
        solo_track_chapter = len(chapter["tracks"]) == 1
        new_chapter_icon = icon_ref if solo_track_chapter else old_chapter_icon
        if is_dry_run:
            return {
                "dry_run": True, "cardId": card_id, "track_key": track_key,
                "old_icon": old_icon, "new_icon": icon_ref,
                "old_chapter_icon": old_chapter_icon, "new_chapter_icon": new_chapter_icon,
            }
        if old_icon == icon_ref and old_chapter_icon == new_chapter_icon:
            return card
        track["display"] = {"icon16x16": icon_ref}
        if new_chapter_icon != old_chapter_icon:
            chapter["display"] = {"icon16x16": new_chapter_icon}
        self._api_json("POST", "/content", json=card)
        verified = self._fetch_playlist(card_id, include_pending=False)
        seen = [
            (c, t)
            for c in verified.get("content", {}).get("chapters", [])
            if isinstance(c, dict)
            for t in c.get("tracks", [])
            if isinstance(t, dict) and t.get("key") == track_key
        ]
        if (
            len(seen) != 1
            or (seen[0][1].get("display") or {}).get("icon16x16") != icon_ref
            or (seen[0][0].get("display") or {}).get("icon16x16") != new_chapter_icon
        ):
            raise YotoAPIError("Yoto did not confirm the icon change")
        return verified

    def reorder_chapters(
        self,
        card_id: str,
        chapter_keys: list[str],
        *,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """Reorder the playlist's chapters (its "songs") to match chapter_keys exactly.

        There is no dedicated reorder endpoint; chapter order is just array order in
        the same whole-card POST every other write uses. Renumbers overlayLabel to
        match the new position, propagated to each chapter's tracks -- correct for
        this tool's own one-track-per-chapter uploads, but a chapter with several
        tracks keeps a single shared label rather than per-track global numbering,
        since the real numbering scheme for that case is unverified.
        """
        card_id = self._required_id(card_id, "card id")
        if not isinstance(chapter_keys, list) or not chapter_keys or not all(
            isinstance(key, str) and key for key in chapter_keys
        ):
            raise ValueError("chapter_keys must be a non-empty list of chapter key strings")
        is_dry_run = self.dry_run if dry_run is None else dry_run
        if not is_dry_run:
            self._require_writes_enabled()
        card = self._fetch_playlist(card_id, include_pending=False)
        chapters = card.get("content", {}).get("chapters")
        if not isinstance(chapters, list) or not chapters:
            raise YotoAPIError("Playlist has no valid chapter list")
        by_key = {chapter.get("key"): chapter for chapter in chapters if isinstance(chapter, dict)}
        if len(chapter_keys) != len(chapters) or set(chapter_keys) != set(by_key):
            raise ValueError("chapter_keys must list every existing chapter key exactly once")
        old_order = [{"key": chapter.get("key"), "title": chapter.get("title")} for chapter in chapters]
        new_order = [{"key": key, "title": by_key[key].get("title")} for key in chapter_keys]
        if is_dry_run:
            return {"dry_run": True, "cardId": card_id, "old_order": old_order, "new_order": new_order}
        if chapter_keys == [item["key"] for item in old_order]:
            return card
        reordered = []
        for index, key in enumerate(chapter_keys, start=1):
            chapter = by_key[key]
            chapter["overlayLabel"] = str(index)
            for track in chapter.get("tracks", []):
                if isinstance(track, dict):
                    track["overlayLabel"] = str(index)
            reordered.append(chapter)
        card["content"]["chapters"] = reordered
        self._api_json("POST", "/content", json=card)
        verified = self._fetch_playlist(card_id, include_pending=False)
        verified_keys = [
            chapter.get("key")
            for chapter in verified.get("content", {}).get("chapters", [])
            if isinstance(chapter, dict)
        ]
        if verified_keys != chapter_keys:
            raise YotoAPIError("Yoto did not confirm the new chapter order")
        return verified

    def export_track(
        self,
        card_id: str,
        track_key: str,
        *,
        destination_name: str | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """Download one owned track's current playable audio to a local file.

        The signed URL is never returned, logged, or cached -- only fetched and
        immediately streamed to disk. Not guaranteed to be byte-identical to any
        original upload; this is Yoto's played/transcoded copy.
        """
        card_id = self._required_id(card_id, "card id")
        track_key = self._required_id(track_key, "track key")
        if self.upload_root is None:
            raise ValueError("YOTO_UPLOAD_ROOT must be configured before exporting")
        is_dry_run = self.dry_run if dry_run is None else dry_run
        if not is_dry_run:
            self._require_writes_enabled()
        card = self._fetch_playlist(card_id, include_pending=False)
        matches = [
            (chapter, track)
            for chapter in card.get("content", {}).get("chapters", [])
            if isinstance(chapter, dict) and isinstance(chapter.get("tracks"), list)
            for track in chapter["tracks"]
            if isinstance(track, dict) and track.get("key") == track_key
        ]
        if len(matches) != 1:
            raise ValueError("Track key was not found uniquely in the playlist")
        _chapter, target = matches[0]
        title = target.get("title") or track_key
        fmt = target.get("format") if isinstance(target.get("format"), str) and target.get("format") else "mp3"
        stem = sanitize_filename_stem(destination_name or title)
        destination = Path(self.upload_root) / f"{stem}.{fmt}"
        if is_dry_run:
            return {
                "dry_run": True, "action": "export_track", "cardId": card_id, "track_key": track_key,
                "title": title, "proposed_file": destination.name,
                "note": "Exported audio is Yoto's played copy; not guaranteed byte-identical to any original upload.",
            }
        if destination.exists():
            raise ValueError(f"Destination file already exists: {destination.name}")
        signed = self._api_json(
            "GET", f"/content/{quote(card_id, safe='')}",
            params={"playable": "true", "signingType": "s3"},
        )
        signed_card = signed.get("card") if isinstance(signed, dict) else None
        signed_chapters = signed_card.get("content", {}).get("chapters") if isinstance(signed_card, dict) else None
        signed_track = next(
            (
                item for chapter in (signed_chapters or []) if isinstance(chapter, dict)
                for item in chapter.get("tracks", [])
                if isinstance(item, dict) and item.get("key") == track_key
            ),
            None,
        )
        url = signed_track.get("trackUrl") if isinstance(signed_track, dict) else None
        if not isinstance(url, str) or not url.startswith("https://"):
            raise YotoAPIError("Yoto did not return a playable signed URL for this track")
        try:
            parsed_url = httpx.URL(url)
        except Exception:  # noqa: BLE001 - URL parser errors can echo signed URLs
            raise YotoAPIError("Yoto returned an invalid signed URL") from None
        if (
            parsed_url.scheme != "https"
            or parsed_url.host != EXPORT_HOST
            or parsed_url.port not in (None, 443)
        ):
            raise YotoAPIError("Yoto returned a signed URL on an unexpected host")
        fd, tmp_name = tempfile.mkstemp(prefix=".export-", dir=self.upload_root)
        total = 0
        try:
            with os.fdopen(fd, "wb") as handle, self._http.stream("GET", url, timeout=60.0) as response:
                if response.status_code != 200:
                    raise YotoAPIError(f"Yoto media download failed (HTTP {response.status_code})")
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_EXPORT_BYTES:
                        raise YotoAPIError("Exported track exceeds the size limit")
                    handle.write(chunk)
            os.replace(tmp_name, destination)
        except YotoAPIError:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        except Exception:  # noqa: BLE001 - transport errors may contain signed URLs
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise YotoAPIError("Yoto media download failed") from None
        return {"file": str(destination), "title": title, "bytes": total}

    def _wait_for_transcode(self, upload_id: str) -> dict[str, Any]:
        if self.poll_attempts < 1:
            raise ValueError("poll_attempts must be at least 1")
        path = f"/media/upload/{quote(upload_id, safe='')}/transcoded"
        for attempt in range(self.poll_attempts):
            response = self._api_json("GET", path, params={"loudnorm": "false"})
            result = response.get("transcode")
            if not isinstance(result, dict):
                raise YotoAPIError("Yoto returned an invalid transcode status")
            if result.get("erroredAt"):
                raise YotoAPIError("Yoto audio transcoding failed")
            if result.get("transcodedSha256"):
                return result
            if attempt + 1 < self.poll_attempts and self.poll_interval > 0:
                time.sleep(self.poll_interval)
        raise YotoAPIError("Yoto audio transcoding did not complete")

    @staticmethod
    def _valid_nonnegative_number(value: Any) -> bool:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and value >= 0
        )

    @staticmethod
    def _number(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return 0
        return max(0, math.floor(value))

    @staticmethod
    def _new_track_key(card: dict[str, Any]) -> str:
        chapters = card.get("content", {}).get("chapters", [])
        existing = {
            track.get("key")
            for chapter in chapters
            if isinstance(chapter, dict)
            for track in chapter.get("tracks", [])
            if isinstance(track, dict)
        }
        for _ in range(10):
            key = secrets.token_urlsafe(10)
            if len(key) <= 20 and key not in existing:
                return key
        raise YotoAPIError("Could not allocate a unique track key")

    def _set_pending_status(self, key: tuple[str, str], status: str) -> None:
        with self._pending_lock:
            pending = self._pending.get(key)
            if pending is not None:
                pending["status"] = status


    @classmethod
    def _update_media_totals(cls, card: dict[str, Any]) -> None:
        chapters = card.get("content", {}).get("chapters", [])
        tracks = [
            track
            for chapter in chapters
            if isinstance(chapter, dict)
            for track in chapter.get("tracks", [])
            if isinstance(track, dict)
        ]
        metadata = card.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            metadata = card["metadata"] = {}
        media = metadata.get("media")
        if not isinstance(media, dict):
            media = {}
        metadata["media"] = {
            **media,
            "duration": sum(cls._number(track.get("duration")) for track in tracks),
            "fileSize": sum(cls._number(track.get("fileSize")) for track in tracks),
        }

    def _api_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            token = self._token_provider()
            if not isinstance(token, str) or not token:
                raise YotoAPIError("No Yoto access token is available")
            response = self._http.request(
                method,
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
            if response.is_error:
                raise YotoAPIError(
                    f"Yoto API request failed (HTTP {response.status_code})",
                    http_status=response.status_code,
                )
            data = response.json() if response.content else {}
        except YotoAPIError:
            raise
        except Exception as exc:  # noqa: BLE001 - upstream/auth exceptions may contain secrets
            raise YotoAPIError(
                "Yoto API request failed",
                exception_category=_safe_exception_category(exc),
            ) from None
        if not isinstance(data, dict):
            raise YotoAPIError("Yoto returned an invalid response")
        return data

    @staticmethod
    def _required_id(value: str, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"A {label} is required")
        return value.strip()

    def _require_writes_enabled(self) -> None:
        if not self.allow_writes:
            raise PermissionError("Writes are disabled; set YOTO_ALLOW_WRITES=1 to enable them")

    def _pending_for_card(self, card_id: str) -> list[dict[str, Any]]:
        with self._pending_lock:
            return [
                copy.deepcopy(entry)
                for (pending_card_id, _), entry in self._pending.items()
                if pending_card_id == card_id
            ]
