# Changelog

## Remote MP3 upload links

- Added the HTTP-only `create_mp3_upload(filename?)` tool and a `PUT`/`POST /uploads/<secret>` route on the existing port. The authenticated tool mints a single-use link (15 minutes, 100 MiB) built from the Host the client used; the agent sends raw bytes with `curl -T` and no bearer header. Host allowlist, form/oversize/non-MP3 rejection (ffprobe), private `0600` storage under `YOTO_UPLOAD_ROOT/inbox/`, a returned `file_path` for `add_mp3`, and a 24-hour inbox sweep. The container smoke performs a real upload and replay rejection.

## Cancel YouTube jobs; repeated add_youtube retries pre-write failures

- Added `cancel_youtube_job(job_id)` for jobs with no reserved track, chapter or media hash (queued, preparing, failed, awaiting duplicate review, or a preview). It deletes staged files, and a running worker stops before its card write; the cancel check and the worker's write intent are one serialized job-store update. Refused once a track is reserved.
- Repeating an identical `add_youtube` request now retries a job that failed before any Yoto write (same job ID, stale error/diagnostic/warnings cleared) and starts a new job in place of a cancelled one; other states still return the existing job. Cancelled jobs are excluded from duplicate review.

## Retry jobs that failed before any Yoto write

- `add_youtube` returns the existing job for an identical request, so a job that failed before upload could not be retried. `resume_youtube_job` now restarts a `failed` job from source when it has no reserved track, chapter, media hash or write intent, clearing its stale error and diagnostic. Failed jobs with any write journal are still refused; the write gate still applies.

## add_youtube survives an unavailable channel avatar

- If preparation cannot obtain the uploader's channel avatar, `add_youtube` no longer fails before upload. The MP3 is uploaded and its exact track, chapter and media hash verified as before; the job completes with `icon_status: skipped_unavailable` and a sanitized warning giving the reason. A staged avatar that later goes missing or escapes the upload root still fails closed, and icon upload/assignment failures after audio is added remain resumable partial failures.

## Playlist rename

- Added `rename_playlist(card_id, title, dry_run=true)`: changes only the playlist (card) title, 1-100 printable characters, through the existing whole-card save; requires `YOTO_ALLOW_WRITES=1` plus `dry_run=false`, and confirms the title with a fresh read.

## Lyric transcription

- Added `transcribe_lyrics`, backed by a CPU-only whisper.cpp v1.9.4 build in the image (`GGML_NATIVE=OFF` + `GGML_CPU_ALL_VARIANTS` for runtime CPU dispatch) and the multilingual `base` model pinned by SHA-256. Runs with `-sns` (suppress non-speech tokens); without it Whisper labels most sung vocals as music and emits almost no words. Results are cached privately by audio hash + model. The image build proves the binary loads the model. Adds ~155 MB to the image.

## Lyric evidence lookup and job diagnostic cleanup

- Added read-only `lookup_lyric_evidence` backed by LRCLIB `GET /api/get`: artist/title required, album/duration optional. Returns an explicit status instead of raising on a miss, honors `Retry-After` on 429, identifies the client via `User-Agent`, and never echoes raw provider bodies in errors. No Yoto access, persistence or transcription.
- A YouTube job that recovered after an earlier upload-stage failure no longer keeps the stale `diagnostic` from that failed attempt once the audio write is confirmed by exact readback.

## Optional YouTube source-timeline ranges
- `add_youtube` accepts optional `start_time`/`end_time` on the original source timeline, validates the selected interval against decoded audio and checks the trimmed MP3's duration. Exact intervals are part of durable job identity, including recovery and duplicate review; old full-source jobs remain compatible. Source, fake-client, real FFmpeg clip and MCP schema tests cover the feature. Deployment and a live Yoto write require a separately verified image and explicit user operation.

## Guarded empty-chapter removal

- Added `remove_empty_chapter(card_id, chapter_key, expected_title, dry_run=true)`. A write requires `YOTO_ALLOW_WRITES=1` and explicit `dry_run=false`, an exact one-of-one empty chapter, a matching title and no shuffle range spanning the target. Other chapters/tracks remain in order, generated ordinal labels shift when applicable, and a fresh card readback checks the surviving identities, titles, icons, audio hashes and labels. No underlying media or icons are deleted.

## Reuse existing Yoto audio

- When Yoto reports that an audio SHA-256 already exists (`uploadUrl: null` plus an upload ID), skip the signed PUT, validate the existing transcode, and continue the guarded card save/readback. Bad IDs and incomplete or errored transcodes still fail closed; ordinary signed URLs retain host validation.
- Verified YouTube jobs report `audio_source: existing_yoto_media` or `uploaded`. Existing media is informational and separate from a playlist duplicate. A hosted run still requires the new image and an explicit guarded resume.

## Hosted upload diagnostics

- `get_youtube_job` now exposes durable, allowlisted `diagnostic` fields when an MP3 upload fails: upload operation, safe HTTP status/category, initial-vs-resume attempt, fixed code/message. Neither raw exception text nor response bodies, file paths, tokens, or signed upload URLs are included. The guarded no-blind-retry behavior is unchanged; diagnose the underlying upload failure after deploying this version.

## Initial public snapshot

- Yoto playlist MCP tools, dry-run-first writes, metadata/icon tooling, persisted YouTube jobs and guarded recovery.
- Optional authenticated Streamable HTTP alongside default stdio; private OAuth session and serialized in-process refresh.
- Non-root Python container with FFmpeg/ffprobe, libmp3lame, Chromaprint and pinned yt-dlp; frozen CI and GHCR publish workflows.

This public repository begins with a new history. No private account data or prior development history is included.
