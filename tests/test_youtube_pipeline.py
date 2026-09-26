from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from yoto_mcp.yoto import YotoAPIError
from yoto_mcp.youtube_jobs import JobStore
from yoto_mcp.youtube_pipeline import YouTubeCoordinator
from yoto_mcp.youtube_source import YouTubeSourceError


class NoYotoWrites:
    def add_mp3(self, *args, **kwargs):
        raise AssertionError("dry-run must not contact Yoto to add audio")

    def upload_icon(self, *args, **kwargs):
        raise AssertionError("dry-run must not contact Yoto to upload icons")

    def set_track_icon(self, *args, **kwargs):
        raise AssertionError("dry-run must not contact Yoto to assign icons")


def test_preview_returns_job_id_then_status_with_prepared_mp3_and_avatar(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    prepared = []

    def prepare(video_id, upload_root, *, artist=None, song_name=None):
        prepared.append((video_id, upload_root, artist, song_name))
        mp3 = root / "audio.mp3"
        avatar = root / "avatar.jpg"
        mp3.write_bytes(b"mp3")
        avatar.write_bytes(b"jpg")
        return {
            "video_id": video_id, "source_title": "Original Video", "channel_id": "UCfake",
            "channel_name": "Video Channel", "artist": "Chosen Artist", "title": "Chosen Song",
            "title_label": "Chosen Artist — Chosen Song", "album": "",
            "metadata_source": "user_override", "metadata_score": None,
            "requires_review": False, "warnings": [],
            "mp3_path": str(mp3), "avatar_path": str(avatar),
        }

    coordinator = YouTubeCoordinator(
        NoYotoWrites(), JobStore(tmp_path / "private-jobs"), root,
        allow_writes=False, prepare=prepare,
    )
    job = coordinator.submit("card-one", "abcdefghijk", artist="Chosen Artist", song_name="Chosen Song")
    assert job["job_id"]
    completed = coordinator.wait(job["job_id"], timeout=3)
    assert completed["status"] == "complete"
    assert completed["stage"] == "preview"
    assert completed["dry_run"] is True
    assert completed["metadata"]["title_label"] == "Chosen Artist — Chosen Song"
    assert completed["metadata"]["source_title"] == "Original Video"
    assert completed["remote_verified"] is False
    assert "audio_source" not in completed
    assert prepared == [("abcdefghijk", root, "Chosen Artist", "Chosen Song")]
    assert coordinator.get(job["job_id"]) == completed
    coordinator.close()


def test_youtube_link_admission_deduplicates_with_canonical_video_id(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    store = JobStore(tmp_path / "private-jobs")
    seen = []

    def prepare(video_id, _upload_root, **_kwargs):
        seen.append(video_id)
        return {"title_label": "Artist — Song", "warnings": []}

    coordinator = YouTubeCoordinator(NoYotoWrites(), store, root, allow_writes=False, prepare=prepare)
    url_job = coordinator.submit("card-one", "https://youtu.be/dQw4w9WgXcQ?si=shared")
    done = coordinator.wait(url_job["job_id"], timeout=3)
    assert done["status"] == "complete"
    assert done["video_id"] == "dQw4w9WgXcQ"
    assert coordinator.submit("card-one", "dQw4w9WgXcQ")["job_id"] == url_job["job_id"]
    assert seen == ["dQw4w9WgXcQ"]
    with pytest.raises(YouTubeSourceError, match="YouTube"):
        coordinator.submit("card-one", "https://youtu.be/dQw4w9WgXcQ?list=bad")
    assert len(store.all_jobs()) == 1
    coordinator.close()


def test_restart_resubmits_source_only_job_without_new_admission(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    store = JobStore(tmp_path / "private-jobs")
    interrupted = store.submit("card-one", "abcdefghijk", dry_run=True)
    store.update(interrupted["job_id"], status="running", stage="source")
    calls = []

    def prepare(video_id, _root, **_kwargs):
        calls.append(video_id)
        return {"title_label": "Artist — Song", "warnings": []}

    restarted = YouTubeCoordinator(NoYotoWrites(), JobStore(tmp_path / "private-jobs"), root,
                                  allow_writes=False, prepare=prepare)
    same = restarted.submit("card-one", "abcdefghijk", dry_run=True)
    assert same["job_id"] == interrupted["job_id"]
    assert calls == []  # Admission alone must not race an old process.
    restarted.resume(same["job_id"])
    done = restarted.wait(same["job_id"], timeout=3)
    assert done["status"] == "complete" and done["stage"] == "preview"
    assert calls == ["abcdefghijk"]
    assert len(store.all_jobs()) == 1
    restarted.close()


def test_write_job_rejected_before_source_work_when_write_gate_off(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    store = JobStore(tmp_path / "private-jobs")
    coordinator = YouTubeCoordinator(
        NoYotoWrites(), store, root, allow_writes=False,
        prepare=lambda *_args, **_kwargs: pytest.fail("must reject before source work"),
    )
    with pytest.raises(ValueError, match="YOTO_ALLOW_WRITES"):
        coordinator.submit("card-one", "abcdefghijk", dry_run=False)
    assert store.recoverable_jobs() == []
    coordinator.close()


def test_write_adds_exact_track_then_icon_without_changing_prior_chapters(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    store = JobStore(tmp_path / "private-jobs")
    original = {"key": "old-chapter", "title": "Keep", "tracks": [
        {"key": "old-track", "title": "Keep", "trackUrl": "yoto:#old"},
    ]}

    class FakeClient:
        def __init__(self):
            self.card = {"cardId": "card-one", "content": {"chapters": [deepcopy(original)]}}
            self.add_calls = 0

        def get_playlist(self, card_id):
            assert card_id == "card-one"
            return deepcopy(self.card)

        def add_mp3(self, card_id, chapter_key, file_path, *, dry_run, title, on_reserved, on_media_hash, on_failure=None):
            assert (card_id, chapter_key, dry_run, title) == (
                "card-one", "new", False, "Chosen Artist — Chosen Song",
            )
            assert Path(file_path).is_file()
            self.add_calls += 1
            on_reserved("fresh-track", "fresh-chapter")
            assert store.recoverable_jobs()[0]["track_key"] == "fresh-track"
            on_media_hash("m" * 43)
            assert store.recoverable_jobs()[0]["media_hash"] == "m" * 43
            self.card["content"]["chapters"].append({
                "key": "fresh-chapter", "title": title, "tracks": [
                    {"key": "fresh-track", "title": title, "trackUrl": "yoto:#" + "m" * 43},
                ],
            })
            return self.get_playlist(card_id)

        def upload_icon(self, file_path, *, auto_convert, dry_run):
            assert Path(file_path).is_file() and auto_convert and dry_run is False
            return {"mediaId": "new-icon"}

        def set_track_icon(self, card_id, track_key, media_id, *, dry_run):
            assert (card_id, track_key, media_id, dry_run) == (
                "card-one", "fresh-track", "new-icon", False,
            )
            chapter = self.card["content"]["chapters"][-1]
            chapter["tracks"][0]["display"] = {"icon16x16": "yoto:#new-icon"}
            chapter["display"] = {"icon16x16": "yoto:#new-icon"}
            return self.get_playlist(card_id)

    def prepare(video_id, upload_root, **_kwargs):
        mp3 = upload_root / "song.mp3"
        avatar = upload_root / "avatar.jpg"
        mp3.write_bytes(b"mp3")
        avatar.write_bytes(b"jpeg")
        return {
            "video_id": video_id, "source_title": "Original Video", "channel_id": "UCfake",
            "channel_name": "Video Channel", "artist": "Chosen Artist", "title": "Chosen Song",
            "title_label": "Chosen Artist — Chosen Song", "album": "",
            "metadata_source": "user_override", "metadata_score": None,
            "requires_review": False, "warnings": [],
            "mp3_path": str(mp3), "avatar_path": str(avatar),
        }

    client = FakeClient()
    coordinator = YouTubeCoordinator(client, store, root, allow_writes=True, prepare=prepare)
    queued = coordinator.submit(
        "card-one", "abcdefghijk", dry_run=False,
        artist="Chosen Artist", song_name="Chosen Song",
    )
    completed = coordinator.wait(queued["job_id"], timeout=3)
    assert completed["status"] == "complete"
    assert completed["remote_verified"] is True
    assert completed["audio_status"] == "verified"
    assert completed["icon_status"] == "assigned"
    assert completed["track_key"] == "fresh-track"
    assert completed["chapter_key"] == "fresh-chapter"
    assert client.add_calls == 1
    assert client.get_playlist("card-one")["content"]["chapters"][0] == original
    assert coordinator.submit("card-one", "abcdefghijk", dry_run=False,
                              artist="Chosen Artist", song_name="Chosen Song")["job_id"] == queued["job_id"]
    assert not (root / "song.mp3").exists()
    coordinator.close()


def test_missing_channel_avatar_blocks_audio_write_before_any_yoto_mutation(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    store = JobStore(tmp_path / "private-jobs")

    def prepare(video_id, upload_root, **_kwargs):
        mp3 = upload_root / "song.mp3"
        mp3.write_bytes(b"mp3")
        return {
            "video_id": video_id, "source_title": "Original Video", "channel_id": "UCfake",
            "channel_name": "Video Channel", "artist": "Video Artist", "title": "Song",
            "title_label": "Video Artist — Song", "album": "", "metadata_source": "video_title_artist_song",
            "metadata_score": None, "requires_review": False, "warnings": ["Channel avatar unavailable"],
            "mp3_path": str(mp3), "avatar_path": None,
        }

    coordinator = YouTubeCoordinator(
        NoYotoWrites(), store, root, allow_writes=True, prepare=prepare,
    )
    job = coordinator.submit("card-one", "abcdefghijk", dry_run=False)
    failed = coordinator.wait(job["job_id"], timeout=3)
    assert failed["status"] == "failed"
    assert "avatar" in failed["error"].lower()
    assert failed.get("track_key") is None
    coordinator.close()


@pytest.mark.parametrize("failure_phase", ["assignment", "upload_timeout"])
def test_restart_resumes_pending_icon_without_readding_audio(tmp_path: Path, failure_phase: str):
    root = tmp_path / "uploads"
    root.mkdir()
    store = JobStore(tmp_path / "private-jobs")

    class FlakyIconClient:
        def __init__(self):
            self.card = {"cardId": "card-one", "content": {"chapters": []}}
            self.add_calls = 0
            self.icon_upload_calls = 0
            self.icon_assignment_calls = 0

        def get_playlist(self, _card_id):
            return deepcopy(self.card)

        def add_mp3(self, _card_id, _chapter, _file, *, dry_run, title, on_reserved, on_media_hash, on_failure=None):
            assert dry_run is False
            self.add_calls += 1
            on_reserved("new-track", "new-chapter")
            on_media_hash("m" * 43)
            self.card["content"]["chapters"].append({
                "key": "new-chapter", "title": title,
                "tracks": [{"key": "new-track", "title": title, "trackUrl": "yoto:#" + "m" * 43}],
            })
            return self.get_playlist("card-one")

        def upload_icon(self, _path, *, auto_convert, dry_run):
            assert auto_convert and dry_run is False
            self.icon_upload_calls += 1
            if failure_phase == "upload_timeout":
                raise TimeoutError("icon uploaded but response was lost")
            return {"mediaId": "new-icon"}

        def set_track_icon(self, _card_id, _track_key, _icon_id, *, dry_run):
            assert dry_run is False
            self.icon_assignment_calls += 1
            if failure_phase == "assignment" and self.icon_assignment_calls == 1:
                raise RuntimeError("transient propagation lag")
            self.card["content"]["chapters"][0]["tracks"][0]["display"] = {
                "icon16x16": "yoto:#new-icon",
            }
            self.card["content"]["chapters"][0]["display"] = {
                "icon16x16": "yoto:#new-icon",
            }
            return self.get_playlist("card-one")

    def prepare(video_id, upload_root, **_kwargs):
        mp3, avatar = upload_root / "song.mp3", upload_root / "avatar.jpg"
        mp3.write_bytes(b"mp3")
        avatar.write_bytes(b"jpg")
        return {
            "video_id": video_id, "source_title": "Original Video", "channel_id": "UCfake",
            "channel_name": "Video Channel", "artist": "Artist", "title": "Song",
            "title_label": "Artist — Song", "album": "", "metadata_source": "channel_title",
            "metadata_score": None, "requires_review": False, "warnings": [],
            "mp3_path": str(mp3), "avatar_path": str(avatar),
        }

    client = FlakyIconClient()
    first = YouTubeCoordinator(client, store, root, allow_writes=True, prepare=prepare)
    job = first.submit("card-one", "abcdefghijk", dry_run=False)
    partial = first.wait(job["job_id"], timeout=3)
    assert partial["status"] == "audio_added_icon_pending"
    assert partial["track_key"] == "new-track"
    assert partial["audio_status"] == "verified"
    assert partial.get("icon_media_id") == ("new-icon" if failure_phase == "assignment" else None)
    first.close()

    restarted = YouTubeCoordinator(
        client, JobStore(tmp_path / "private-jobs"), root, allow_writes=True,
        prepare=lambda *_args, **_kwargs: pytest.fail("do not reacquire source"),
    )
    if failure_phase == "upload_timeout":
        with pytest.raises(ValueError, match="icon_media_id"):
            restarted.resume(job["job_id"])
        restarted.resume(job["job_id"], icon_media_id="new-icon")
    else:
        restarted.resume(job["job_id"])
    completed = restarted.wait(job["job_id"], timeout=3)
    assert completed["status"] == "complete"
    assert completed["icon_status"] == "assigned"
    assert client.add_calls == 1
    assert client.icon_upload_calls == 1
    assert client.icon_assignment_calls == (2 if failure_phase == "assignment" else 1)
    restarted.close()


def test_post_applied_then_timed_out_reconciles_exact_audio_without_second_add(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    store = JobStore(tmp_path / "private-jobs")

    class AppliedThenTimeout:
        def __init__(self):
            self.card = {"cardId": "card-one", "content": {"chapters": []}}
            self.add_calls = 0

        def get_playlist(self, _card_id):
            return deepcopy(self.card)

        def add_mp3(self, _card_id, _chapter, _path, *, dry_run, title, on_reserved, on_media_hash, on_failure=None):
            assert dry_run is False
            self.add_calls += 1
            on_reserved("exact-track", "exact-chapter")
            on_media_hash("m" * 43)
            self.card["content"]["chapters"].append({
                "key": "exact-chapter", "title": title,
                "tracks": [{"key": "exact-track", "title": title, "trackUrl": "yoto:#" + "m" * 43}],
            })
            raise TimeoutError("readback failed after POST")

        def upload_icon(self, _path, *, auto_convert, dry_run):
            assert auto_convert and dry_run is False
            return {"mediaId": "icon-exact"}

        def set_track_icon(self, _card_id, _track_key, _icon_id, *, dry_run):
            assert dry_run is False
            chapter = self.card["content"]["chapters"][0]
            chapter["tracks"][0]["display"] = {"icon16x16": "yoto:#icon-exact"}
            chapter["display"] = {"icon16x16": "yoto:#icon-exact"}
            return self.get_playlist("card-one")

    def prepare(video_id, upload_root, **_kwargs):
        mp3, avatar = upload_root / "song.mp3", upload_root / "avatar.jpg"
        mp3.write_bytes(b"mp3")
        avatar.write_bytes(b"jpg")
        return {
            "video_id": video_id, "source_title": "Artist - Song", "channel_id": "UCfake",
            "channel_name": "Artist", "artist": "Artist", "title": "Song", "album": "",
            "title_label": "Artist — Song", "metadata_source": "video_title_artist_song",
            "metadata_score": None, "requires_review": False, "warnings": [],
            "mp3_path": str(mp3), "avatar_path": str(avatar),
        }

    client = AppliedThenTimeout()
    coordinator = YouTubeCoordinator(client, store, root, allow_writes=True, prepare=prepare)
    job = coordinator.submit("card-one", "abcdefghijk", dry_run=False)
    completed = coordinator.wait(job["job_id"], timeout=3)
    assert completed["status"] == "complete"
    assert completed["remote_verified"] is True
    assert completed["track_key"] == "exact-track"
    assert client.add_calls == 1
    assert len(client.card["content"]["chapters"]) == 1
    coordinator.close()


@pytest.mark.parametrize("interrupt_retry_before_reservation", [False, True])
def test_reserved_key_without_media_hash_retries_only_after_exact_absence_readback(
    tmp_path: Path, interrupt_retry_before_reservation: bool,
):
    root = tmp_path / "uploads"
    root.mkdir()
    store = JobStore(tmp_path / "private-jobs")

    class BeforePostFailure:
        def __init__(self):
            self.card = {"content": {"chapters": []}}
            self.add_calls = 0

        def get_playlist(self, _card_id):
            return deepcopy(self.card)

        def add_mp3(self, _card_id, _chapter, _path, *, dry_run, title, on_reserved, on_media_hash, on_failure=None):
            assert dry_run is False and title == "Artist — Song"
            self.add_calls += 1
            if self.add_calls == 1:
                on_reserved("stale-track", "stale-chapter")
                raise RuntimeError("upload failed before media hash; POST cannot have started")
            if self.add_calls == 2 and interrupt_retry_before_reservation:
                raise RuntimeError("retry interrupted before a second reservation")
            on_reserved("new-track", "new-chapter")
            on_media_hash("m" * 43)
            self.card["content"]["chapters"].append({
                "key": "new-chapter", "title": title,
                "tracks": [{"key": "new-track", "title": title, "trackUrl": "yoto:#" + "m" * 43}],
            })
            return self.get_playlist("card-one")

        def upload_icon(self, _path, *, auto_convert, dry_run):
            assert auto_convert and dry_run is False
            return {"mediaId": "icon-new"}

        def set_track_icon(self, _card_id, _track_key, _icon_id, *, dry_run):
            assert dry_run is False
            chapter = self.card["content"]["chapters"][0]
            chapter["tracks"][0]["display"] = {"icon16x16": "yoto:#icon-new"}
            chapter["display"] = {"icon16x16": "yoto:#icon-new"}
            return self.get_playlist("card-one")

    def prepare(video_id, upload_root, **_kwargs):
        mp3, avatar = upload_root / "song.mp3", upload_root / "avatar.jpg"
        mp3.write_bytes(b"mp3")
        avatar.write_bytes(b"jpg")
        return {
            "video_id": video_id, "source_title": "Artist - Song", "channel_id": "UCfake",
            "channel_name": "Artist", "artist": "Artist", "title": "Song", "album": "",
            "title_label": "Artist — Song", "metadata_source": "video_title_artist_song",
            "metadata_score": None, "requires_review": False, "warnings": [],
            "mp3_path": str(mp3), "avatar_path": str(avatar),
        }

    client = BeforePostFailure()
    first = YouTubeCoordinator(client, store, root, allow_writes=True, prepare=prepare)
    admitted = first.submit("card-one", "abcdefghijk", dry_run=False)
    stuck = first.wait(admitted["job_id"], timeout=3)
    assert stuck["status"] == "audio_uncertain" and stuck["track_key"] == "stale-track"
    assert stuck.get("media_hash") is None and client.add_calls == 1
    first.close()

    restarted = YouTubeCoordinator(client, JobStore(tmp_path / "private-jobs"), root,
                                  allow_writes=True,
                                  prepare=lambda *_args, **_kwargs: pytest.fail("must reuse staged source"))
    restarted.resume(admitted["job_id"])
    done = restarted.wait(admitted["job_id"], timeout=3)
    if interrupt_retry_before_reservation:
        assert done["status"] == "audio_uncertain" and done["track_key"] == "stale-track"
        assert done.get("media_hash") is None
        restarted.resume(admitted["job_id"])
        done = restarted.wait(admitted["job_id"], timeout=3)
    assert done["status"] == "complete" and done["track_key"] == "new-track"
    assert done["remote_verified"] is True and client.add_calls == (
        3 if interrupt_retry_before_reservation else 2
    )
    assert len(client.card["content"]["chapters"]) == 1
    restarted.close()


@pytest.mark.parametrize("restarted_status", ["audio_uncertain", "running"])
def test_uncertain_audio_waits_for_exact_remote_readback_then_resumes_without_readd(
    tmp_path: Path, restarted_status: str,
):
    root = tmp_path / "uploads"
    root.mkdir()
    store = JobStore(tmp_path / "private-jobs")

    class DelayedReadback:
        def __init__(self):
            self.visible = False
            self.add_calls = 0
            self.chapter = {
                "key": "exact-chapter", "title": "Artist — Song",
                "tracks": [{
                    "key": "exact-track", "title": "Artist — Song", "trackUrl": "yoto:#" + "m" * 43,
                }],
            }

        def get_playlist(self, _card_id):
            return {"cardId": "card-one", "content": {
                "chapters": [deepcopy(self.chapter)] if self.visible else [],
            }}

        def add_mp3(self, _card_id, _chapter, _path, *, dry_run, title, on_reserved, on_media_hash, on_failure=None):
            assert dry_run is False and title == "Artist — Song"
            self.add_calls += 1
            on_reserved("exact-track", "exact-chapter")
            on_media_hash("m" * 43)
            raise TimeoutError("POST may have applied")

        def upload_icon(self, _path, *, auto_convert, dry_run):
            assert auto_convert and dry_run is False
            return {"mediaId": "icon-exact"}

        def set_track_icon(self, _card_id, _track_key, _icon_id, *, dry_run):
            assert dry_run is False
            self.chapter["tracks"][0]["display"] = {"icon16x16": "yoto:#icon-exact"}
            self.chapter["display"] = {"icon16x16": "yoto:#icon-exact"}
            return self.get_playlist("card-one")

    def prepare(video_id, upload_root, **_kwargs):
        mp3, avatar = upload_root / "song.mp3", upload_root / "avatar.jpg"
        mp3.write_bytes(b"mp3")
        avatar.write_bytes(b"jpg")
        return {
            "video_id": video_id, "source_title": "Artist - Song", "channel_id": "UCfake",
            "channel_name": "Artist", "artist": "Artist", "title": "Song", "album": "",
            "title_label": "Artist — Song", "metadata_source": "video_title_artist_song",
            "metadata_score": None, "requires_review": False, "warnings": [],
            "mp3_path": str(mp3), "avatar_path": str(avatar),
        }

    client = DelayedReadback()
    first = YouTubeCoordinator(client, store, root, allow_writes=True, prepare=prepare)
    job = first.submit("card-one", "abcdefghijk", dry_run=False)
    pending = first.wait(job["job_id"], timeout=3)
    assert pending["status"] == "audio_uncertain"
    assert pending["track_key"] == "exact-track"
    assert client.add_calls == 1
    first.close()

    if restarted_status == "running":
        store.update(job["job_id"], status="running", stage="audio", write_intent="add_mp3")
    client.visible = True
    restarted = YouTubeCoordinator(
        client, JobStore(tmp_path / "private-jobs"), root, allow_writes=True,
        prepare=lambda *_args, **_kwargs: pytest.fail("must not redownload"),
    )
    restarted.resume(job["job_id"])
    done = restarted.wait(job["job_id"], timeout=3)
    assert done["status"] == "complete"
    assert done["remote_verified"] is True
    assert client.add_calls == 1
    restarted.close()


def test_same_title_from_different_video_flags_source_duplicate_before_audio_write(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    store = JobStore(tmp_path / "private-jobs")
    prior = store.submit("card-one", "abcdefghijk", dry_run=False)
    store.update(
        prior["job_id"], status="complete", stage="complete",
        metadata={"title_label": "Artist — Song"},
    )

    class ReviewThenWrite:
        def __init__(self):
            self.card = {"content": {"chapters": [{
                "key": "old-chapter", "title": "Artist - Song",
                "tracks": [{"key": "old-track", "title": "Artist - Song", "trackUrl": "yoto:#old"}],
            }]}}
            self.add_calls = 0

        def get_playlist(self, _card_id):
            return deepcopy(self.card)

        def add_mp3(self, _card_id, _chapter, _path, *, dry_run, title, on_reserved, on_media_hash, on_failure=None):
            assert dry_run is False
            self.add_calls += 1
            on_reserved("new-track", "new-chapter")
            on_media_hash("m" * 43)
            self.card["content"]["chapters"].append({
                "key": "new-chapter", "title": title,
                "tracks": [{"key": "new-track", "title": title, "trackUrl": "yoto:#" + "m" * 43}],
            })
            return self.get_playlist("card-one")

        def upload_icon(self, _path, *, auto_convert, dry_run):
            assert auto_convert and dry_run is False
            return {"mediaId": "icon-new"}

        def set_track_icon(self, _card_id, _track_key, _icon_id, *, dry_run):
            assert dry_run is False
            chapter = self.card["content"]["chapters"][-1]
            chapter["tracks"][0]["display"] = {"icon16x16": "yoto:#icon-new"}
            chapter["display"] = {"icon16x16": "yoto:#icon-new"}
            return self.get_playlist("card-one")

    def prepare(video_id, upload_root, **_kwargs):
        mp3, avatar = upload_root / "song.mp3", upload_root / "avatar.jpg"
        mp3.write_bytes(b"mp3")
        avatar.write_bytes(b"jpg")
        return {
            "video_id": video_id, "source_title": "Artist - Song", "channel_id": "UCfake",
            "channel_name": "Artist", "artist": "Artist", "title": "Song", "album": "",
            "title_label": "Artist — Song", "metadata_source": "video_title_artist_song",
            "metadata_score": None, "requires_review": False, "warnings": [],
            "mp3_path": str(mp3), "avatar_path": str(avatar),
        }

    client = ReviewThenWrite()
    coordinator = YouTubeCoordinator(client, store, root, allow_writes=True, prepare=prepare)
    queued = coordinator.submit("card-one", "zyxwvutsrqp", dry_run=False)
    flagged = coordinator.wait(queued["job_id"], timeout=3)
    assert flagged["status"] == "needs_duplicate_review"
    assert flagged["duplicate_sources"] == ["abcdefghijk"]
    assert flagged["duplicate_existing_track_keys"] == ["old-track"]
    assert flagged.get("track_key") is None
    assert Path(flagged["mp3_path"]).is_file()
    assert client.add_calls == 0
    with pytest.raises(ValueError, match="approve_duplicate"):
        coordinator.resume(queued["job_id"])
    client.card["content"]["chapters"][0]["tracks"].append({
        "key": "later-track", "title": "Artist — Song", "trackUrl": "yoto:#later",
    })
    coordinator.resume(queued["job_id"], approve_duplicate=True)
    changed = coordinator.wait(queued["job_id"], timeout=3)
    assert changed["status"] == "needs_duplicate_review"
    assert changed["duplicate_existing_track_keys"] == ["later-track", "old-track"]
    assert changed["duplicate_approved"] is False
    assert client.add_calls == 0
    coordinator.resume(queued["job_id"], approve_duplicate=True)
    completed = coordinator.wait(queued["job_id"], timeout=3)
    assert completed["status"] == "complete"
    assert completed["duplicate_sources"] == ["abcdefghijk"]
    assert client.add_calls == 1
    assert client.card["content"]["chapters"][0]["tracks"][0] == {
        "key": "old-track", "title": "Artist - Song", "trackUrl": "yoto:#old",
    }
    coordinator.close()


@pytest.mark.parametrize(
    ("operation", "reserve_first", "expected_code", "http_status", "category"),
    [
        ("local_preflight", False, "local_preflight_failed", None, "unexpected"),
        ("upload_url", True, "upload_url_failed", None, "unexpected"),
        ("upload_put", True, "upload_put_failed", None, "unexpected"),
        ("transcode", True, "transcode_failed", None, "unexpected"),
        ("upload_url", True, "upload_url_failed", 503, "yoto_api"),
    ],
)
def test_upload_failure_diagnostic_is_persisted_without_exception_secrets(
    tmp_path: Path, operation: str, reserve_first: bool, expected_code: str,
    http_status: int | None, category: str,
):
    root = tmp_path / "uploads"
    root.mkdir()
    store_root = tmp_path / "private-jobs"
    store = JobStore(store_root)
    secret_text = (
        "Bearer bearer-secret https://signed.invalid/upload?sig=signed-secret "
        "/private/absolute/path response-body-secret"
    )

    class FailingUploadClient:
        def get_playlist(self, _card_id):
            return {"content": {"chapters": []}}

        def add_mp3(
            self, _card_id, _chapter, _path, *, dry_run, title, on_reserved,
            on_media_hash, on_failure,
        ):
            assert dry_run is False
            if reserve_first:
                on_reserved("reserved-track", "reserved-chapter")
            exception = (
                YotoAPIError("HTTP status with Bearer token-secret response-body-secret", http_status=http_status)
                if http_status is not None else RuntimeError(secret_text)
            )
            on_failure(operation, exception)
            raise exception

    def prepare(_video_id, upload_root, **_kwargs):
        mp3, avatar = upload_root / "song.mp3", upload_root / "avatar.jpg"
        mp3.write_bytes(b"mp3")
        avatar.write_bytes(b"jpg")
        return {
            "title_label": "Artist — Song", "warnings": [],
            "mp3_path": str(mp3), "avatar_path": str(avatar),
        }

    coordinator = YouTubeCoordinator(
        FailingUploadClient(), store, root, allow_writes=True, prepare=prepare,
    )
    queued = coordinator.submit("card-one", "abcdefghijk", dry_run=False)
    failed = coordinator.wait(queued["job_id"], timeout=3)
    persisted = JobStore(store_root).get(queued["job_id"])

    assert failed["status"] == ("audio_uncertain" if reserve_first else "failed")
    diagnostic = persisted["diagnostic"]
    assert diagnostic == {
        "stage": "audio_upload",
        "operation": operation,
        "http_status": http_status,
        "exception_category": category,
        "code": expected_code,
        "attempt": "initial",
        "message": {
            "local_preflight": "Prepared audio files failed local validation.",
            "upload_url": "Audio upload URL request failed.",
            "upload_put": "Audio file upload failed.",
            "transcode": "Audio transcoding failed.",
        }[operation],
    }
    sensitive_values = (
        "bearer-secret", "signed.invalid", "signed-secret", "/private/absolute/path",
        "response-body-secret", "token-secret",
    )
    for value in diagnostic.values():
        assert not any(secret in str(value) for secret in sensitive_values)
    assert failed["error"] == (
        "Audio write outcome is uncertain; no automatic retry"
        if reserve_first else "YouTube job failed"
    )
    assert "audio_source" not in persisted
    if reserve_first:
        assert failed["track_key"] == "reserved-track"
        assert failed.get("media_hash") is None
    coordinator.close()


def test_resumed_upload_failure_replaces_diagnostic_with_resume_context(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    store_root = tmp_path / "private-jobs"
    store = JobStore(store_root)
    operations = iter(["upload_url", "transcode"])
    calls = []

    class FailingUploadClient:
        def get_playlist(self, _card_id):
            return {"content": {"chapters": []}}

        def add_mp3(
            self, _card_id, _chapter, _path, *, dry_run, title, on_reserved,
            on_media_hash, on_failure,
        ):
            calls.append("add_mp3")
            operation = next(operations)
            if len(calls) == 1:
                on_reserved("reserved-track", "reserved-chapter")
            exception = TimeoutError("Bearer secret; signed URL https://secret.invalid")
            on_failure(operation, exception)
            raise exception

    def prepare(_video_id, upload_root, **_kwargs):
        mp3, avatar = upload_root / "song.mp3", upload_root / "avatar.jpg"
        mp3.write_bytes(b"mp3")
        avatar.write_bytes(b"jpg")
        return {"title_label": "Artist — Song", "warnings": [],
                "mp3_path": str(mp3), "avatar_path": str(avatar)}

    client = FailingUploadClient()
    first = YouTubeCoordinator(client, store, root, allow_writes=True, prepare=prepare)
    job = first.submit("card-one", "abcdefghijk", dry_run=False)
    failed = first.wait(job["job_id"], timeout=3)
    assert failed["diagnostic"]["operation"] == "upload_url"
    first.close()

    resumed = YouTubeCoordinator(
        client, JobStore(store_root), root, allow_writes=True,
        prepare=lambda *_args, **_kwargs: pytest.fail("resume must use staged audio"),
    )
    resumed.resume(job["job_id"])
    resumed_failed = resumed.wait(job["job_id"], timeout=3)
    assert resumed_failed["status"] == "audio_uncertain"
    assert resumed_failed["track_key"] == "reserved-track"
    assert resumed_failed.get("media_hash") is None
    assert resumed_failed["diagnostic"] == {
        "stage": "audio_upload",
        "operation": "transcode",
        "http_status": None,
        "exception_category": "timeout",
        "code": "transcode_failed",
        "attempt": "resume",
        "message": "Audio transcoding failed.",
    }
    assert resumed_failed["error"] == "Audio write outcome is uncertain; no automatic retry"
    assert calls == ["add_mp3", "add_mp3"]
    assert "secret" not in repr(resumed_failed["diagnostic"])
    resumed.close()


@pytest.mark.parametrize("audio_source", ["existing_yoto_media", "uploaded"])
def test_verified_audio_source_is_durable_and_non_warning(tmp_path: Path, audio_source: str):
    root = tmp_path / "uploads"
    root.mkdir()
    store_root = tmp_path / "private-jobs"
    store = JobStore(store_root)

    class SourceAwareClient:
        def __init__(self):
            self.card = {"cardId": "card-one", "content": {"chapters": []}}

        def get_playlist(self, _card_id):
            return deepcopy(self.card)

        def add_mp3(
            self, _card_id, _chapter, _path, *, dry_run, title, on_reserved,
            on_media_hash, on_audio_source, on_failure=None,
        ):
            assert dry_run is False
            on_reserved("source-track", "source-chapter")
            on_media_hash("s" * 43)
            self.card["content"]["chapters"].append({
                "key": "source-chapter", "title": title,
                "tracks": [{
                    "key": "source-track", "title": title, "trackUrl": "yoto:#" + "s" * 43,
                }],
            })
            verified = self.get_playlist("card-one")
            on_audio_source(audio_source)
            return verified

        def upload_icon(self, _path, *, auto_convert, dry_run):
            assert auto_convert and dry_run is False
            return {"mediaId": "source-icon"}

        def set_track_icon(self, _card_id, _track_key, _icon_id, *, dry_run):
            assert dry_run is False
            chapter = self.card["content"]["chapters"][0]
            chapter["display"] = {"icon16x16": "yoto:#source-icon"}
            chapter["tracks"][0]["display"] = {"icon16x16": "yoto:#source-icon"}
            return self.get_playlist("card-one")

    def prepare(_video_id, upload_root, **_kwargs):
        mp3, avatar = upload_root / "song.mp3", upload_root / "avatar.jpg"
        mp3.write_bytes(b"mp3")
        avatar.write_bytes(b"jpg")
        return {
            "title_label": "Artist — Song", "warnings": [],
            "mp3_path": str(mp3), "avatar_path": str(avatar),
        }

    coordinator = YouTubeCoordinator(
        SourceAwareClient(), store, root, allow_writes=True, prepare=prepare,
    )
    queued = coordinator.submit("card-one", "abcdefghijk", dry_run=False)
    completed = coordinator.wait(queued["job_id"], timeout=3)
    durable = JobStore(store_root).get(queued["job_id"])

    assert completed["status"] == "complete"
    assert completed["audio_source"] == audio_source
    assert durable["audio_source"] == audio_source
    assert "audio_source" not in completed["warnings"]
    coordinator.close()


def test_guarded_resume_persists_existing_yoto_audio_source_only_after_readback(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    store_root = tmp_path / "private-jobs"
    store = JobStore(store_root)

    class RetryClient:
        def __init__(self):
            self.card = {"cardId": "card-one", "content": {"chapters": []}}
            self.add_calls = 0

        def get_playlist(self, _card_id):
            return deepcopy(self.card)

        def add_mp3(
            self, _card_id, _chapter, _path, *, dry_run, title, on_reserved,
            on_media_hash, on_audio_source, on_failure=None,
        ):
            assert dry_run is False
            self.add_calls += 1
            if self.add_calls == 1:
                on_reserved("reserved-track", "reserved-chapter")
                raise RuntimeError("pre-POST failure")
            on_reserved("reused-track", "reused-chapter")
            on_media_hash("r" * 43)
            self.card["content"]["chapters"].append({
                "key": "reused-chapter", "title": title,
                "tracks": [{
                    "key": "reused-track", "title": title, "trackUrl": "yoto:#" + "r" * 43,
                }],
            })
            verified = self.get_playlist("card-one")
            on_audio_source("existing_yoto_media")
            return verified

        def upload_icon(self, _path, *, auto_convert, dry_run):
            assert auto_convert and dry_run is False
            return {"mediaId": "reused-icon"}

        def set_track_icon(self, _card_id, _track_key, _icon_id, *, dry_run):
            assert dry_run is False
            chapter = self.card["content"]["chapters"][0]
            chapter["display"] = {"icon16x16": "yoto:#reused-icon"}
            chapter["tracks"][0]["display"] = {"icon16x16": "yoto:#reused-icon"}
            return self.get_playlist("card-one")

    def prepare(_video_id, upload_root, **_kwargs):
        mp3, avatar = upload_root / "song.mp3", upload_root / "avatar.jpg"
        mp3.write_bytes(b"mp3")
        avatar.write_bytes(b"jpg")
        return {
            "title_label": "Artist — Song", "warnings": [],
            "mp3_path": str(mp3), "avatar_path": str(avatar),
        }

    client = RetryClient()
    first = YouTubeCoordinator(client, store, root, allow_writes=True, prepare=prepare)
    queued = first.submit("card-one", "abcdefghijk", dry_run=False)
    uncertain = first.wait(queued["job_id"], timeout=3)
    assert uncertain["status"] == "audio_uncertain"
    assert "audio_source" not in uncertain
    first.close()

    resumed = YouTubeCoordinator(
        client, JobStore(store_root), root, allow_writes=True,
        prepare=lambda *_args, **_kwargs: pytest.fail("guarded resume must reuse staged source"),
    )
    resumed.resume(queued["job_id"])
    completed = resumed.wait(queued["job_id"], timeout=3)
    durable = JobStore(store_root).get(queued["job_id"])

    assert completed["status"] == "complete"
    assert completed["audio_source"] == "existing_yoto_media"
    assert durable["audio_source"] == "existing_yoto_media"
    assert client.add_calls == 2
    assert len(client.card["content"]["chapters"]) == 1
    resumed.close()


def test_source_interval_is_admitted_persisted_and_forwarded_to_preview(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    store = JobStore(tmp_path / "private-jobs")
    prepared_calls = []

    def prepare(video_id, upload_root, **kwargs):
        prepared_calls.append((video_id, kwargs))
        mp3 = upload_root / "clip.mp3"
        avatar = upload_root / "avatar.jpg"
        mp3.write_bytes(b"mp3")
        avatar.write_bytes(b"jpg")
        return {
            "title_label": "Artist — Song", "warnings": [],
            "mp3_path": str(mp3), "avatar_path": str(avatar),
            "source_interval": {
                "start_ms": kwargs["start_ms"], "end_ms": 30_000,
                "effective_duration_ms": 10_000,
            },
        }

    coordinator = YouTubeCoordinator(
        NoYotoWrites(), store, root, allow_writes=False, prepare=prepare,
    )
    queued = coordinator.submit(
        "card-one", "abcdefghijk", start_time="0:20", end_time="0:30",
    )
    completed = coordinator.wait(queued["job_id"], timeout=3)

    assert (queued["start_ms"], queued["end_ms"]) == (20_000, 30_000)
    assert prepared_calls == [("abcdefghijk", {
        "artist": None, "song_name": None, "start_ms": 20_000, "end_ms": 30_000,
    })]
    assert completed["metadata"]["source_interval"] == {
        "start_ms": 20_000, "end_ms": 30_000, "effective_duration_ms": 10_000,
    }
    assert coordinator.get(queued["job_id"])["start_ms"] == 20_000
    coordinator.close()


def test_invalid_range_is_rejected_before_coordinator_admission(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    store = JobStore(tmp_path / "private-jobs")
    coordinator = YouTubeCoordinator(
        NoYotoWrites(), store, root, allow_writes=False,
        prepare=lambda *_args, **_kwargs: pytest.fail("invalid range must not start source work"),
    )

    with pytest.raises(ValueError, match="timestamp|range|time"):
        coordinator.submit("card-one", "abcdefghijk", start_time="62")

    assert store.all_jobs() == []
    coordinator.close()


def test_source_only_resume_reuses_the_persisted_source_interval(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    store = JobStore(tmp_path / "private-jobs")
    interrupted = store.submit(
        "card-one", "abcdefghijk", dry_run=True,
        start_time="0:20", end_time="0:40",
    )
    store.update(interrupted["job_id"], status="running", stage="source")
    prepare_calls = []

    def prepare(video_id, _root, **kwargs):
        prepare_calls.append((video_id, kwargs))
        return {"title_label": "Artist — Song", "warnings": []}

    coordinator = YouTubeCoordinator(
        NoYotoWrites(), JobStore(tmp_path / "private-jobs"), root,
        allow_writes=False, prepare=prepare,
    )
    coordinator.resume(interrupted["job_id"])
    completed = coordinator.wait(interrupted["job_id"], timeout=3)

    assert completed["status"] == "complete"
    assert prepare_calls == [("abcdefghijk", {
        "artist": None, "song_name": None, "start_ms": 20_000, "end_ms": 40_000,
    })]
    assert completed["metadata"]["source_interval"] == {
        "start_ms": 20_000, "end_ms": 40_000,
    }
    coordinator.close()


def test_same_label_from_another_interval_of_same_video_requires_duplicate_review(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    store = JobStore(tmp_path / "private-jobs")
    previous = store.submit(
        "card-one", "abcdefghijk", dry_run=False,
        start_time="0:20", end_time="0:30",
    )
    store.update(
        previous["job_id"], status="complete", stage="complete",
        metadata={"title_label": "Artist — Song"},
    )
    prepare_calls = []
    add_calls = []

    class NoUploadClient:
        def get_playlist(self, _card_id):
            return {"content": {"chapters": []}}

        def add_mp3(self, *_args, **_kwargs):
            add_calls.append(_args)
            raise RuntimeError("fake Yoto upload deliberately stopped")

    def prepare(_video_id, upload_root, **kwargs):
        prepare_calls.append((kwargs["start_ms"], kwargs["end_ms"]))
        mp3, avatar = upload_root / "song.mp3", upload_root / "avatar.jpg"
        mp3.write_bytes(b"mp3")
        avatar.write_bytes(b"jpg")
        return {
            "title_label": "Artist — Song", "warnings": [],
            "mp3_path": str(mp3), "avatar_path": str(avatar),
            "source_interval": {
                "start_ms": kwargs["start_ms"], "end_ms": kwargs["end_ms"],
                "effective_duration_ms": 10_000,
            },
        }

    coordinator = YouTubeCoordinator(
        NoUploadClient(), store, root, allow_writes=True, prepare=prepare,
    )
    queued = coordinator.submit(
        "card-one", "abcdefghijk", dry_run=False,
        start_time="0:40", end_time="0:50",
    )
    flagged = coordinator.wait(queued["job_id"], timeout=3)

    assert flagged["status"] == "needs_duplicate_review"
    assert flagged["duplicate_sources"] == ["abcdefghijk"]
    assert flagged["duplicate_source_intervals"] == [{
        "video_id": "abcdefghijk", "start_ms": 20_000, "end_ms": 30_000,
    }]
    assert flagged["start_ms"] == 40_000
    assert flagged["metadata"]["source_interval"]["start_ms"] == 40_000
    coordinator.resume(queued["job_id"], approve_duplicate=True)
    resumed = coordinator.wait(queued["job_id"], timeout=3)
    assert prepare_calls == [(40_000, 50_000)]
    assert len(add_calls) == 1
    assert resumed["start_ms"] == 40_000 and resumed["end_ms"] == 50_000
    assert resumed["metadata"]["source_interval"]["start_ms"] == 40_000
    coordinator.close()
