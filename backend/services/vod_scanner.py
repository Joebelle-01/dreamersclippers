"""Twitch VOD downloader and multi-signal hype peak extractor."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from contextlib import suppress
from inspect import isawaitable
from pathlib import Path
from typing import Any
from uuid import uuid4

import cv2
import httpx
import numpy as np
import yt_dlp
from sqlmodel import Session

from backend.config import Settings, resolve_ffmpeg_path, resolve_ffprobe_path
from backend.models import Channel, ClipItem, ClipRead
from backend.security import (
    safe_resolve_under,
    sanitize_chat_text,
    validate_crop_box,
    validate_twitch_channel,
    validate_twitch_vod_url,
)
from backend.services.hype_detector import HypeDetector
from backend.services.ring_buffer import compute_pcm_dbfs
from backend.services.video_renderer import generate_thumbnail, probe_video_duration
from backend.services.webcam_detector import detect_webcam

logger = logging.getLogger(__name__)

_VOD_ID_RE = re.compile(r"/videos/(\d+)$")
_TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
_AUDIO_1S_BYTES = 16000 * 2  # 16kHz mono s16le -> 32,000 bytes/sec
_MOTION_W = 160
_MOTION_H = 90
_MOTION_FRAME_BYTES = _MOTION_W * _MOTION_H


async def _emit_event(
    broadcast_fn: Callable[..., Any] | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    if broadcast_fn is None:
        return
    try:
        res = broadcast_fn(event_type, payload)
        if isawaitable(res):
            await res
    except TypeError:
        res = broadcast_fn({"type": event_type, "payload": payload})
        if isawaitable(res):
            await res
    except Exception:
        logger.exception("Broadcast failed for event '%s'", event_type)


def extract_non_overlapping_peaks(
    timeline: list[dict[str, float]],
    *,
    threshold: float,
    max_clips: int,
    min_separation_sec: float,
) -> list[dict[str, float]]:
    """Select top-N non-overlapping hype peaks sorted descending by score."""
    if not timeline or max_clips <= 0:
        return []

    candidates: list[dict[str, float]] = []
    for idx, point in enumerate(timeline):
        score = float(point.get("fused", 0.0))
        if score < threshold:
            continue
        prev_score = float(timeline[idx - 1].get("fused", 0.0)) if idx > 0 else -1.0
        next_score = float(timeline[idx + 1].get("fused", 0.0)) if idx + 1 < len(timeline) else -1.0
        if score >= prev_score and score >= next_score:
            candidates.append(point)

    candidates.sort(key=lambda item: float(item.get("fused", 0.0)), reverse=True)
    selected: list[dict[str, float]] = []
    for cand in candidates:
        t_cand = float(cand["t"])
        if any(abs(t_cand - float(picked["t"])) < min_separation_sec for picked in selected):
            continue
        selected.append(cand)
        if len(selected) >= max_clips:
            break

    selected.sort(key=lambda item: float(item["t"]))
    return selected


def _scan_audio_timeline_sync(video_path: Path, ffmpeg_path: str) -> list[tuple[float, float]]:
    """Extract 1-second RMS dBFS timeline from VOD via FFmpeg PCM pipe."""
    proc = subprocess.Popen(
        [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "pipe:1",
        ],
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    samples: list[tuple[float, float]] = []
    sec_idx = 0
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(_AUDIO_1S_BYTES)
            if not chunk:
                break
            if len(chunk) >= 1600:
                samples.append((sec_idx + 0.5, compute_pcm_dbfs(chunk)))
            sec_idx += 1
        if proc.stderr is not None:
            _ = proc.stderr.read().decode("utf-8", errors="replace")
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            with suppress(OSError):
                proc.kill()
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                with suppress(OSError, ValueError):
                    pipe.close()
    return samples


def _scan_motion_timeline_sync(video_path: Path, ffmpeg_path: str) -> list[tuple[float, float]]:
    """Extract 1.5 fps grayscale motion magnitudes from VOD via FFmpeg rawvideo pipe."""
    proc = subprocess.Popen(
        [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-an",
            "-vf",
            f"fps=1.5,scale={_MOTION_W}:{_MOTION_H},format=gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    samples: list[tuple[float, float]] = []
    prev_gray: np.ndarray | None = None
    frame_idx = 0
    try:
        assert proc.stdout is not None
        while True:
            buf = bytearray()
            while len(buf) < _MOTION_FRAME_BYTES:
                part = proc.stdout.read(_MOTION_FRAME_BYTES - len(buf))
                if not part:
                    break
                buf.extend(part)
            if len(buf) < _MOTION_FRAME_BYTES:
                break
            gray = np.frombuffer(bytes(buf), dtype=np.uint8).reshape((_MOTION_H, _MOTION_W))
            blurred = cv2.GaussianBlur(gray, (5, 5), 0)
            ts = frame_idx / 1.5
            if prev_gray is not None:
                diff = cv2.absdiff(blurred, prev_gray)
                score = round(float(np.mean(diff)) * (100.0 / 255.0), 3)
                samples.append((ts, score))
            prev_gray = blurred
            frame_idx += 1
        if proc.stderr is not None:
            _ = proc.stderr.read().decode("utf-8", errors="replace")
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            with suppress(OSError):
                proc.kill()
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                with suppress(OSError, ValueError):
                    pipe.close()
    return samples


async def _fetch_twitch_gql_chat(vod_id: str) -> list[tuple[float, str, str]]:
    """Fetch public Twitch GQL VOD chat replay when an optional client ID is configured."""
    client_id = os.environ.get("STREAMCLIPPERS_TWITCH_GQL_CLIENT_ID", "").strip()
    if not client_id:
        return []

    messages: list[tuple[float, str, str]] = []
    cursor: str | None = None
    headers = {"Client-Id": client_id, "Content-Type": "application/json"}
    query = (
        "query($videoID: ID!, $cursor: Cursor, $offset: Int) { "
        "video(id: $videoID) { comments(after: $cursor, contentOffsetSeconds: $offset) { "
        "edges { cursor node { contentOffsetSeconds commenter { displayName login } "
        "message { fragments { text } } } } pageInfo { hasNextPage } } } }"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for page in range(15):
                variables: dict[str, Any] = {"videoID": vod_id}
                if cursor is not None:
                    variables["cursor"] = cursor
                else:
                    variables["offset"] = 0
                resp = await client.post(
                    _TWITCH_GQL_URL,
                    headers=headers,
                    json={"query": query, "variables": variables},
                )
                if resp.status_code != 200:
                    break
                payload = resp.json()
                comments = ((payload.get("data") or {}).get("video") or {}).get("comments") or {}
                edges = comments.get("edges") or []
                if not edges:
                    break
                for edge in edges:
                    node = edge.get("node") or {}
                    offset = float(node.get("contentOffsetSeconds") or 0.0)
                    commenter = node.get("commenter") or {}
                    author = sanitize_chat_text(
                        str(commenter.get("displayName") or commenter.get("login") or "viewer"),
                        max_len=64,
                    )
                    frags = (node.get("message") or {}).get("fragments") or []
                    text = sanitize_chat_text("".join(str(f.get("text") or "") for f in frags), max_len=240)
                    if author and text:
                        messages.append((offset, author, text))
                    cursor = edge.get("cursor")
                if not (comments.get("pageInfo") or {}).get("hasNextPage") or not cursor:
                    break
                if page >= 14:
                    break
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.info("Skipping Twitch GQL chat replay for VOD %s: %s", vod_id, exc)
        return []

    messages.sort(key=lambda item: item[0])
    return messages


def _download_vod_with_ytdlp_sync(
    vod_url: str,
    work_dir: Path,
    ffmpeg_path: str,
) -> tuple[Path, dict[str, Any]]:
    """Download Twitch VOD using yt-dlp and return local MP4 path + metadata."""
    ydl_opts: dict[str, Any] = {
        "outtmpl": str(work_dir / "%(id)s.%(ext)s"),
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = str(Path(ffmpeg_path).parent)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(vod_url, download=True)
        if not isinstance(info, dict):
            raise RuntimeError("yt-dlp did not return video metadata")
        prepared = Path(ydl.prepare_filename(info))
        candidates = [
            prepared,
            prepared.with_suffix(".mp4"),
            *sorted(work_dir.glob("*.mp4")),
            *sorted(work_dir.glob("*.mkv")),
            *sorted(work_dir.glob("*.ts")),
        ]
        for cand in candidates:
            if cand.is_file() and cand.stat().st_size > 0:
                return cand, info

    raise RuntimeError(f"yt-dlp finished without producing a video file for {vod_url}")


def _cut_vod_segment_sync(
    vod_path: Path,
    out_path: Path,
    start_sec: float,
    duration_sec: float,
    ffmpeg_path: str,
) -> Path:
    """Cut a segment from the downloaded VOD cleanly using FFmpeg."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base_args = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, start_sec):.3f}",
        "-i",
        str(vod_path),
        "-t",
        f"{max(1.0, duration_sec):.3f}",
    ]
    copy_proc = subprocess.run(
        [
            *base_args,
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            str(out_path),
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if copy_proc.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
        return out_path

    reencode_proc = subprocess.run(
        [
            *base_args,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(out_path),
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    if reencode_proc.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
        err = (reencode_proc.stderr or copy_proc.stderr or "").strip()
        raise RuntimeError(f"Failed to cut VOD segment: {err}")
    return out_path


def _resolve_channel_context(
    session_factory: Callable[[], Session],
    channel_id: int | None,
    info: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "channel_id": int(channel_id or 0),
        "channel_login": "twitch_vod",
        "pre_roll_sec": float(settings.pre_roll_sec),
        "post_roll_sec": float(settings.post_roll_sec),
        "layout_mode": "split_top_cam",
        "split_ratio": float(settings.default_split_ratio),
        "preset_webcam_json": None,
        "preset_gameplay_json": None,
    }
    if channel_id is not None:
        with session_factory() as session:
            ch = session.get(Channel, channel_id)
            if ch is not None:
                ctx["channel_id"] = int(ch.id or 0)
                ctx["channel_login"] = str(ch.channel_login)
                ctx["pre_roll_sec"] = float(ch.pre_roll_sec)
                ctx["post_roll_sec"] = float(ch.post_roll_sec)
                ctx["layout_mode"] = str(ch.preset_layout_mode)
                ctx["split_ratio"] = float(ch.preset_split_ratio)
                ctx["preset_webcam_json"] = ch.preset_webcam_json
                ctx["preset_gameplay_json"] = ch.preset_gameplay_json
                return ctx

    raw_uploader = str(info.get("uploader_id") or info.get("uploader") or info.get("channel") or "")
    try:
        ctx["channel_login"] = validate_twitch_channel(raw_uploader)
    except ValueError:
        cleaned = re.sub(r"[^a-zA-Z0-9_]", "", raw_uploader).lower()[:25]
        if len(cleaned) >= 3:
            ctx["channel_login"] = cleaned
    return ctx


async def scan_twitch_vod(
    vod_url: str,
    channel_id: int | None,
    max_clips: int,
    settings: Settings,
    session_factory: Callable[[], Session],
    broadcast_fn: Callable[..., Any] | None = None,
    hype_threshold: float | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Download a Twitch VOD, detect top hype peaks, and save pending review clips."""
    safe_url = validate_twitch_vod_url(vod_url)
    id_match = _VOD_ID_RE.search(safe_url)
    assert id_match is not None
    vod_id = id_match.group(1)
    active_job_id = job_id or str(uuid4())
    effective_threshold = float(hype_threshold if hype_threshold is not None else settings.hype_threshold)
    capped_clips = max(1, min(20, int(max_clips)))

    ffmpeg_path = settings.ffmpeg_path or resolve_ffmpeg_path()
    ffprobe_path = settings.ffprobe_path or resolve_ffprobe_path(ffmpeg_path)
    if not ffmpeg_path or not ffprobe_path:
        raise RuntimeError("FFmpeg and ffprobe executables must be available for VOD scanning")

    settings.ensure_data_dirs()
    work_dir = safe_resolve_under(settings.buffers_dir, f"vod_{vod_id}_{uuid4().hex[:8]}")
    work_dir.mkdir(parents=True, exist_ok=True)

    await _emit_event(
        broadcast_fn,
        "vod_progress",
        {
            "job_id": active_job_id,
            "vod_url": safe_url,
            "stage": "downloading",
            "progress": 10,
            "status": "running",
            "message": f"Downloading Twitch VOD {vod_id} with yt-dlp",
        },
    )

    try:
        vod_path, info = await asyncio.to_thread(
            _download_vod_with_ytdlp_sync,
            safe_url,
            work_dir,
            ffmpeg_path,
        )
        duration_sec = await asyncio.to_thread(probe_video_duration, vod_path, ffprobe_path)
        if duration_sec <= 0.0:
            duration_sec = float(info.get("duration") or 0.0)
        if duration_sec <= 0.0:
            raise RuntimeError("Downloaded VOD has zero duration")

        channel_ctx = _resolve_channel_context(session_factory, channel_id, info, settings)
        channel_login = channel_ctx["channel_login"]

        await _emit_event(
            broadcast_fn,
            "vod_progress",
            {
                "job_id": active_job_id,
                "vod_url": safe_url,
                "stage": "analyzing",
                "progress": 40,
                "status": "running",
                "message": "Scanning audio dBFS, visual motion, and chat replay",
            },
        )

        audio_task = asyncio.to_thread(_scan_audio_timeline_sync, vod_path, ffmpeg_path)
        motion_task = asyncio.to_thread(_scan_motion_timeline_sync, vod_path, ffmpeg_path)
        chat_task = _fetch_twitch_gql_chat(vod_id)
        audio_samples, motion_samples, chat_samples = await asyncio.gather(
            audio_task,
            motion_task,
            chat_task,
        )

        detector = HypeDetector(
            channel_login=channel_login,
            threshold=effective_threshold,
            cooldown_sec=0.0,
        )
        has_chat = len(chat_samples) >= 5
        a_idx = 0
        m_idx = 0
        c_idx = 0
        timeline: list[dict[str, float]] = []

        total_secs = max(1, int(math.ceil(duration_sec)))
        for sec in range(1, total_secs + 1):
            t_now = float(sec)
            while a_idx < len(audio_samples) and audio_samples[a_idx][0] <= t_now:
                detector.ingest_audio_rms(audio_samples[a_idx][1], ts=audio_samples[a_idx][0])
                a_idx += 1
            while m_idx < len(motion_samples) and motion_samples[m_idx][0] <= t_now:
                detector.ingest_motion(motion_samples[m_idx][1], ts=motion_samples[m_idx][0])
                m_idx += 1
            while c_idx < len(chat_samples) and chat_samples[c_idx][0] <= t_now:
                c_ts, c_author, c_text = chat_samples[c_idx]
                detector.ingest_chat(c_author, c_text, ts=c_ts)
                c_idx += 1

            ev = detector.evaluate(now=t_now)
            score = (
                ev.fused_score
                if has_chat
                else round(min(100.0, 0.68 * ev.audio_score + 0.32 * ev.motion_score), 2)
            )
            timeline.append(
                {
                    "t": t_now,
                    "chat": ev.chat_score,
                    "audio": ev.audio_score,
                    "motion": ev.motion_score,
                    "fused": score,
                }
            )

        pre_roll = float(channel_ctx["pre_roll_sec"])
        post_roll = float(channel_ctx["post_roll_sec"])
        min_sep = min(max(6.0, duration_sec * 0.2), pre_roll + post_roll)
        peaks = extract_non_overlapping_peaks(
            timeline,
            threshold=effective_threshold,
            max_clips=capped_clips,
            min_separation_sec=min_sep,
        )

        await _emit_event(
            broadcast_fn,
            "vod_progress",
            {
                "job_id": active_job_id,
                "vod_url": safe_url,
                "stage": "extracting",
                "progress": 70,
                "status": "running",
                "clips_found": len(peaks),
                "message": f"Extracting {len(peaks)} hype clip(s)",
            },
        )

        created_ids: list[str] = []
        vod_title = sanitize_chat_text(str(info.get("title") or f"Twitch VOD {vod_id}"), max_len=80)
        for idx, peak in enumerate(peaks, start=1):
            t_peak = float(peak["t"])
            clip_start = max(0.0, t_peak - pre_roll)
            clip_end = min(duration_sec, t_peak + post_roll)
            if clip_end - clip_start < 2.0:
                clip_start = max(0.0, min(duration_sec - 2.0, clip_start))
                clip_end = min(duration_sec, clip_start + 2.0)

            clip_id = str(uuid4())
            raw_clip_path = safe_resolve_under(settings.clips_dir, f"{clip_id}.mp4")
            await asyncio.to_thread(
                _cut_vod_segment_sync,
                vod_path,
                raw_clip_path,
                clip_start,
                clip_end - clip_start,
                ffmpeg_path,
            )
            clip_dur = await asyncio.to_thread(probe_video_duration, raw_clip_path, ffprobe_path)
            if clip_dur <= 0.0:
                clip_dur = max(0.5, clip_end - clip_start)

            thumb_path = safe_resolve_under(settings.thumbs_dir, f"{clip_id}.jpg")
            await asyncio.to_thread(
                generate_thumbnail,
                raw_clip_path,
                thumb_path,
                ffmpeg_path,
                min(1.0, clip_dur * 0.25),
            )

            preset_cam_json = channel_ctx["preset_webcam_json"]
            preset_game_json = channel_ctx["preset_gameplay_json"]
            webcam_box_json: str | None = None
            gameplay_box_json: str | None = None
            used_preset = False
            if preset_cam_json:
                try:
                    validated_cam = validate_crop_box(json.loads(preset_cam_json))
                    webcam_box_json = json.dumps(validated_cam.model_dump())
                    if preset_game_json:
                        validated_game = validate_crop_box(json.loads(preset_game_json))
                        gameplay_box_json = json.dumps(validated_game.model_dump())
                    else:
                        gameplay_box_json = json.dumps({"x": 0.22, "y": 0.0, "w": 0.56, "h": 1.0})
                    layout_mode = str(channel_ctx["layout_mode"])
                    webcam_conf = 1.0
                    used_preset = True
                except (ValueError, TypeError):
                    used_preset = False
            if not used_preset:
                detection = await asyncio.to_thread(detect_webcam, raw_clip_path, ffmpeg_path)
                layout_mode = str(detection.layout_mode)
                webcam_conf = float(detection.confidence)
                webcam_box_json = (
                    json.dumps(detection.webcam_box.model_dump())
                    if detection.webcam_box is not None
                    else None
                )
                gameplay_box_json = json.dumps(detection.gameplay_box.model_dump())

            local_timeline = [
                {
                    "t": round(float(pt["t"]) - clip_start, 2),
                    "chat": pt["chat"],
                    "audio": pt["audio"],
                    "motion": pt["motion"],
                    "fused": pt["fused"],
                }
                for pt in timeline
                if clip_start <= float(pt["t"]) <= clip_end
            ]
            local_chat = [
                {"author": author, "text": text, "ts": round(ts - clip_start, 2)}
                for ts, author, text in chat_samples
                if (t_peak - 8.0) <= ts <= (t_peak + 4.0)
            ][:15]

            reason_parts = []
            if float(peak["chat"]) >= 40.0:
                reason_parts.append("Chat")
            if float(peak["audio"]) >= 40.0:
                reason_parts.append("Audio")
            if float(peak["motion"]) >= 40.0:
                reason_parts.append("Motion")
            reason_label = " + ".join(reason_parts) if reason_parts else "Audio + Motion"
            mode_suffix = "" if has_chat else " (VOD audio+motion)"

            clip_row = ClipItem(
                id=clip_id,
                channel_id=int(channel_ctx["channel_id"]),
                channel_login=channel_login,
                source_type="vod",
                source_ref=safe_url,
                title=f"{vod_title} Peak #{idx}",
                status="pending_review",
                hype_score=float(peak["fused"]),
                trigger_reason=f"{reason_label} spike{mode_suffix}",
                trigger_chat_samples_json=json.dumps(local_chat),
                hype_timeline_json=json.dumps(local_timeline),
                raw_video_path=str(raw_clip_path),
                thumbnail_path=str(thumb_path),
                duration_sec=float(clip_dur),
                trim_start_sec=0.0,
                trim_end_sec=float(clip_dur),
                layout_mode=layout_mode,
                split_ratio=float(channel_ctx["split_ratio"]),
                webcam_box_json=webcam_box_json,
                gameplay_box_json=gameplay_box_json,
                webcam_confidence=webcam_conf,
                normalize_audio=True,
            )

            with session_factory() as session:
                session.add(clip_row)
                session.commit()
                session.refresh(clip_row)
                clip_payload = ClipRead.from_orm_row(clip_row).model_dump(mode="json")

            created_ids.append(clip_id)
            await _emit_event(broadcast_fn, "clip_created", clip_payload)

        await _emit_event(
            broadcast_fn,
            "vod_progress",
            {
                "job_id": active_job_id,
                "vod_url": safe_url,
                "stage": "complete",
                "progress": 100,
                "status": "completed",
                "clips_found": len(created_ids),
                "message": f"VOD scan complete: {len(created_ids)} clip(s) queued",
            },
        )

        return {
            "job_id": active_job_id,
            "vod_url": safe_url,
            "vod_id": vod_id,
            "used_chat_replay": has_chat,
            "clips_created": created_ids,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
