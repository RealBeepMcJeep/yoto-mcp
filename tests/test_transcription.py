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
            if "-ss" not in args:
                with open(args[-1], "wb") as wav:
                    wav.truncate(44 + 32000 * runner.seconds)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if "-dl" in args:
            lang, p = runner.detections.pop(0) if runner.detections else ("en", 0.9)
            line = f"whisper_full: auto-detected language: {lang} (p = {p})\n" if lang else "no detection\n"
            return subprocess.CompletedProcess(args, 0, stdout="", stderr=line)
        return subprocess.CompletedProcess(args, runner.whisper_code, stdout=" placeholder line one\n\n placeholder line two\n", stderr="")

    runner.seconds = 120
    runner.whisper_code = 0
    runner.detections = []
    return root, env, runner, calls, model


def test_transcribes_then_serves_repeat_from_private_cache(setup):
    root, env, runner, calls, _ = setup
    first = transcribe_audio(root, "song.opus", env=env, runner=runner)
    assert first["text"] == "placeholder line one\nplaceholder line two"
    assert first["cache_hit"] is False
    assert first["audio_seconds"] == 120.0
    assert first["threads"] == 2
    whisper_args = calls[-1]
    assert whisper_args[whisper_args.index("-t") + 1] == "2"
    assert whisper_args[whisper_args.index("-l") + 1] == "en"
    assert "-sns" in whisper_args
    assert first["language"] == "en" and first["language_source"] == "detected"

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
    assert sum("-sns" in c for c in calls) == 1
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


def test_language_vote_sums_probabilities_over_inner_clips(setup):
    root, env, runner, calls, _ = setup
    runner.detections = [("ko", 0.6), ("en", 0.5), ("en", 0.4)]
    result = transcribe_audio(root, "song.opus", env=env, runner=runner)
    assert result["language"] == "en"
    clip_starts = [float(c[c.index("-ss") + 1]) for c in calls if "-ss" in c]
    assert clip_starts == [30.0, 48.0, 66.0]


def test_explicit_language_skips_detection_and_is_part_of_the_cache_key(setup):
    root, env, runner, calls, _ = setup
    result = transcribe_audio(root, "song.opus", language="es", env=env, runner=runner)
    assert result["language"] == "es" and result["language_source"] == "requested"
    assert not any("-dl" in c for c in calls)
    assert calls[-1][calls[-1].index("-l") + 1] == "es"
    calls.clear()
    assert transcribe_audio(root, "song.opus", language="en", env=env, runner=runner)["cache_hit"] is False
    assert transcribe_audio(root, "song.opus", language="es", env=env, runner=runner)["cache_hit"] is True


def test_failed_detection_falls_back_to_whisper_auto(setup):
    root, env, runner, _, _ = setup
    runner.detections = [(None, 0)] * 3
    result = transcribe_audio(root, "song.opus", env=env, runner=runner)
    assert result["language"] == "auto" and result["language_source"] == "whisper_auto"


def test_short_audio_detects_once_from_the_start(setup):
    root, env, runner, calls, _ = setup
    runner.seconds = 20
    transcribe_audio(root, "song.opus", env=env, runner=runner)
    assert [c[c.index("-ss") + 1] for c in calls if "-ss" in c] == ["0.00"]


@pytest.mark.parametrize("language", ["english", "EN", "e", "en;rm", ""])
def test_invalid_language_is_rejected_before_any_subprocess(setup, language):
    root, env, runner, calls, _ = setup
    with pytest.raises(ValueError, match="language"):
        transcribe_audio(root, "song.opus", language=language, env=env, runner=runner)
    assert calls == []
