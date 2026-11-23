"""Integration tests for automatic webcam detection and 9:16 FFmpeg rendering."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import cv2
import numpy as np

from backend.config import resolve_ffmpeg_path, resolve_ffprobe_path
from backend.security import CropBox
from backend.services.video_renderer import (
    RenderResult,
    generate_thumbnail,
    probe_video_duration,
    render_vertical_clip,
)
from backend.services.webcam_detector import WebcamDetectionResult, detect_webcam


def _ffprobe_streams(video_path: Path, ffprobe_path: str) -> dict:
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
        check=True,
    )
    return json.loads(proc.stdout)


def _draw_haar_face(frame: np.ndarray, cx: int, cy: int) -> None:
    """Draw a Haar-detectable frontal face + shoulders centered at (cx, cy)."""
    # Shoulders
    cv2.ellipse(frame, (cx, cy + 100), (74, 34), 0, 180, 360, (60, 75, 115), -1)
    # Hair cap
    cv2.ellipse(frame, (cx, cy - 8), (46, 52), 0, 0, 360, (35, 32, 38), -1)
    # Face oval
    cv2.ellipse(frame, (cx, cy + 4), (38, 48), 0, 0, 360, (175, 195, 225), -1)
    # Forehead highlight
    cv2.ellipse(frame, (cx, cy - 18), (24, 12), 0, 0, 360, (195, 212, 238), -1)
    # Eyebrows
    cv2.line(frame, (cx - 24, cy - 14), (cx - 7, cy - 16), (40, 35, 35), 4)
    cv2.line(frame, (cx + 7, cy - 16), (cx + 24, cy - 14), (40, 35, 35), 4)
    # Eye sockets + irises
    cv2.ellipse(frame, (cx - 15, cy - 5), (8, 5), 0, 0, 360, (65, 65, 75), -1)
    cv2.ellipse(frame, (cx + 15, cy - 5), (8, 5), 0, 0, 360, (65, 65, 75), -1)
    cv2.circle(frame, (cx - 15, cy - 5), 3, (25, 25, 30), -1)
    cv2.circle(frame, (cx + 15, cy - 5), 3, (25, 25, 30), -1)
    # Nose bridge & nostrils
    cv2.line(frame, (cx, cy - 4), (cx, cy + 13), (205, 220, 245), 4)
    cv2.circle(frame, (cx - 4, cy + 14), 2, (110, 120, 145), -1)
    cv2.circle(frame, (cx + 4, cy + 14), 2, (110, 120, 145), -1)
    # Mouth
    cv2.ellipse(frame, (cx, cy + 28), (13, 4), 0, 0, 360, (85, 85, 130), -1)


def _draw_simulated_webcam_frame(
    width: int,
    height: int,
    frame_idx: int,
    *,
    center_face_only: bool = False,
) -> np.ndarray:
    """Create a 1280x720 frame with moving gameplay and optional corner webcam or center NPC face."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    # Moving dark-cyan/amber gameplay pattern across the main canvas
    shift = (frame_idx * 17) % width
    frame[:, :] = (32, 38, 48)
    cv2.rectangle(
        frame,
        ((shift + 120) % (width - 180), 220),
        ((shift + 120) % (width - 180) + 160, 540),
        (170, 110, 40),
        -1,
    )
    cv2.circle(frame, (width // 2 + ((frame_idx * 13) % 180) - 90, 420), 48, (70, 180, 220), -1)

    if center_face_only:
        _draw_haar_face(frame, width // 2 + ((frame_idx % 5) - 2) * 4, height // 2)
        return frame

    # Top-left static webcam bezel (x=20..300, y=20..220)
    cam_x0, cam_y0, cam_x1, cam_y1 = 20, 20, 300, 220
    cv2.rectangle(frame, (cam_x0, cam_y0), (cam_x1, cam_y1), (36, 34, 42), -1)
    cv2.rectangle(frame, (cam_x0, cam_y0), (cam_x1, cam_y1), (220, 220, 225), 3)
    _draw_haar_face(frame, 160, 108)
    return frame


def _create_webcam_test_mp4(
    out_path: Path,
    ffmpeg_path: str,
    *,
    center_face_only: bool = False,
) -> Path:
    """Write a 3-second 1280x720 MP4 with simulated corner webcam + 440Hz audio sine tone."""
    avi_path = out_path.with_suffix(".avi")
    writer = cv2.VideoWriter(
        str(avi_path),
        cv2.VideoWriter_fourcc(*"MJPG"),
        30.0,
        (1280, 720),
    )
    if not writer.isOpened():
        raise RuntimeError("Failed to open OpenCV VideoWriter for synthetic test video")
    try:
        for i in range(90):
            writer.write(
                _draw_simulated_webcam_frame(1280, 720, i, center_face_only=center_face_only)
            )
    finally:
        writer.release()

    subprocess.run(
        [
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(avi_path),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-shortest",
            str(out_path),
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=True,
    )
    avi_path.unlink(missing_ok=True)
    return out_path


def _create_ffmpeg_no_cam_mp4(out_path: Path, ffmpeg_path: str, *, with_audio: bool = True) -> Path:
    """Create a 3-second 1280x720 FFmpeg testsrc2 clip without any faces."""
    argv = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1280x720:rate=30:duration=3",
    ]
    if with_audio:
        argv.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=520:sample_rate=44100:duration=3",
            ]
        )
    argv.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if with_audio:
        argv.extend(["-c:a", "aac", "-shortest"])
    else:
        argv.append("-an")
    argv.append(str(out_path))

    subprocess.run(
        argv,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=True,
    )
    return out_path


def _assert_valid_vertical_mp4(mp4_path: Path, ffprobe_path: str, *, expect_audio: bool = True) -> None:
    assert mp4_path.is_file() and mp4_path.stat().st_size > 1024
    info = _ffprobe_streams(mp4_path, ffprobe_path)
    streams = info.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    assert len(video_streams) == 1
    v = video_streams[0]
    assert v["width"] == 1080
    assert v["height"] == 1920
    assert v.get("sample_aspect_ratio") == "1:1"
    assert v.get("codec_name") == "h264"

    if expect_audio:
        assert len(audio_streams) == 1
        a = audio_streams[0]
        assert a.get("codec_name") == "aac"
        assert str(a.get("sample_rate")) == "48000"
    else:
        assert len(audio_streams) == 0


def test_detect_webcam_and_render_both_layouts(tmp_path: Path) -> None:
    ffmpeg_path = resolve_ffmpeg_path()
    ffprobe_path = resolve_ffprobe_path(ffmpeg_path)
    assert ffmpeg_path and Path(ffmpeg_path).is_file()
    assert ffprobe_path and Path(ffprobe_path).is_file()

    cam_video = _create_webcam_test_mp4(tmp_path / "with_cam.mp4", ffmpeg_path)
    no_cam_video = _create_ffmpeg_no_cam_mp4(tmp_path / "no_cam.mp4", ffmpeg_path, with_audio=True)

    # Duration & thumbnail checks
    dur = probe_video_duration(cam_video, ffprobe_path)
    assert 2.5 <= dur <= 3.5

    thumb = generate_thumbnail(cam_video, tmp_path / "thumb.jpg", ffmpeg_path, at_sec=1.0)
    assert thumb.is_file() and thumb.stat().st_size > 500

    # Webcam detection on simulated corner-cam clip
    cam_result = detect_webcam(cam_video, ffmpeg_path)
    assert isinstance(cam_result, WebcamDetectionResult)
    assert cam_result.found is True
    assert cam_result.confidence >= 0.55
    assert cam_result.layout_mode == "split_top_cam"
    assert cam_result.webcam_box is not None
    assert cam_result.webcam_box.x < 0.20
    assert cam_result.webcam_box.y < 0.20

    # Webcam detection on no-cam testsrc2 clip
    no_cam_result = detect_webcam(no_cam_video, ffmpeg_path)
    assert isinstance(no_cam_result, WebcamDetectionResult)
    assert no_cam_result.found is False
    assert no_cam_result.confidence < 0.55
    assert no_cam_result.webcam_box is None
    assert no_cam_result.layout_mode == "blur_no_cam"
    assert no_cam_result.gameplay_box == CropBox(x=0.0, y=0.0, w=1.0, h=1.0)

    # Render split_top_cam
    split_out = tmp_path / "export_split.mp4"
    split_res = render_vertical_clip(
        raw_video_path=cam_video,
        output_path=split_out,
        trim_start_sec=0.2,
        trim_end_sec=2.6,
        layout_mode="split_top_cam",
        split_ratio=0.35,
        webcam_box=cam_result.webcam_box,
        gameplay_box=cam_result.gameplay_box,
        normalize_audio=True,
        ffmpeg_path=ffmpeg_path,
        use_nvenc=False,
    )
    assert isinstance(split_res, RenderResult)
    assert split_res.width == 1080
    assert split_res.height == 1920
    assert split_res.encoder_used == "libx264"
    assert 2.0 <= split_res.duration_sec <= 2.8
    _assert_valid_vertical_mp4(split_out, ffprobe_path, expect_audio=True)

    # Render blur_no_cam
    blur_out = tmp_path / "export_blur.mp4"
    blur_res = render_vertical_clip(
        raw_video_path=no_cam_video,
        output_path=blur_out,
        trim_start_sec=0.0,
        trim_end_sec=2.5,
        layout_mode="blur_no_cam",
        split_ratio=0.35,
        webcam_box=None,
        gameplay_box=no_cam_result.gameplay_box,
        normalize_audio=True,
        ffmpeg_path=ffmpeg_path,
        use_nvenc=False,
    )
    assert isinstance(blur_res, RenderResult)
    assert blur_res.width == 1080
    assert blur_res.height == 1920
    assert blur_res.encoder_used == "libx264"
    _assert_valid_vertical_mp4(blur_out, ffprobe_path, expect_audio=True)


def test_render_silent_video_gracefully(tmp_path: Path) -> None:
    ffmpeg_path = resolve_ffmpeg_path()
    ffprobe_path = resolve_ffprobe_path(ffmpeg_path)
    silent_video = _create_ffmpeg_no_cam_mp4(tmp_path / "silent.mp4", ffmpeg_path, with_audio=False)

    silent_out = tmp_path / "silent_blur.mp4"
    res = render_vertical_clip(
        raw_video_path=silent_video,
        output_path=silent_out,
        trim_start_sec=0.0,
        trim_end_sec=2.0,
        layout_mode="blur_no_cam",
        split_ratio=0.40,
        webcam_box=None,
        gameplay_box=CropBox(x=0.0, y=0.0, w=1.0, h=1.0),
        normalize_audio=True,
        ffmpeg_path=ffmpeg_path,
        use_nvenc=True,
    )
    assert res.width == 1080
    assert res.height == 1920
    assert res.encoder_used in {"h264_nvenc", "libx264"}
    _assert_valid_vertical_mp4(silent_out, ffprobe_path, expect_audio=False)


def test_center_face_rejected_and_nvenc_fallback(tmp_path: Path, monkeypatch) -> None:
    ffmpeg_path = resolve_ffmpeg_path()
    ffprobe_path = resolve_ffprobe_path(ffmpeg_path)

    center_video = _create_webcam_test_mp4(
        tmp_path / "center_npc.mp4",
        ffmpeg_path,
        center_face_only=True,
    )
    center_result = detect_webcam(center_video, ffmpeg_path)
    assert center_result.found is False
    assert center_result.confidence < 0.55
    assert center_result.layout_mode == "blur_no_cam"
    assert center_result.webcam_box is None

    # Verify automatic fallback from h264_nvenc -> libx264 when NVENC fails
    real_run = subprocess.run

    def _mock_nvenc_fail(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "h264_nvenc" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="simulated nvenc error")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("backend.services.video_renderer.subprocess.run", _mock_nvenc_fail)

    fallback_out = tmp_path / "fallback_split.mp4"
    res = render_vertical_clip(
        raw_video_path=center_video,
        output_path=fallback_out,
        trim_start_sec=0.0,
        trim_end_sec=1.5,
        layout_mode="split_top_cam",
        split_ratio=0.10,  # clamped cleanly to 0.25 (even dimensions)
        webcam_box=CropBox(x=0.0, y=0.0, w=0.25, h=0.28),
        gameplay_box=CropBox(x=0.22, y=0.0, w=0.56, h=1.0),
        normalize_audio=False,
        ffmpeg_path=ffmpeg_path,
        use_nvenc=True,
    )
    assert res.encoder_used == "libx264"
    assert res.width == 1080
    assert res.height == 1920
    _assert_valid_vertical_mp4(fallback_out, ffprobe_path, expect_audio=True)
