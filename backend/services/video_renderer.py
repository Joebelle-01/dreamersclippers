"""FFmpeg vertical 9:16 video rendering, probing, and thumbnail generation."""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from backend.config import resolve_ffmpeg_path, resolve_ffprobe_path
from backend.security import CropBox, validate_crop_box
from backend.services.caption_service import build_title_overlay_ass, escape_ffmpeg_filter_path

logger = logging.getLogger(__name__)

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
_DEFAULT_WEBCAM_BOX = CropBox(x=0.0, y=0.0, w=0.30, h=0.30)
_DEFAULT_GAMEPLAY_SPLIT_BOX = CropBox(x=0.22, y=0.0, w=0.56, h=1.0)
_DEFAULT_GAMEPLAY_FULL_BOX = CropBox(x=0.0, y=0.0, w=1.0, h=1.0)


class RenderResult(BaseModel):
    """Metadata describing a finished vertical 9:16 MP4 render."""

    output_path: str
    encoder_used: str
    width: int
    height: int
    duration_sec: float


@dataclass(frozen=True)
class _StreamInfo:
    width: int
    height: int
    fps: float
    has_audio: bool
    duration_sec: float


def _parse_fps(rate_str: str | None) -> float:
    if not rate_str or rate_str in ("0/0", "N/A"):
        return 0.0
    if "/" in rate_str:
        num_s, den_s = rate_str.split("/", 1)
        try:
            num = float(num_s)
            den = float(den_s)
            if den > 0.0:
                val = num / den
                return val if math.isfinite(val) and val > 0.0 else 0.0
        except ValueError:
            return 0.0
        return 0.0
    try:
        val = float(rate_str)
        return val if math.isfinite(val) and val > 0.0 else 0.0
    except ValueError:
        return 0.0


def _run_ffprobe_json(video_path: Path, ffprobe_path: str) -> dict:
    proc = subprocess.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(video_path),
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        data = json.loads(proc.stdout)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _probe_streams(video_path: Path, ffprobe_path: str) -> _StreamInfo:
    resolved_probe = ffprobe_path or resolve_ffprobe_path()
    if not resolved_probe:
        return _StreamInfo(width=0, height=0, fps=30.0, has_audio=False, duration_sec=0.0)

    info = _run_ffprobe_json(video_path, resolved_probe)
    streams = info.get("streams") or []
    fmt = info.get("format") or {}

    width = 0
    height = 0
    fps = 0.0
    has_audio = False
    stream_duration = 0.0

    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec_type = stream.get("codec_type")
        if codec_type == "video" and width == 0:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            fps = _parse_fps(stream.get("avg_frame_rate")) or _parse_fps(stream.get("r_frame_rate"))
            try:
                sd = float(stream.get("duration") or 0.0)
                if math.isfinite(sd) and sd > stream_duration:
                    stream_duration = sd
            except (TypeError, ValueError):
                pass
        elif codec_type == "audio":
            has_audio = True

    fmt_duration = 0.0
    try:
        fd = float(fmt.get("duration") or 0.0)
        if math.isfinite(fd) and fd > 0.0:
            fmt_duration = fd
    except (TypeError, ValueError):
        fmt_duration = 0.0

    duration = fmt_duration if fmt_duration > 0.0 else stream_duration
    return _StreamInfo(
        width=width,
        height=height,
        fps=fps if 1.0 <= fps <= 240.0 else 30.0,
        has_audio=has_audio,
        duration_sec=duration,
    )


