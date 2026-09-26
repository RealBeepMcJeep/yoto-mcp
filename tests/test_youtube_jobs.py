from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from yoto_mcp import youtube_jobs
from yoto_mcp.youtube_jobs import JobStore, JobStoreError


def test_submit_persists_default_job_and_get_survives_restart(tmp_path):
    root = tmp_path / "private-jobs"
    store = JobStore(root)

    submitted = store.submit("card-one", "abcdefghijk", dry_run=True)

    assert submitted["card_id"] == "card-one"
    assert submitted["video_id"] == "abcdefghijk"
    assert submitted["dry_run"] is True
    assert submitted["stage"] == "queued"
    assert submitted["status"] == "queued"
    assert submitted["resume_from"] == "source"
    assert submitted["audio_status"] == "pending"
    assert submitted["icon_status"] == "pending"
    assert submitted["job_id"]
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / ".lock").stat().st_mode & 0o777 == 0o600
    record_path = root / f"{submitted['job_id']}.json"
    assert record_path.stat().st_mode & 0o777 == 0o600

    restarted = JobStore(root)
    assert restarted.get(submitted["job_id"]) == submitted
    assert restarted.recoverable_jobs() == [submitted]


def test_update_persists_reserved_keys_and_partial_icon_recovery_state(tmp_path):
    root = tmp_path / "private-jobs"
    store = JobStore(root)
    submitted = store.submit("card-one", "abcdefghijk", dry_run=False)

    updated = store.update(
        submitted["job_id"],
        stage="icon",
        status="audio_added_icon_pending",
        resume_from="icon",
        audio_status="verified",
        icon_status="pending",
        track_key="track-reserved",
        chapter_key="chapter-reserved",
        media_hash="m" * 43,
    )

    restarted = JobStore(root)
    assert restarted.get(submitted["job_id"]) == updated
    assert updated["created_at"] == submitted["created_at"]
    assert updated["status"] == "audio_added_icon_pending"
    assert updated["resume_from"] == "icon"
    assert updated["track_key"] == "track-reserved"
    assert updated["chapter_key"] == "chapter-reserved"
    assert updated["media_hash"] == "m" * 43
    assert restarted.recoverable_jobs() == [updated]


def test_submit_is_idempotent_per_card_video_and_dry_run_mode(tmp_path):
    store = JobStore(tmp_path / "private-jobs")

    preview = store.submit("card-one", "abcdefghijk", dry_run=True)
    duplicate_preview = store.submit("card-one", "abcdefghijk", dry_run=True)
    write_job = store.submit("card-one", "abcdefghijk", dry_run=False)
    different_video = store.submit("card-one", "zyxwvutsrqp", dry_run=True)
    different_card = store.submit("card-two", "abcdefghijk", dry_run=True)

    assert duplicate_preview["job_id"] == preview["job_id"]
    assert write_job["job_id"] != preview["job_id"]
    assert different_video["job_id"] not in {preview["job_id"], write_job["job_id"]}
    assert different_card["job_id"] not in {preview["job_id"], write_job["job_id"]}
    assert write_job["dry_run"] is False
    assert len(store.recoverable_jobs()) == 4

def test_explicit_credits_are_persisted_and_idempotent_across_restart(tmp_path):
    root = tmp_path / "private-jobs"
    store = JobStore(root)
    first = store.submit(
        "card-one", "abcdefghijk", dry_run=False,
        artist="Chosen Artist", song_name="Chosen Song",
    )
    second = JobStore(root).submit(
        "card-one", "abcdefghijk", dry_run=False,
        artist="Chosen Artist", song_name="Chosen Song",
    )
    assert second == first
    assert first["artist"] == "Chosen Artist"
    assert first["song_name"] == "Chosen Song"

def test_same_video_rejects_changed_explicit_credits_instead_of_reusing_job(tmp_path):
    store = JobStore(tmp_path / "private-jobs")
    original = store.submit("card-one", "abcdefghijk", dry_run=False)
    with pytest.raises(ValueError, match="different artist/song_name"):
        store.submit(
            "card-one", "abcdefghijk", dry_run=False,
            artist="Other Artist", song_name="Other Song",
        )
    assert store.get(original["job_id"]) == original

def test_partial_explicit_credits_are_rejected_before_admission(tmp_path):
    store = JobStore(tmp_path / "private-jobs")
    with pytest.raises(ValueError, match="artist and song_name"):
        store.submit("card-one", "abcdefghijk", dry_run=False, artist="Only Artist")
    assert store.recoverable_jobs() == []


