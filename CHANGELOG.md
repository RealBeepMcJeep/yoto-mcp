# Changelog

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
