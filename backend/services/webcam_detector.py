"""Automatic streamer webcam detection and bounding-box refinement."""

from __future__ import annotations

import logging
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from pydantic import BaseModel, Field

from backend.config import resolve_ffmpeg_path, resolve_ffprobe_path
from backend.security import CropBox

logger = logging.getLogger(__name__)

_HAAR_FILENAME = "haarcascade_frontalface_default.xml"
_HAAR_PROFILE_FILENAME = "haarcascade_profileface.xml"
_CONFIDENCE_THRESHOLD = 0.55
_DEFAULT_GAMEPLAY_WITH_CAM = CropBox(x=0.22, y=0.0, w=0.56, h=1.0)
_DEFAULT_GAMEPLAY_NO_CAM = CropBox(x=0.0, y=0.0, w=1.0, h=1.0)


class WebcamDetectionResult(BaseModel):
    """Result of automatic webcam framing detection on a 16:9 clip."""

    found: bool
    confidence: float = Field(ge=0.0, le=1.0)
    webcam_box: CropBox | None = None
    gameplay_box: CropBox
    layout_mode: Literal["split_top_cam", "blur_no_cam"]


@dataclass
class _FaceDetection:
    frame_idx: int
    cx: float
    cy: float
    w: float
    h: float


@dataclass
class _FaceCluster:
    detections: list[_FaceDetection] = field(default_factory=list)

    @property
    def frame_hits(self) -> int:
        return len({det.frame_idx for det in self.detections})

    @property
    def median_cx(self) -> float:
        return float(np.median([det.cx for det in self.detections]))

    @property
    def median_cy(self) -> float:
        return float(np.median([det.cy for det in self.detections]))

    @property
    def median_w(self) -> float:
        return float(np.median([det.w for det in self.detections]))

    @property
    def median_h(self) -> float:
        return float(np.median([det.h for det in self.detections]))

    def distance_to(self, cx: float, cy: float) -> float:
        return math.hypot(self.median_cx - cx, self.median_cy - cy)


def _load_face_cascade() -> cv2.CascadeClassifier:
    cascade_path = Path(cv2.data.haarcascades) / _HAAR_FILENAME
    cascade = cv2.CascadeClassifier(str(cascade_path))
    if cascade.empty():
        raise RuntimeError(f"Failed to load OpenCV Haar cascade from '{cascade_path}'")
    return cascade


def _load_profile_cascade() -> cv2.CascadeClassifier | None:
    cascade_path = Path(cv2.data.haarcascades) / _HAAR_PROFILE_FILENAME
    if not cascade_path.is_file():
        return None
    cascade = cv2.CascadeClassifier(str(cascade_path))
    return None if cascade.empty() else cascade


def _probe_duration_sec(video_path: Path, ffmpeg_path: str) -> float:
    ffprobe_path = resolve_ffprobe_path(ffmpeg_path)
    if not ffprobe_path:
        return 0.0
    try:
        proc = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0.0
    if proc.returncode != 0:
        return 0.0
    for line in proc.stdout.splitlines():
        try:
            value = float(line.strip())
            if math.isfinite(value) and value > 0.0:
                return value
        except ValueError:
            continue
    return 0.0


def _sample_frames_opencv(video_path: Path, count: int) -> list[np.ndarray]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        cap.release()
        return []

    frames: list[np.ndarray] = []
    try:
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames >= count:
            start_idx = max(0, int(round(total_frames * 0.1)))
            end_idx = max(start_idx, int(round(total_frames * 0.9)) - 1)
            indices = np.linspace(start_idx, end_idx, count, dtype=int).tolist()
            for idx in indices:
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
                ok, frame = cap.read()
                if ok and frame is not None and frame.size > 0:
                    frames.append(frame)
        elif total_frames > 0:
            all_frames: list[np.ndarray] = []
            while len(all_frames) < 300:
                ok, frame = cap.read()
                if not ok or frame is None or frame.size == 0:
                    break
                all_frames.append(frame)
            if all_frames:
                picks = np.linspace(0, len(all_frames) - 1, min(count, len(all_frames)), dtype=int)
                frames = [all_frames[int(i)] for i in picks]
    finally:
        cap.release()
    return frames