def test_job_store_rejects_symlink_traversal_in_root_path(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)

    with pytest.raises(JobStoreError, match="unsafe or invalid directory"):
        JobStore(alias / "jobs")

    assert not (outside / "jobs").exists()


def test_corrupt_json_record_fails_closed_with_store_error(tmp_path):
    root = tmp_path / "private-jobs"
    store = JobStore(root)
    job = store.submit("card-one", "abcdefghijk", dry_run=True)
    (root / f"{job['job_id']}.json").write_text("{truncated", encoding="utf-8")

    with pytest.raises(JobStoreError, match="invalid JSON"):
        store.get(job["job_id"])


def test_job_store_refuses_symlinked_job_record(tmp_path):
    root = tmp_path / "private-jobs"
    store = JobStore(root)
    job = store.submit("card-one", "abcdefghijk", dry_run=True)
    record = root / f"{job['job_id']}.json"
    outside_record = tmp_path / "outside.json"
    outside_record.write_text(json.dumps(job), encoding="utf-8")
    record.unlink()
    record.symlink_to(outside_record)

    with pytest.raises(JobStoreError, match="safely open"):
        store.get(job["job_id"])


def test_job_store_validates_public_identifiers_before_path_use(tmp_path):
    store = JobStore(tmp_path / "private-jobs")

    with pytest.raises(ValueError, match="video_id"):
        store.submit("card-one", "short", dry_run=True)
    with pytest.raises(ValueError, match="card_id"):
        store.submit("../outside", "abcdefghijk", dry_run=True)
    with pytest.raises(ValueError, match="job_id"):
        store.get("../outside")
    with pytest.raises(TypeError, match="dry_run"):
        store.submit("card-one", "abcdefghijk", dry_run=1)


def test_concurrent_admission_creates_one_durable_job(tmp_path):
    store = JobStore(tmp_path / "private-jobs")

    with ThreadPoolExecutor(max_workers=12) as executor:
        jobs = list(executor.map(
            lambda _index: store.submit("card-one", "abcdefghijk", dry_run=False),
            range(40),
        ))

    assert len({job["job_id"] for job in jobs}) == 1
    assert len(store.recoverable_jobs()) == 1


def test_job_store_refuses_public_repository_as_private_root():
    repository_root = Path(__file__).resolve().parents[1]

    with pytest.raises(ValueError, match="outside the public repository"):
        JobStore(repository_root)


def test_recoverable_jobs_are_in_creation_order(tmp_path, monkeypatch):
    generated_ids = iter(("f" * 32, "a" * 32, "0" * 32, "b" * 32))
    generated_times = iter(("2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"))
    monkeypatch.setattr(youtube_jobs.uuid, "uuid4", lambda: SimpleNamespace(hex=next(generated_ids)))
    monkeypatch.setattr(JobStore, "_now", staticmethod(lambda: next(generated_times)))
    store = JobStore(tmp_path / "private-jobs")

    oldest = store.submit("card-one", "abcdefghijk", dry_run=True)
    newest = store.submit("card-two", "abcdefghijk", dry_run=True)

    assert [job["job_id"] for job in store.recoverable_jobs()] == [
        oldest["job_id"], newest["job_id"]
    ]


def test_job_store_refuses_filesystem_root():
    with pytest.raises(ValueError, match="must not be the filesystem root"):
        JobStore(Path("/"))


def test_completed_jobs_leave_recovery_queue_and_identity_is_immutable(tmp_path):
    store = JobStore(tmp_path / "private-jobs")
    job = store.submit("card-one", "abcdefghijk", dry_run=False)

    completed = store.update(
        job["job_id"],
        status="complete",
        stage="complete",
        audio_status="verified",
        icon_status="assigned",
    )

    assert completed["status"] == "complete"
    assert store.recoverable_jobs() == []
    with pytest.raises(ValueError, match="immutable"):
        store.update(job["job_id"], video_id="lmnopqrstuv")


def test_all_jobs_includes_completed_provenance_for_duplicate_source_review(tmp_path):
    store = JobStore(tmp_path / "private-jobs")
    older = store.submit("card-one", "abcdefghijk", dry_run=False)
    store.update(older["job_id"], status="complete", stage="complete")
    newer = store.submit("card-one", "zyxwvutsrqp", dry_run=False)
    assert [job["job_id"] for job in store.all_jobs()] == [
        older["job_id"], newer["job_id"],
    ]
    assert [job["job_id"] for job in store.recoverable_jobs()] == [newer["job_id"]]
