# DreamersClippers

Self-hosted real-time Twitch live stream and VOD auto-clipper with an integrated 9:16 vertical studio workflow (powered by the **StreamClippers v1.1** engine).

> Basic Twitch IRC + Streamlink live monitoring, VOD scanning, captions, thumbnails, and 9:16 rendering work **out of the box with no Twitch API keys required**. Optional Twitch Developer credentials unlock Helix Clip Surge detection, EventSub live status, Top Clips import, and publishing official Twitch Clips.

---

## Key Capabilities

- **Live Multi-Signal Hype Detection**: Combines Twitch IRC chat velocity and hype keywords, FFmpeg audio RMS dBFS bursts, OpenCV frame motion, and optional Twitch Helix Clip surge signals with adaptive quiet-chat weighting and anti-spam unique-chatter gating.
- **Rolling 140-Second MPEG-TS Ring Buffer**: Continuously records live streams via Streamlink + FFmpeg into bounded 4-second rolling segments (`35` segments) for instant `-c copy` snapshot remuxing on hype triggers or manual **Clip Now** (no re-encode during capture).
- **Opt-In Live Twitch Preview & Chat**: Toggle an embedded Twitch player and dark-popout chat directly inside the Live Radar panel while monitoring.
- **Twitch VOD Scanner & Top Clips Importer**: Scan public `https://twitch.tv/videos/<id>` VODs via `yt-dlp` for top non-overlapping hype peaks, or browse and import existing Twitch Top Clips directly into your Review Queue.
- **Smart Webcam & Gameplay Framing**: Automatically detects streamer facecam bounds (`split_top_cam`) or falls back to `blur_no_cam`, paired with interactive draggable/resizable crop boxes and saved per-channel presets.
- **Hardware-Accelerated 9:16 Export**: Renders `1080x1920` MP4s (`setsar=1`, optional `-14 LUFS` loudnorm audio) using `h264_nvenc` on NVIDIA GPUs with automatic `libx264` CPU fallback.
- **Local Closed Captions (`faster-whisper`)**: Generate word/segment captions locally before downloading, edit lines inline with live `<canvas>` preview, choose **Split-Seam** or **Lower-Safe** placement, burn subtitles directly into the 9:16 MP4, and export `.srt`.
- **9:16 Cover Thumbnail Capture**: Scrub to any moment in the Studio preview to capture and download a `1080x1920` `.jpg` cover thumbnail alongside your vertical export.
- **Optional Twitch Helix, EventSub & OAuth**: Connect a Twitch App (and optional OAuth user login) in Settings for EventSub WebSocket notifications (`stream.online`, `stream.offline`, `channel.raid`, `channel.hype_train.*`, `channel.cheer`), creating official Twitch Clips (`POST /helix/clips`, `POST /helix/clips/vod`), and placing stream markers. Credentials are stored locally in `data/twitch_credentials.json` (excluded by `.gitignore`).

---

## Requirements

- Windows 10 / 11 (or Linux/macOS with FFmpeg on `PATH`)
- Python `>= 3.12` + [`uv`](https://docs.astral.sh/uv/)
- Full FFmpeg & `ffprobe` build (on Windows, WinGet `Gyan.FFmpeg` is auto-resolved ahead of stripped `PATH` shims)
- Node.js `>= 20` + `npm`
- *(Optional)* NVIDIA GPU + drivers for `h264_nvenc` hardware encoding

---

## Quick Start

1. Clone the repository:

   ```powershell
   git clone https://github.com/Joebelle-01/dreamersclippers.git
   cd dreamersclippers
   ```

2. Install Python dependencies:

   ```powershell
   uv sync
   ```

3. Build the React/Vite frontend:

   ```powershell
   cd frontend
   npm install
   npm run build
   cd ..
   ```

4. Launch the Studio:

   ```powershell
   .\Start-StreamClippers.ps1
   ```

   Pass `-Dev` to run the Vite dev server on `http://127.0.0.1:5174` alongside the FastAPI backend on `http://127.0.0.1:8430`. Pass `-CheckOnly` to verify backend imports without starting the server.

---

## API & WebSocket Overview

- `GET /api/system/status` — FFmpeg/`ffprobe` paths, NVENC/GPU status, active monitor and clip counts
- `GET/POST/PATCH/DELETE /api/channels` — Channel monitor CRUD and per-channel thresholds/presets
- `POST /api/channels/{channel_id}/start|stop|clip-now` — Live monitor control and instant capture
- `POST /api/vods/scan` — Queue a background Twitch VOD hype scan
- `GET/PATCH/DELETE /api/clips` — Review Queue listing, trim/crop/caption updates, and delete
- `POST /api/clips/{clip_id}/detect-webcam|save-preset|captions/generate|thumbnail|render` — Framing detection, preset save, Whisper CC generation, thumbnail capture, and 9:16 MP4 export
- `GET /api/media/raw/{clip_id}`, `/export/{clip_id}`, `/thumb/{clip_id}`, `/export-thumb/{clip_id}`, `/srt/{clip_id}` — Media streaming (`206 Partial Content` Range support) & exports
- `GET/PUT /api/twitch/status|config`, `GET /api/twitch/oauth/start|callback`, `POST /api/twitch/oauth/disconnect`, `GET /api/twitch/clips/{channel_login}`, `POST /api/twitch/clips/import`, `POST /api/channels/{channel_id}/publish-clip|marker` — Optional Twitch Helix & OAuth endpoints
- `WS /ws/live` — Real-time telemetry (`hype_tick`, `chat_burst`, `clip_created`, `render_status`, `vod_progress`, `monitor_status`, `eventsub_event`)

---

## Configuration

Settings can be overridden via `STREAMCLIPPERS_*` environment variables or a local `.env` file (see `backend/config.py`):

- `STREAMCLIPPERS_DATA_DIR` (default: `./data`)
- `STREAMCLIPPERS_HOST` / `STREAMCLIPPERS_PORT` (defaults: `127.0.0.1` / `8430`)
- `STREAMCLIPPERS_HYPE_THRESHOLD` (default: `68.0`)
- `STREAMCLIPPERS_PRE_ROLL_SEC` / `STREAMCLIPPERS_POST_ROLL_SEC` / `STREAMCLIPPERS_COOLDOWN_SEC` (defaults: `25` / `15` / `35`)
- `STREAMCLIPPERS_WHISPER_MODEL` (default: `base`)
- `STREAMCLIPPERS_FFMPEG_PATH` / `STREAMCLIPPERS_FFPROBE_PATH`
- `STREAMCLIPPERS_TWITCH_CLIENT_ID` / `STREAMCLIPPERS_TWITCH_CLIENT_SECRET` *(optional)*

---

## Development Checks

```powershell
uv run pytest -q
uv run ruff check backend tests
uv run bandit -r backend -x tests
cd frontend && npx tsc --noEmit && npm run build
```
