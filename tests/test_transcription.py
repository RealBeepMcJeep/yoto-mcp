from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from yoto_mcp.transcription import TranscriptionError, transcribe_audio


@pytest.fixture
def setup(tmp_path: Path, monkeypatch):
    root = tmp_path / "uploads"
    root.mkdir()
    (root / "song.opus").write_bytes(b"opus-audio" * 50)
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("ffmpeg", "whisper-cli"):
        (tools / name).write_text("#!/bin/sh\n")
        (tools / name).chmod(0o755)
    model = tools / "ggml-base.bin"
    model.write_bytes(b"m" * 100)
    monkeypatch.setenv("YOTO_FFMPEG", str(tools / "ffmpeg"))
    env = {
        "YOTO_WHISPER_CLI": str(tools / "whisper-cli"),
        "YOTO_WHISPER_MODEL": str(model),
        "YOTO_TRANSCRIPT_CACHE": str(tmp_path / "cache"),
        "YOTO_WHISPER_THREADS": "2",
    }
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(args)
        if Path(args[0]).name == "ffmpeg":
            with open(args[-1], "wb") as wav:
                wav.truncate(44 + 32000 * runner.seconds)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, runner.whisper_code, stdout=" placeholder line one\n\n placeholder line two\n", stderr="")

    runner.seconds = 120
    runner.whisper_code = 0
    return root, env, runner, calls, model


def test_transcribes_then_serves_repeat_from_private_cache(setup):
    root, env, runner, calls, _ = setup
    first = transcribe_audio(root, "song.opus", env=env, runner=runner)
    assert first["text"] == "placeholder line one\nplaceholder line two"
    assert first["cache_hit"] is False
    assert first["audio_seconds"] == 120.0
    assert first["threads"] == 2
    whisper_args = calls[1]
    assert whisper_args[whisper_args.index("-t") + 1] == "2"
    assert whisper_args[whisper_args.index("-l") + 1] == "auto"
    assert "-sns" in whisper_args

    cache_files = list(Path(env["YOTO_TRANSCRIPT_CACHE"]).glob("*.json"))
    assert len(cache_files) == 1
    assert json.loads(cache_files[0].read_text())["audio_sha256"] == first["audio_sha256"]
    if os.name != "nt":
        assert stat.S_IMODE(cache_files[0].stat().st_mode) & 0o077 == 0

    calls.clear()
    second = transcribe_audio(root, "song.opus", env=env, runner=runner)
    assert second["cache_hit"] is True
    assert second["text"] == first["text"]
    assert calls == []


def test_refresh_and_model_change_bypass_the_cache(setup):
    root, env, runner, calls, model = setup
    transcribe_audio(root, "song.opus", env=env, runner=runner)
    calls.clear()
    assert transcribe_audio(root, "song.opus", env=env, runner=runner, refresh=True)["cache_hit"] is False
    assert len(calls) == 2
    model.write_bytes(b"m" * 200)
    assert transcribe_audio(root, "song.opus", env=env, runner=runner)["cache_hit"] is False


def test_audio_over_one_hour_is_rejected_before_whisper(setup):
    root, env, runner, calls, _ = setup
    runner.seconds = 3601
    with pytest.raises(TranscriptionError, match="longer than one hour"):
        transcribe_audio(root, "song.opus", env=env, runner=runner)
    assert [Path(c[0]).name for c in calls] == ["ffmpeg"]


def test_whisper_failure_is_sanitized_and_not_cached(setup):
    root, env, runner, _, _ = setup
    runner.whisper_code = 1
    with pytest.raises(TranscriptionError, match="failed or timed out"):
        transcribe_audio(root, "song.opus", env=env, runner=runner)
    assert not list(Path(env["YOTO_TRANSCRIPT_CACHE"]).glob("*.json"))


def test_missing_whisper_install_fails_clearly(setup):
    root, env, runner, calls, _ = setup
    env["YOTO_WHISPER_CLI"] = "/nonexistent/whisper-cli"
    with pytest.raises(TranscriptionError, match="YOTO_WHISPER_CLI"):
        transcribe_audio(root, "song.opus", env=env, runner=runner)
    assert calls == []


def test_paths_outside_upload_root_are_rejected_before_any_subprocess(setup, tmp_path: Path):
    root, env, runner, calls, _ = setup
    outside = tmp_path / "private.opus"
    outside.write_bytes(b"x" * 500)
    with pytest.raises(ValueError, match="inside YOTO_UPLOAD_ROOT"):
        transcribe_audio(root, str(outside), env=env, runner=runner)
    assert calls == []
