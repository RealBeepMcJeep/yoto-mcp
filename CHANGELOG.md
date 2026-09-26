# Changelog

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