def _sample_frames_ffmpeg(video_path: Path, ffmpeg_path: str, count: int) -> list[np.ndarray]:
    resolved_ffmpeg = ffmpeg_path or resolve_ffmpeg_path()
    if not resolved_ffmpeg:
        return []

    duration = _probe_duration_sec(video_path, resolved_ffmpeg)
    if duration <= 0.2:
        timestamps = [0.0]
    else:
        timestamps = np.linspace(duration * 0.1, duration * 0.9, count).tolist()

    frames: list[np.ndarray] = []
    for ts in timestamps:
        try:
            proc = subprocess.run(
                [
                    resolved_ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{max(0.0, ts):.3f}",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "-",
                ],
                shell=False,
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0 or not proc.stdout:
            continue
        arr = np.frombuffer(proc.stdout, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is not None and frame.size > 0:
            frames.append(frame)
    return frames


def _sample_frames(video_path: Path, ffmpeg_path: str, count: int = 5) -> list[np.ndarray]:
    frames = _sample_frames_opencv(video_path, count)
    if len(frames) >= min(3, count):
        return frames
    ffmpeg_frames = _sample_frames_ffmpeg(video_path, ffmpeg_path, count)
    return ffmpeg_frames if len(ffmpeg_frames) > len(frames) else frames


def _webcam_zone_weight(cx: float, cy: float) -> float:
    """Strongly favor 4 corners and side edges over center gameplay."""
    horiz_corner = cx <= 0.34 or cx >= 0.66
    vert_corner = cy <= 0.38 or cy >= 0.62
    if horiz_corner and vert_corner:
        return 1.0
    if cx <= 0.28 or cx >= 0.72:
        return 0.88
    if cy <= 0.24 or cy >= 0.76:
        return 0.62
    if 0.32 <= cx <= 0.68 and 0.24 <= cy <= 0.76:
        return 0.15
    return 0.45


def _detect_faces_per_frame(
    grays: list[np.ndarray],
    cascade: cv2.CascadeClassifier,
    profile_cascade: cv2.CascadeClassifier | None = None,
) -> list[_FaceDetection]:
    detections: list[_FaceDetection] = []
    for idx, gray in enumerate(grays):
        height, width = gray.shape[:2]
        min_dim = max(24, int(min(height, width) * 0.045))
        max_w = max(min_dim + 4, int(width * 0.40))
        max_h = max(min_dim + 4, int(height * 0.55))

        faces = cascade.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=4,
            minSize=(min_dim, min_dim),
            maxSize=(max_w, max_h),
        )
        frame_boxes: list[tuple[int, int, int, int]] = (
            [(int(fx), int(fy), int(fw), int(fh)) for fx, fy, fw, fh in faces]
            if len(faces) > 0
            else []
        )

        # Fallback for 3/4-profile streamers looking toward a second monitor
        if not frame_boxes and profile_cascade is not None:
            prof_left = profile_cascade.detectMultiScale(
                gray,
                scaleFactor=1.1,
                minNeighbors=4,
                minSize=(min_dim, min_dim),
                maxSize=(max_w, max_h),
            )
            for fx, fy, fw, fh in prof_left:
                frame_boxes.append((int(fx), int(fy), int(fw), int(fh)))
            if not frame_boxes:
                flipped = cv2.flip(gray, 1)
                prof_right = profile_cascade.detectMultiScale(
                    flipped,
                    scaleFactor=1.1,
                    minNeighbors=4,
                    minSize=(min_dim, min_dim),
                    maxSize=(max_w, max_h),
                )
                for fx, fy, fw, fh in prof_right:
                    frame_boxes.append((int(width - fx - fw), int(fy), int(fw), int(fh)))

        for fx, fy, fw, fh in frame_boxes:
            w_norm = float(fw) / float(width)
            h_norm = float(fh) / float(height)
            # Filter tiny noisy fragments or implausible aspect boxes
            if w_norm < 0.03 or h_norm < 0.04:
                continue
            cx_norm = (float(fx) + float(fw) * 0.5) / float(width)
            cy_norm = (float(fy) + float(fh) * 0.5) / float(height)
            detections.append(
                _FaceDetection(
                    frame_idx=idx,
                    cx=cx_norm,
                    cy=cy_norm,
                    w=w_norm,
                    h=h_norm,
                )
            )
    return detections


def _cluster_detections(detections: list[_FaceDetection]) -> list[_FaceCluster]:
    clusters: list[_FaceCluster] = []
    for det in detections:
        best_cluster: _FaceCluster | None = None
        best_dist = 0.11
        for cluster in clusters:
            dist = cluster.distance_to(det.cx, det.cy)
            if dist < best_dist:
                best_dist = dist
                best_cluster = cluster
        if best_cluster is None:
            clusters.append(_FaceCluster(detections=[det]))
            continue
        existing_in_frame = [d for d in best_cluster.detections if d.frame_idx == det.frame_idx]
        if existing_in_frame:
            prev = existing_in_frame[0]
            prev_score = _webcam_zone_weight(prev.cx, prev.cy) * (prev.w * prev.h)
            curr_score = _webcam_zone_weight(det.cx, det.cy) * (det.w * det.h)
            if curr_score > prev_score:
                best_cluster.detections.remove(prev)
                best_cluster.detections.append(det)
        else:
            best_cluster.detections.append(det)
    return clusters


def _find_static_edge_peak(
    grad_map: np.ndarray,
    std_map: np.ndarray,
    band_start: int,
    band_end: int,
    search_start: int,
    search_end: int,
    *,
    vertical_edge: bool,
) -> int | None:
    """Locate a strong static rectangular border line within a search band."""
    if band_end <= band_start + 4 or search_end <= search_start + 4:
        return None
    if vertical_edge:
        grad_slice = grad_map[band_start:band_end, search_start:search_end]
        std_slice = std_map[band_start:band_end, search_start:search_end]
        grad_profile = np.mean(grad_slice, axis=0)
        std_profile = np.mean(std_slice, axis=0)
    else:
        grad_slice = grad_map[search_start:search_end, band_start:band_end]
        std_slice = std_map[search_start:search_end, band_start:band_end]
        grad_profile = np.mean(grad_slice, axis=1)
        std_profile = np.mean(std_slice, axis=1)

    if grad_profile.size < 4:
        return None
    # Favor edges that remain stable (low temporal std) across the 5 frames.
    weighted = grad_profile / (1.0 + 0.04 * std_profile)
    peak_idx = int(np.argmax(weighted))
    peak_val = float(weighted[peak_idx])
    baseline = float(np.median(weighted))
    if peak_val > max(16.0, baseline * 2.1):
        return search_start + peak_idx
    return None


def _refine_webcam_bounds(
    grays: list[np.ndarray],
    cluster: _FaceCluster,
) -> tuple[CropBox, int]:
    """Refine webcam box using static edge boundaries plus ergonomic head-and-shoulders padding."""
    height, width = grays[0].shape[:2]
    stack = np.stack([g.astype(np.float32) for g in grays], axis=0)
    mean_gray = np.mean(stack, axis=0)
    std_gray = np.std(stack, axis=0)

    gx = np.abs(cv2.Sobel(mean_gray, cv2.CV_32F, 1, 0, ksize=3))
    gy = np.abs(cv2.Sobel(mean_gray, cv2.CV_32F, 0, 1, ksize=3))

    mcx = cluster.median_cx * width
    mcy = cluster.median_cy * height
    mfw = cluster.median_w * width
    mfh = cluster.median_h * height

    band_y0 = max(0, int(round(mcy - 0.7 * mfh)))
    band_y1 = min(height, int(round(mcy + 1.5 * mfh)))
    band_x0 = max(0, int(round(mcx - 1.1 * mfw)))
    band_x1 = min(width, int(round(mcx + 1.1 * mfw)))

    ergo_x0 = max(0, int(round(mcx - 1.45 * mfw)))
    ergo_x1 = min(width, int(round(mcx + 1.45 * mfw)))
    ergo_y0 = max(0, int(round(mcy - 0.85 * mfh)))
    ergo_y1 = min(height, int(round(mcy + 1.95 * mfh)))

    border_hits = 0

    if ergo_x0 <= int(0.03 * width):
        x0 = 0
        border_hits += 1
    else:
        edge = _find_static_edge_peak(
            gx,
            std_gray,
            band_y0,
            band_y1,
            max(0, int(round(mcx - 2.5 * mfw))),
            max(0, int(round(mcx - 0.65 * mfw))),
            vertical_edge=True,
        )
        if edge is not None:
            x0 = edge
            border_hits += 1
        else:
            x0 = ergo_x0

    if ergo_x1 >= int(0.97 * width):
        x1 = width
        border_hits += 1
    else:
        edge = _find_static_edge_peak(
            gx,
            std_gray,
            band_y0,
            band_y1,
            min(width, int(round(mcx + 0.65 * mfw))),
            min(width, int(round(mcx + 2.5 * mfw))),
            vertical_edge=True,
        )
        if edge is not None:
            x1 = edge
            border_hits += 1
        else:
            x1 = ergo_x1

    if ergo_y0 <= int(0.03 * height):
        y0 = 0
        border_hits += 1
    else:
        edge = _find_static_edge_peak(
            gy,
            std_gray,
            band_x0,
            band_x1,
            max(0, int(round(mcy - 1.6 * mfh))),
            max(0, int(round(mcy - 0.45 * mfh))),
            vertical_edge=False,
        )
        if edge is not None:
            y0 = edge
            border_hits += 1
        else:
            y0 = ergo_y0

    if ergo_y1 >= int(0.97 * height):
        y1 = height
        border_hits += 1
    else:
        edge = _find_static_edge_peak(
            gy,
            std_gray,
            band_x0,
            band_x1,
            min(height, int(round(mcy + 0.65 * mfh))),
            min(height, int(round(mcy + 2.7 * mfh))),
            vertical_edge=False,
        )
        if edge is not None:
            y1 = edge
            border_hits += 1
        else:
            y1 = ergo_y1

    if x1 - x0 < int(0.08 * width):
        x0, x1 = ergo_x0, ergo_x1
    if y1 - y0 < int(0.08 * height):
        y0, y1 = ergo_y0, ergo_y1

    # Corner-cam snapping: when a streamer sits slightly off-center inside a corner webcam
    # (e.g., top-left cam where face cx is ~0.14..0.22), snap the outer edge flush to the
    # screen border and enforce an ergonomic corner webcam span so the box covers the full cam.
    if cluster.median_cx <= 0.26 and x0 <= int(0.09 * width):
        x0 = 0
        x1 = max(x1, int(round(max(0.22 * width, mcx + 1.55 * mfw))))
    elif cluster.median_cx >= 0.74 and x1 >= int(0.91 * width):
        x1 = width
        x0 = min(x0, int(round(min(0.78 * width, mcx - 1.55 * mfw))))

    if cluster.median_cy <= 0.30 and y0 <= int(0.09 * height):
        y0 = 0
        y1 = max(y1, int(round(max(0.26 * height, mcy + 2.05 * mfh))))
    elif cluster.median_cy >= 0.70 and y1 >= int(0.91 * height):
        y1 = height
        y0 = min(y0, int(round(min(0.74 * height, mcy - 1.05 * mfh))))

    return _to_clamped_crop_box(x0 / width, y0 / height, x1 / width, y1 / height), border_hits


def _build_gameplay_box_for_webcam(webcam_box: CropBox) -> CropBox:
    """Position the default gameplay box so it does not collide with a side/corner webcam."""
    gp_w = 0.56
    if webcam_box.x <= 0.08 and (webcam_box.x + webcam_box.w) > 0.22:
        gp_x = min(0.42, round(webcam_box.x + webcam_box.w, 4))
        return CropBox(x=gp_x, y=0.0, w=min(gp_w, round(1.0 - gp_x, 4)), h=1.0)
    if webcam_box.x >= 0.65 and webcam_box.x < 0.78:
        gp_x = max(0.04, round(webcam_box.x - gp_w, 4))
        return CropBox(x=gp_x, y=0.0, w=gp_w, h=1.0)
    return _DEFAULT_GAMEPLAY_WITH_CAM


def _to_clamped_crop_box(x0: float, y0: float, x1: float, y1: float) -> CropBox:
    """Convert normalized corners into a strictly valid CropBox in [0, 1]."""
    nx = max(0.0, min(0.95, round(x0, 4)))
    ny = max(0.0, min(0.95, round(y0, 4)))
    nw = max(0.05, round(x1 - nx, 4))
    nh = max(0.05, round(y1 - ny, 4))
    if nx + nw > 1.0:
        nw = max(0.05, math.floor((1.0 - nx) * 10000.0) / 10000.0)
        if nx + nw > 1.0:
            nx = round(1.0 - nw, 4)
    if ny + nh > 1.0:
        nh = max(0.05, math.floor((1.0 - ny) * 10000.0) / 10000.0)
        if ny + nh > 1.0:
            ny = round(1.0 - nh, 4)
    return CropBox(x=nx, y=ny, w=nw, h=nh)


def detect_webcam(video_path: Path, ffmpeg_path: str) -> WebcamDetectionResult:
    """Detect whether a 16:9 clip contains a corner/edge streamer webcam."""
    target_path = Path(video_path)
    if not target_path.is_file():
        raise FileNotFoundError(f"Video file not found for webcam detection: {target_path}")

    frames = _sample_frames(target_path, ffmpeg_path, count=5)
    if not frames:
        return WebcamDetectionResult(
            found=False,
            confidence=0.0,
            webcam_box=None,
            gameplay_box=_DEFAULT_GAMEPLAY_NO_CAM,
            layout_mode="blur_no_cam",
        )

    base_h, base_w = frames[0].shape[:2]
    grays = [
        cv2.cvtColor(
            cv2.resize(frame, (base_w, base_h))
            if frame.shape[:2] != (base_h, base_w)
            else frame,
            cv2.COLOR_BGR2GRAY,
        )
        for frame in frames
    ]

    cascade = _load_face_cascade()
    profile_cascade = _load_profile_cascade()
    detections = _detect_faces_per_frame(grays, cascade, profile_cascade)
    if not detections:
        return WebcamDetectionResult(
            found=False,
            confidence=0.0,
            webcam_box=None,
            gameplay_box=_DEFAULT_GAMEPLAY_NO_CAM,
            layout_mode="blur_no_cam",
        )

    clusters = _cluster_detections(detections)
    best_result: tuple[float, CropBox] | None = None
    best_confidence = 0.0

    for cluster in clusters:
        zone_weight = _webcam_zone_weight(cluster.median_cx, cluster.median_cy)
        temporal_ratio = cluster.frame_hits / float(len(grays))
        max_jitter = max(
            (cluster.distance_to(det.cx, det.cy) for det in cluster.detections),
            default=0.0,
        )
        stability_norm = max(0.0, 1.0 - max_jitter / 0.08)
        box, border_hits = _refine_webcam_bounds(grays, cluster)
        border_norm = min(1.0, border_hits / 4.0)

        raw_score = (
            0.42 * temporal_ratio
            + 0.38 * zone_weight
            + 0.10 * stability_norm
            + 0.10 * border_norm
        )
        confidence = raw_score * (0.35 + 0.65 * zone_weight)
        if len(grays) >= 3 and cluster.frame_hits < 2:
            confidence = min(confidence, 0.49)
        confidence = round(max(0.0, min(0.99, confidence)), 4)

        if confidence > best_confidence:
            best_confidence = confidence
            best_result = (confidence, box)

    if best_result is None or best_confidence < _CONFIDENCE_THRESHOLD:
        return WebcamDetectionResult(
            found=False,
            confidence=best_confidence,
            webcam_box=None,
            gameplay_box=_DEFAULT_GAMEPLAY_NO_CAM,
            layout_mode="blur_no_cam",
        )

    return WebcamDetectionResult(
        found=True,
        confidence=best_confidence,
        webcam_box=best_result[1],
        gameplay_box=_build_gameplay_box_for_webcam(best_result[1]),
        layout_mode="split_top_cam",
    )
