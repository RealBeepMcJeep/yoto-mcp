# yoto-mcp

An independent Python MCP server for managing your own Yoto Make Your Own playlists. Not affiliated with or endorsed by Yoto. Uses Yoto's public-client OAuth Authorization Code + PKCE flow and the documented developer API.

## Features

- List playlists and read chapters/tracks.
- Dry-run-first upload, rename, track removal, chapter ordering, owned-track export, and per-track icon tools. `remove_empty_chapter` accepts an exact chapter key and expected title, refuses nonempty chapters or overlapping shuffle ranges, and checks a fresh readback after the card save. It never deletes the underlying media or icon.
- `add_youtube` downloads one exact YouTube video or supported video URL with pinned yt-dlp, converts/validates an MP3 with FFmpeg, suggests metadata, and applies the channel avatar as a custom icon. Jobs have persisted status, previews, duplicate review and guarded recovery. The upload path reuses Yoto audio when an existing SHA-256 yields `uploadUrl: null`; verified jobs record `audio_source` as `existing_yoto_media` or `uploaded`. Respect rights and the services' terms when sourcing audio.
- Local stdio by default; opt-in bearer-protected Streamable HTTP for a trusted Docker network.

The code does not contain Yoto credentials. Set `YOTO_CLIENT_ID` to **your own** registered public Yoto OAuth client ID, with `http://127.0.0.1:8787/callback` registered as the redirect URI. First consent requires a browser on the same computer as the callback listener. Store the resulting OAuth session privately (`0600`); its refresh token rotates and the file must be writable. `YOTO_USER`/`YOTO_PASS` are not supported for unattended initial consent.

## Local stdio

With Python 3.12+ and [uv](https://docs.astral.sh/uv/), run `uv sync --frozen` then `uv run --frozen yoto-mcp`. The first Yoto tool call starts the one-time browser consent flow if no session exists. For jobs, configure private writable `YOTO_JOB_ROOT` and `YOTO_UPLOAD_ROOT`. Install `/usr/bin/ffmpeg` and `/usr/bin/ffprobe` with libmp3lame/Chromaprint; alternatively set `YOTO_FFMPEG` and `YOTO_FFPROBE` to trusted absolute executable paths. `ACOUSTID_API_KEY` is optional; without it, jobs fall back to video title/channel with a warning.

For writes, set `YOTO_ALLOW_WRITES=1` **and** explicitly request `dry_run=false` on the tool. Default previews do not modify Yoto. Whole-playlist updates lack an upstream compare-and-swap API: use one writer for each playlist, inspect duplicate/uncertain states, and never blindly retry uncertain writes.

## Container and deployment

See [the generic Compose example and setup guide](deploy/README.md). The image builds with FFmpeg/ffprobe, libmp3lame, Chromaprint and lockfile-pinned yt-dlp. The container's HTTP mode requires `YOTO_HTTP_TOKEN` (at least 32 characters) and `YOTO_ALLOWED_HOSTS`; `/healthz` is unauthenticated but validates Host. Never publish the MCP port directly to the Internet. Persist private writable session, jobs, and uploads mounts, and start with `YOTO_ALLOW_WRITES=0`. The Dockerfile's presence does not itself prove an image was published; check the GitHub Actions result and anonymous GHCR manifest for the tag before deploying.

The repo includes frozen Python tests, HTTP/stdin smoke probes, image build checks, and a GHCR publish workflow. The first new GHCR package defaults private even when this source repo is public; the package owner must explicitly make it public once, after reviewing the image, before anonymous pulls work.

No private session, media, family card state, or research artifacts belong in Git or the container build context.