def probe_video_duration(video_path: Path, ffprobe_path: str) -> float:
    """Probe media duration in seconds using ffprobe."""
    target = Path(video_path)
    if not target.is_file():
        raise FileNotFoundError(f"Video file not found: {target}")

    resolved_probe = ffprobe_path or resolve_ffprobe_path()
    if not resolved_probe:
        raise RuntimeError("ffprobe executable path could not be resolved")

    proc = subprocess.run(
        [
            resolved_probe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(target),
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            try:
                val = float(line.strip())
                if math.isfinite(val) and val > 0.0:
                    return round(val, 4)
            except ValueError:
                continue

    stream_info = _probe_streams(target, resolved_probe)
    return round(stream_info.duration_sec, 4)


def generate_thumbnail(
    video_path: Path,
    thumb_path: Path,
    ffmpeg_path: str,
    at_sec: float = 1.0,
) -> Path:
    """Extract a single 16:9 preview thumbnail frame using FFmpeg."""
    src = Path(video_path)
    dst = Path(thumb_path)
    if not src.is_file():
        raise FileNotFoundError(f"Source video not found for thumbnail: {src}")

    resolved_ffmpeg = ffmpeg_path or resolve_ffmpeg_path()
    if not resolved_ffmpeg:
        raise RuntimeError("ffmpeg executable path could not be resolved")

    dst.parent.mkdir(parents=True, exist_ok=True)

    ffprobe_path = resolve_ffprobe_path(resolved_ffmpeg)
    duration = _probe_streams(src, ffprobe_path).duration_sec if ffprobe_path else 0.0
    seek_sec = max(0.0, float(at_sec))
    if duration > 0.0 and seek_sec >= duration:
        seek_sec = max(0.0, duration * 0.25)

    attempts = [seek_sec] if seek_sec <= 0.01 else [seek_sec, 0.0]
    last_err = ""
    for candidate_sec in attempts:
        proc = subprocess.run(
            [
                resolved_ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{candidate_sec:.3f}",
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-vf",
                "scale=640:-2",
                "-q:v",
                "3",
                str(dst),
            ],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if proc.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
            return dst
        last_err = proc.stderr.strip()

    raise RuntimeError(f"Failed to generate thumbnail for '{src}': {last_err}")


def _build_crop_filter(box: CropBox, src_w: int, src_h: int) -> str:
    if src_w >= 4 and src_h >= 4:
        cw = max(2, min(src_w, (int(round(src_w * box.w)) // 2) * 2))
        ch = max(2, min(src_h, (int(round(src_h * box.h)) // 2) * 2))
        cx = max(0, min(src_w - cw, int(round(src_w * box.x))))
        cy = max(0, min(src_h - ch, int(round(src_h * box.y))))
        return f"crop={cw}:{ch}:{cx}:{cy}"
    return (
        f"crop=w='max(2,trunc(iw*{box.w:.6f}/2)*2)':"
        f"h='max(2,trunc(ih*{box.h:.6f}/2)*2)':"
        f"x='min(iw-ow,trunc(iw*{box.x:.6f}))':"
        f"y='min(ih-oh,trunc(ih*{box.y:.6f}))'"
    )


def _format_target_fps(src_fps: float) -> str:
    capped = min(src_fps, 60.0) if src_fps >= 1.0 else 30.0
    rounded_int = round(capped)
    if abs(capped - rounded_int) < 0.02:
        return str(int(rounded_int))
    return f"{capped:.3f}".rstrip("0").rstrip(".")


def _build_filter_complex(
    *,
    layout_mode: str,
    split_ratio: float,
    webcam_box: CropBox,
    gameplay_box: CropBox,
    src_w: int,
    src_h: int,
    fps_str: str,
    ass_subtitle_path: Path | None = None,
) -> str:
    ass_step = ""
    if ass_subtitle_path is not None and Path(ass_subtitle_path).is_file():
        escaped = escape_ffmpeg_filter_path(ass_subtitle_path)
        ass_step = f",ass='{escaped}'"

    if layout_mode == "split_top_cam":
        clamped_ratio = max(0.25, min(0.55, float(split_ratio)))
        h_cam = int(round(TARGET_HEIGHT * clamped_ratio / 2.0) * 2)
        h_game = TARGET_HEIGHT - h_cam
        cam_crop = _build_crop_filter(webcam_box, src_w, src_h)
        game_crop = _build_crop_filter(gameplay_box, src_w, src_h)
        return (
            f"[0:v]split=2[v_cam_in][v_game_in];"
            f"[v_cam_in]{cam_crop},"
            f"scale={TARGET_WIDTH}:{h_cam}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={TARGET_WIDTH}:{h_cam}:(iw-{TARGET_WIDTH})/2:(ih-{h_cam})/2,setsar=1[top];"
            f"[v_game_in]{game_crop},"
            f"scale={TARGET_WIDTH}:{h_game}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={TARGET_WIDTH}:{h_game}:(iw-{TARGET_WIDTH})/2:(ih-{h_game})/2,setsar=1[bot];"
            f"[top][bot]vstack=inputs=2,"
            f"drawbox=x=0:y={h_cam - 1}:w={TARGET_WIDTH}:h=2:color=0x111111@0.95:t=fill,"
            f"fps={fps_str},setsar=1{ass_step},format=yuv420p[vout]"
        )

    if layout_mode == "blur_no_cam":
        fg_crop = _build_crop_filter(gameplay_box, src_w, src_h)
        return (
            f"[0:v]split=2[bg_src][fg_src];"
            f"[bg_src]scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=increase:flags=bicubic,"
            f"crop={TARGET_WIDTH}:{TARGET_HEIGHT}:(iw-{TARGET_WIDTH})/2:(ih-{TARGET_HEIGHT})/2,"
            f"gblur=sigma=24,eq=brightness=-0.08,setsar=1[bg];"
            f"[fg_src]{fg_crop},"
            f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={fps_str},setsar=1{ass_step},format=yuv420p[vout]"
        )

    raise ValueError(f"Unsupported layout_mode '{layout_mode}'; expected 'split_top_cam' or 'blur_no_cam'")


def _build_render_argv(
    *,
    ffmpeg_path: str,
    raw_video_path: Path,
    temp_output_path: Path,
    trim_start_sec: float,
    clip_duration_sec: float,
    filter_complex: str,
    has_audio: bool,
    normalize_audio: bool,
    encoder: Literal["h264_nvenc", "libx264"],
) -> list[str]:
    argv = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{trim_start_sec:.3f}",
        "-i",
        str(raw_video_path),
        "-t",
        f"{clip_duration_sec:.3f}",
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
    ]

    if has_audio:
        argv.extend(["-map", "0:a:0"])
        if normalize_audio:
            argv.extend(["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"])
        argv.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"])
    else:
        argv.append("-an")

    if encoder == "h264_nvenc":
        argv.extend(["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "20", "-rc", "vbr"])
    else:
        argv.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "19"])

    argv.extend(["-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temp_output_path)])
    return argv


def render_vertical_clip(
    raw_video_path: Path,
    output_path: Path,
    trim_start_sec: float,
    trim_end_sec: float,
    layout_mode: str,
    split_ratio: float,
    webcam_box: CropBox | None,
    gameplay_box: CropBox | None,
    normalize_audio: bool,
    ffmpeg_path: str,
    use_nvenc: bool,
    ass_subtitle_path: Path | None = None,
) -> RenderResult:
    """Render a 1080x1920 vertical 9:16 MP4 clip with optional burned-in ASS subtitles."""
    src_path = Path(raw_video_path)
    dst_path = Path(output_path)
    if not src_path.is_file():
        raise FileNotFoundError(f"Raw clip video not found: {src_path}")

    resolved_ffmpeg = ffmpeg_path or resolve_ffmpeg_path()
    if not resolved_ffmpeg:
        raise RuntimeError("ffmpeg executable path could not be resolved")
    ffprobe_path = resolve_ffprobe_path(resolved_ffmpeg)

    stream_info = _probe_streams(src_path, ffprobe_path)
    start_sec = max(0.0, float(trim_start_sec))
    end_sec = float(trim_end_sec)
    if end_sec <= start_sec:
        if stream_info.duration_sec > start_sec:
            end_sec = stream_info.duration_sec
        else:
            raise ValueError(
                f"Invalid trim window: start={start_sec:.3f}s, end={end_sec:.3f}s"
            )
    clip_duration_sec = max(0.1, end_sec - start_sec)

    norm_webcam = (
        validate_crop_box(webcam_box) if webcam_box is not None else _DEFAULT_WEBCAM_BOX
    )
    if gameplay_box is not None:
        norm_gameplay = validate_crop_box(gameplay_box)
    elif layout_mode == "split_top_cam":
        norm_gameplay = _DEFAULT_GAMEPLAY_SPLIT_BOX
    else:
        norm_gameplay = _DEFAULT_GAMEPLAY_FULL_BOX

    fps_str = _format_target_fps(stream_info.fps)
    filter_complex = _build_filter_complex(
        layout_mode=layout_mode,
        split_ratio=split_ratio,
        webcam_box=norm_webcam,
        gameplay_box=norm_gameplay,
        src_w=stream_info.width,
        src_h=stream_info.height,
        fps_str=fps_str,
        ass_subtitle_path=ass_subtitle_path,
    )

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    temp_out = dst_path.with_name(f".{dst_path.stem}.rendering{dst_path.suffix or '.mp4'}")

    encoders: list[Literal["h264_nvenc", "libx264"]] = (
        ["h264_nvenc", "libx264"] if use_nvenc else ["libx264"]
    )
    encoder_used = ""
    last_stderr = ""

    try:
        for encoder in encoders:
            if temp_out.exists():
                temp_out.unlink()
            argv = _build_render_argv(
                ffmpeg_path=resolved_ffmpeg,
                raw_video_path=src_path,
                temp_output_path=temp_out,
                trim_start_sec=start_sec,
                clip_duration_sec=clip_duration_sec,
                filter_complex=filter_complex,
                has_audio=stream_info.has_audio,
                normalize_audio=normalize_audio,
                encoder=encoder,
            )
            proc = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                check=False,
            )
            if proc.returncode == 0 and temp_out.is_file() and temp_out.stat().st_size > 0:
                encoder_used = encoder
                break
            last_stderr = proc.stderr.strip()
            logger.warning("FFmpeg render attempt with %s failed: %s", encoder, last_stderr)

        if not encoder_used:
            raise RuntimeError(f"FFmpeg vertical clip render failed: {last_stderr}")

        temp_out.replace(dst_path)
    finally:
        if temp_out.exists():
            try:
                temp_out.unlink()
            except OSError:
                pass

    rendered_duration = (
        probe_video_duration(dst_path, ffprobe_path) if ffprobe_path else round(clip_duration_sec, 4)
    )
    return RenderResult(
        output_path=str(dst_path),
        encoder_used=encoder_used,
        width=TARGET_WIDTH,
        height=TARGET_HEIGHT,
        duration_sec=rendered_duration if rendered_duration > 0.0 else round(clip_duration_sec, 4),
    )


def render_vertical_thumbnail(
    raw_video_path: Path,
    output_path: Path,
    at_sec: float,
    layout_mode: str,
    split_ratio: float,
    webcam_box: CropBox | None,
    gameplay_box: CropBox | None,
    ffmpeg_path: str,
    title_overlay_text: str | None = None,
) -> Path:
    """Render a 1080x1920 vertical 9:16 cover JPEG frame with optional title overlay."""
    src_path = Path(raw_video_path)
    dst_path = Path(output_path)
    if not src_path.is_file():
        raise FileNotFoundError(f"Raw clip video not found for thumbnail: {src_path}")

    resolved_ffmpeg = ffmpeg_path or resolve_ffmpeg_path()
    if not resolved_ffmpeg:
        raise RuntimeError("ffmpeg executable path could not be resolved")
    ffprobe_path = resolve_ffprobe_path(resolved_ffmpeg)

    stream_info = _probe_streams(src_path, ffprobe_path)
    seek_sec = max(0.0, float(at_sec))
    if stream_info.duration_sec > 0.0 and seek_sec >= stream_info.duration_sec:
        seek_sec = max(0.0, stream_info.duration_sec - 0.1)

    norm_webcam = (
        validate_crop_box(webcam_box) if webcam_box is not None else _DEFAULT_WEBCAM_BOX
    )
    if gameplay_box is not None:
        norm_gameplay = validate_crop_box(gameplay_box)
    elif layout_mode == "split_top_cam":
        norm_gameplay = _DEFAULT_GAMEPLAY_SPLIT_BOX
    else:
        norm_gameplay = _DEFAULT_GAMEPLAY_FULL_BOX

    title_ass_path: Path | None = None
    try:
        if title_overlay_text and title_overlay_text.strip():
            handle = tempfile.NamedTemporaryFile(
                suffix=".ass",
                delete=False,
                mode="w",
                encoding="utf-8",
            )
            title_ass_path = Path(handle.name)
            handle.write(build_title_overlay_ass(title_overlay_text))
            handle.close()

        fps_str = _format_target_fps(stream_info.fps)
        filter_complex = _build_filter_complex(
            layout_mode=layout_mode,
            split_ratio=split_ratio,
            webcam_box=norm_webcam,
            gameplay_box=norm_gameplay,
            src_w=stream_info.width,
            src_h=stream_info.height,
            fps_str=fps_str,
            ass_subtitle_path=title_ass_path,
        )

        dst_path.parent.mkdir(parents=True, exist_ok=True)
        attempts = [seek_sec] if seek_sec <= 0.01 else [seek_sec, 0.0]
        last_err = ""
        for candidate_sec in attempts:
            if dst_path.exists():
                dst_path.unlink(missing_ok=True)
            proc = subprocess.run(
                [
                    resolved_ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{candidate_sec:.3f}",
                    "-i",
                    str(src_path),
                    "-filter_complex",
                    filter_complex,
                    "-map",
                    "[vout]",
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(dst_path),
                ],
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            if proc.returncode == 0 and dst_path.is_file() and dst_path.stat().st_size > 0:
                return dst_path
            last_err = proc.stderr.strip()

        raise RuntimeError(f"FFmpeg vertical thumbnail render failed: {last_err}")
    finally:
        if title_ass_path is not None and title_ass_path.exists():
            try:
                os.unlink(title_ass_path)
            except OSError:
                pass
