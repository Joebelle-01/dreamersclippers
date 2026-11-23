"""Local Whisper ASR transcription and ASS/SRT closed-caption formatting."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from backend.config import resolve_ffmpeg_path
from backend.models import CaptionSegment, CaptionWord
from backend.security import CropBox

logger = logging.getLogger(__name__)

_MODEL_LOCK = threading.Lock()
_MODEL_CACHE: dict[str, Any] = {}

_SENTENCE_END_CHARS = (".", "!", "?")
_CLAUSE_END_CHARS = (",", ";", ":")


def sanitize_ass_text(text: str) -> str:
    """Strip control characters and ASS override delimiters from caption text."""
    if not isinstance(text, str) or not text:
        return ""
    cleaned_chars: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch in ("\r", "\n", "\t", "\v", "\f"):
            cleaned_chars.append(" ")
        elif code < 32 or code == 127 or 0x80 <= code <= 0x9F:
            continue
        elif ch == "{":
            cleaned_chars.append("(")
        elif ch == "}":
            cleaned_chars.append(")")
        elif ch == "\\":
            cleaned_chars.append(" ")
        else:
            cleaned_chars.append(ch)
    return " ".join("".join(cleaned_chars).split())


def escape_ffmpeg_filter_path(path: Path) -> str:
    """Escape a filesystem path for FFmpeg's `ass='...'` filter expression on Windows/POSIX."""
    posix_path = Path(path).resolve().as_posix()
    return (
        posix_path.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def _format_ass_time(seconds: float) -> str:
    total_cs = max(0, int(round(float(seconds) * 100.0)))
    hours, rem_cs = divmod(total_cs, 360000)
    minutes, rem_cs = divmod(rem_cs, 6000)
    secs, cs = divmod(rem_cs, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _format_srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000.0)))
    hours, rem_ms = divmod(total_ms, 3600000)
    minutes, rem_ms = divmod(rem_ms, 60000)
    secs, ms = divmod(rem_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _cuda_cublas_available() -> bool:
    """Return True if cublas64_12 (required by CTranslate2 CUDA on Windows) is loadable."""
    if os.name != "nt":
        return True
    try:
        ctypes.WinDLL("cublas64_12.dll")
        return True
    except OSError:
        return False


def _get_whisper_model(model_size: str, *, force_cpu: bool = False) -> Any:
    """Lazily initialize and cache a faster-whisper model (CUDA float16 -> CPU int8 fallback)."""
    normalized = (model_size or "base").strip() or "base"
    cache_key = f"{normalized}:cpu" if force_cpu else normalized
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(cache_key)
        if cached is not None:
            return cached

        # Silence benign symlink warnings on Windows without Developer Mode
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from faster_whisper import WhisperModel

        model: Any = None
        if not force_cpu and _cuda_cublas_available():
            try:
                model = WhisperModel(normalized, device="cuda", compute_type="float16")
                logger.info("Loaded faster-whisper '%s' on CUDA float16", normalized)
            except Exception as exc:
                logger.warning(
                    "CUDA faster-whisper init unavailable (%s); falling back to CPU int8",
                    exc,
                )
                model = None

        if model is None:
            model = WhisperModel(normalized, device="cpu", compute_type="int8")
            logger.info("Loaded faster-whisper '%s' on CPU int8", normalized)
            _MODEL_CACHE[f"{normalized}:cpu"] = model

        _MODEL_CACHE[cache_key] = model
        return model


def _group_words_into_phrases(
    words: list[CaptionWord],
    *,
    max_words: int = 4,
    max_chars: int = 26,
    max_gap_sec: float = 0.45,
) -> list[CaptionSegment]:
    """Group word-level tokens into readable short-form vertical caption phrases."""
    if not words:
        return []

    phrases: list[CaptionSegment] = []
    bucket: list[CaptionWord] = []
    bucket_chars = 0

    def _flush() -> None:
        nonlocal bucket, bucket_chars
        if not bucket:
            return
        text = sanitize_ass_text(" ".join(item.word for item in bucket))
        if text:
            start = max(0.0, float(bucket[0].start))
            end = max(start + 0.12, float(bucket[-1].end))
            phrases.append(
                CaptionSegment(
                    start=round(start, 3),
                    end=round(end, 3),
                    text=text,
                    words=list(bucket),
                )
            )
        bucket = []
        bucket_chars = 0

    for item in words:
        clean_word = sanitize_ass_text(item.word)
        if not clean_word:
            continue
        w_start = max(0.0, float(item.start))
        w_end = max(w_start + 0.04, float(item.end))
        normalized_word = CaptionWord(
            start=round(w_start, 3),
            end=round(w_end, 3),
            word=clean_word,
        )

        if bucket:
            gap = normalized_word.start - bucket[-1].end
            projected_chars = bucket_chars + 1 + len(clean_word)
            if gap > max_gap_sec or len(bucket) >= max_words or projected_chars > max_chars:
                _flush()

        bucket.append(normalized_word)
        bucket_chars = len(clean_word) if len(bucket) == 1 else bucket_chars + 1 + len(clean_word)

        if clean_word.endswith(_SENTENCE_END_CHARS) or (
            clean_word.endswith(_CLAUSE_END_CHARS) and len(bucket) >= 2
        ):
            _flush()

    _flush()
    return phrases


def _extract_wav_audio(
    video_path: Path,
    wav_path: Path,
    *,
    trim_start_sec: float,
    trim_end_sec: float,
    ffmpeg_path: str,
    timeout_sec: int = 120,
    channels: int = 1,
) -> bool:
    """Extract 16 kHz PCM WAV for Whisper; returns False if clip has no audio stream."""
    resolved_ffmpeg = ffmpeg_path or resolve_ffmpeg_path()
    if not resolved_ffmpeg:
        raise RuntimeError("ffmpeg executable path could not be resolved")

    start_sec = max(0.0, float(trim_start_sec))
    argv: list[str] = [
        resolved_ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_sec:.3f}",
        "-i",
        str(video_path),
    ]
    if trim_end_sec > start_sec:
        argv.extend(["-t", f"{max(0.1, float(trim_end_sec) - start_sec):.3f}"])
    argv.extend(
        [
            "-vn",
            "-ac",
            str(max(1, min(2, int(channels)))),
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ]
    )

    proc = subprocess.run(
        argv,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(30, int(timeout_sec)),
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip()
        if "matches no streams" in err or "does not contain any stream" in err:
            return False
        raise RuntimeError(f"Failed to extract audio for captions: {err}")

    return wav_path.is_file() and wav_path.stat().st_size > 44


def _load_wav_channels_float32(wav_path: Path) -> tuple[np.ndarray, np.ndarray | None]:
    """Load a 16 kHz s16le WAV file into (mono_1d_float32, optional_stereo_2d_float32)."""
    with wave.open(str(wav_path), "rb") as wf:
        n_channels = wf.getnchannels()
        frame_count = wf.getnframes()
        if frame_count <= 0:
            return np.zeros(0, dtype=np.float32), None
        pcm_bytes = wf.readframes(frame_count)
    if not pcm_bytes:
        return np.zeros(0, dtype=np.float32), None
    raw = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels >= 2:
        usable = (raw.size // n_channels) * n_channels
        reshaped = raw[:usable].reshape(-1, n_channels)[:, :2]
        mono = np.mean(reshaped, axis=1).astype(np.float32)
        return mono, reshaped
    return raw, None


def _load_wav_float32(wav_path: Path) -> np.ndarray:
    """Load a 16 kHz mono/stereo s16le WAV file into a 1D float32 array in [-1.0, 1.0].

    Passing a float32 NumPy array directly to faster-whisper's WhisperModel.transcribe()
    bypasses faster_whisper.audio.decode_audio(), which calls av.open(..., metadata_errors="ignore")
    and fails on PyAV 19+ where metadata_errors was removed.
    """
    mono, _stereo = _load_wav_channels_float32(wav_path)
    return mono


def _transcribe_wav_words(
    wav_path: Path,
    model_size: str,
    *,
    offset_sec: float = 0.0,
) -> tuple[list[CaptionWord], list[dict[str, Any]]]:
    """Run Whisper over a pre-extracted WAV file and collect words + sentence segments."""
    audio_samples = _load_wav_float32(wav_path)
    if audio_samples.size == 0:
        return [], []

    model = _get_whisper_model(model_size)
    try:
        raw_iter, _info = model.transcribe(
            audio_samples,
            word_timestamps=True,
            vad_filter=True,
        )
        raw_segments = list(raw_iter)
    except RuntimeError as exc:
        err_lower = str(exc).lower()
        if any(tok in err_lower for tok in ("cublas", "cudnn", "cuda")):
            logger.warning(
                "CUDA runtime library missing during Whisper transcribe (%s); retrying on CPU int8",
                exc,
            )
            model = _get_whisper_model(model_size, force_cpu=True)
            raw_iter, _info = model.transcribe(
                audio_samples,
                word_timestamps=True,
                vad_filter=True,
            )
            raw_segments = list(raw_iter)
        else:
            raise

    base_offset = max(0.0, float(offset_sec))
    collected_words: list[CaptionWord] = []
    speech_segments: list[dict[str, Any]] = []

    for seg in raw_segments:
        seg_words = getattr(seg, "words", None)
        seg_word_list: list[CaptionWord] = []
        if seg_words:
            for w in seg_words:
                token = sanitize_ass_text(str(getattr(w, "word", "")))
                if not token:
                    continue
                w_start = base_offset + max(0.0, float(getattr(w, "start", 0.0)))
                w_end = max(w_start + 0.04, base_offset + float(getattr(w, "end", 0.1)))
                cw = CaptionWord(start=round(w_start, 3), end=round(w_end, 3), word=token)
                collected_words.append(cw)
                seg_word_list.append(cw)
        else:
            seg_text_raw = sanitize_ass_text(str(getattr(seg, "text", "")))
            if not seg_text_raw:
                continue
            s_start = base_offset + max(0.0, float(getattr(seg, "start", 0.0)))
            s_end = max(s_start + 0.1, base_offset + float(getattr(seg, "end", 0.5)))
            parts = seg_text_raw.split()
            step = (s_end - s_start) / max(1, len(parts))
            for idx, part in enumerate(parts):
                w_start = s_start + idx * step
                w_end = s_start + (idx + 1) * step
                cw = CaptionWord(start=round(w_start, 3), end=round(w_end, 3), word=part)
                collected_words.append(cw)
                seg_word_list.append(cw)

        if seg_word_list:
            seg_text = sanitize_ass_text(str(getattr(seg, "text", ""))) or " ".join(
                cw.word for cw in seg_word_list
            )
            seg_start = seg_word_list[0].start
            seg_end = max(seg_start + 0.08, seg_word_list[-1].end)
            speech_segments.append(
                {
                    "start": round(seg_start, 3),
                    "end": round(seg_end, 3),
                    "text": seg_text,
                    "words": seg_word_list,
                }
            )

    return collected_words, speech_segments


def transcribe_window_words(
    video_path: Path,
    start_sec: float,
    end_sec: float,
    ffmpeg_path: str,
    model_size: str = "base",
) -> tuple[list[CaptionWord], list[dict[str, Any]]]:
    """Transcribe a [start_sec, end_sec] window with timestamps anchored to absolute video time."""
    src_path = Path(video_path)
    if not src_path.is_file():
        raise FileNotFoundError(f"Video not found for transcription: {src_path}")

    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav_path = Path(handle.name)
    handle.close()

    try:
        has_audio = _extract_wav_audio(
            src_path,
            wav_path,
            trim_start_sec=start_sec,
            trim_end_sec=end_sec,
            ffmpeg_path=ffmpeg_path,
            timeout_sec=600,
        )
        if not has_audio:
            return [], []
        return _transcribe_wav_words(wav_path, model_size, offset_sec=start_sec)
    finally:
        try:
            if wav_path.exists():
                os.unlink(wav_path)
        except OSError:
            pass


def slice_captions_for_window(
    words: list[CaptionWord],
    clip_start: float,
    clip_end: float,
) -> list[CaptionSegment]:
    """Slice pre-transcribed words for [clip_start, clip_end] into relative caption phrases."""
    if not words:
        return []
    win_start = max(0.0, float(clip_start))
    win_end = max(win_start + 0.1, float(clip_end))
    shifted: list[CaptionWord] = []
    for item in words:
        if item.end < win_start - 0.05 or item.start > win_end + 0.05:
            continue
        rel_start = max(0.0, float(item.start) - win_start)
        rel_end = max(rel_start + 0.04, float(item.end) - win_start)
        shifted.append(
            CaptionWord(
                start=round(rel_start, 3),
                end=round(rel_end, 3),
                word=item.word,
            )
        )
    return _group_words_into_phrases(shifted)


def _compute_acoustic_segment_features(
    audio_mono: np.ndarray,
    stereo_lr: np.ndarray | None,
    sample_rate: int,
    start_sec: float,
    end_sec: float,
) -> dict[str, Any]:
    """Compute near-field close-mic acoustic features and a 16-D speaker voiceprint vector."""
    n_total = int(audio_mono.size)
    if n_total == 0:
        return {
            "vocal_rms": 0.0,
            "proximity_ratio": 0.0,
            "dry_presence_ratio": 0.0,
            "center_lock_score": 0.5,
            "voiceprint": np.zeros(16, dtype=np.float32),
        }

    i0 = max(0, min(n_total - 1, int(round(start_sec * sample_rate))))
    i1 = max(i0 + 1, min(n_total, int(round(end_sec * sample_rate))))
    seg_mono = audio_mono[i0:i1]
    if seg_mono.size < 256:
        seg_mono = np.pad(seg_mono, (0, 256 - seg_mono.size))

    # Stereo Mid/Side center-channel lock (streamer mics are mono/center-panned: L == R)
    center_lock_score = 0.85
    if stereo_lr is not None and stereo_lr.shape[0] >= i1 and stereo_lr.shape[1] >= 2:
        seg_lr = stereo_lr[i0:i1, :2]
        mid = 0.5 * (seg_lr[:, 0] + seg_lr[:, 1])
        side = 0.5 * (seg_lr[:, 0] - seg_lr[:, 1])
        mid_rms = float(np.sqrt(np.mean(np.square(mid)) + 1e-12))
        side_rms = float(np.sqrt(np.mean(np.square(side)) + 1e-12))
        side_ratio = side_rms / (mid_rms + 1e-7)
        center_lock_score = float(np.clip(1.0 - 1.75 * side_ratio, 0.0, 1.0))

    # Short-time FFT over 1024-sample (64 ms) Hann frames
    n_fft = 1024
    hop = 512
    if seg_mono.size < n_fft:
        padded = np.pad(seg_mono, (0, n_fft - seg_mono.size))
        frames = padded[np.newaxis, :]
    else:
        n_frames = 1 + (seg_mono.size - n_fft) // hop
        strides = (seg_mono.strides[0] * hop, seg_mono.strides[0])
        frames = np.lib.stride_tricks.as_strided(
            seg_mono,
            shape=(max(1, n_frames), n_fft),
            strides=strides,
        ).copy()

    window = np.hanning(n_fft).astype(np.float32)
    spec = np.abs(np.fft.rfft(frames * window[np.newaxis, :], axis=1)) ** 2
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / float(sample_rate))

    mean_power = np.mean(spec, axis=0) + 1e-12
    total_energy = float(np.sum(mean_power))

    # Near-field close-mic bands: proximity chest warmth (110-280 Hz) + dry vocal presence (110-3400 Hz)
    vocal_mask = (freqs >= 110.0) & (freqs <= 3400.0)
    prox_mask = (freqs >= 110.0) & (freqs <= 285.0)
    vocal_energy = float(np.sum(mean_power[vocal_mask]))
    prox_energy = float(np.sum(mean_power[prox_mask]))

    frame_vocal_rms = np.sqrt(np.mean(spec[:, vocal_mask], axis=1) + 1e-12)
    # 80th percentile frame vocal RMS captures active syllables even across brief pauses
    vocal_rms = float(np.percentile(frame_vocal_rms, 80)) if frame_vocal_rms.size > 0 else 0.0
    proximity_ratio = float(np.clip(prox_energy / (vocal_energy + 1e-9), 0.0, 1.0))
    dry_presence_ratio = float(np.clip(vocal_energy / (total_energy + 1e-9), 0.0, 1.0))

    # 13 log-spaced vocal filterbank bands (110..3600 Hz) + spectral centroid + rolloff + pitch F0
    band_edges = np.geomspace(110.0, 3600.0, 14)
    band_vec = np.zeros(13, dtype=np.float32)
    for b_idx in range(13):
        b_mask = (freqs >= band_edges[b_idx]) & (freqs < band_edges[b_idx + 1])
        band_vec[b_idx] = float(np.log10(np.mean(mean_power[b_mask]) + 1e-10))
    band_vec -= float(np.mean(band_vec))
    band_norm = float(np.linalg.norm(band_vec))
    if band_norm > 1e-6:
        band_vec /= band_norm

    vocal_freqs = freqs[vocal_mask]
    vocal_pow = mean_power[vocal_mask]
    centroid_hz = float(np.sum(vocal_freqs * vocal_pow) / (np.sum(vocal_pow) + 1e-9))
    centroid_norm = float(np.clip((centroid_hz - 200.0) / 2400.0, 0.0, 1.0))

    cumsum_pow = np.cumsum(vocal_pow)
    rolloff_idx = int(np.searchsorted(cumsum_pow, 0.85 * cumsum_pow[-1]))
    rolloff_hz = float(vocal_freqs[min(rolloff_idx, vocal_freqs.size - 1)])
    rolloff_norm = float(np.clip((rolloff_hz - 400.0) / 3000.0, 0.0, 1.0))

    # Pitch F0 estimate via harmonic peak in 85..340 Hz
    f0_mask = (freqs >= 85.0) & (freqs <= 340.0)
    f0_freqs = freqs[f0_mask]
    f0_pow = mean_power[f0_mask]
    f0_hz = float(f0_freqs[int(np.argmax(f0_pow))]) if f0_pow.size > 0 else 150.0
    f0_norm = float(np.clip((f0_hz - 85.0) / 255.0, 0.0, 1.0))

    voiceprint = np.concatenate(
        [
            band_vec * 0.80,
            np.array([centroid_norm * 0.45, rolloff_norm * 0.35, f0_norm * 0.55], dtype=np.float32),
        ]
    )
    vp_norm = float(np.linalg.norm(voiceprint))
    if vp_norm > 1e-6:
        voiceprint /= vp_norm

    return {
        "vocal_rms": vocal_rms,
        "proximity_ratio": proximity_ratio,
        "dry_presence_ratio": dry_presence_ratio,
        "center_lock_score": center_lock_score,
        "voiceprint": voiceprint,
    }


def _detect_ingame_subtitle_frame_score(
    gray_frame: np.ndarray,
    webcam_box: CropBox | None,
) -> float:
    """Measure bottom-center 16:9 in-game subtitle glyph density in [0.0, 1.0]."""
    h, w = gray_frame.shape[:2]
    if h < 90 or w < 160:
        return 0.0

    # Bottom-center gameplay subtitle strip: x in [0.22..0.78], y in [0.75..0.93]
    x0, x1 = int(round(0.22 * w)), int(round(0.78 * w))
    y0, y1 = int(round(0.75 * h)), int(round(0.93 * h))

    # Avoid any overlap if the streamer's webcam is positioned at the bottom
    if webcam_box is not None and (webcam_box.y + webcam_box.h) > 0.72:
        wx0 = int(round(webcam_box.x * w))
        wx1 = int(round((webcam_box.x + webcam_box.w) * w))
        if wx1 <= int(0.5 * w):
            x0 = max(x0, wx1 + 8)
        elif wx0 >= int(0.5 * w):
            x1 = min(x1, wx0 - 8)

    if x1 - x0 < 40 or y1 - y0 < 16:
        return 0.0

    strip = gray_frame[y0:y1, x0:x1]
    # Game subtitles consist of bright glyph strokes (>= 205) outlined by dark pixels (<= 65)
    bright_mask = strip >= 205
    bright_ratio = float(np.mean(bright_mask))
    if bright_ratio < 0.004 or bright_ratio > 0.22:
        return 0.0

    kernel = np.ones((3, 5), dtype=np.uint8)
    dark_neighbor = cv2.erode(strip, kernel) <= 65
    outlined_glyphs = bright_mask & dark_neighbor
    glyph_ratio = float(np.mean(outlined_glyphs))

    gx = np.abs(cv2.Sobel(strip, cv2.CV_32F, 1, 0, ksize=3))
    gy = np.abs(cv2.Sobel(strip, cv2.CV_32F, 0, 1, ksize=3))
    horiz_stroke_contrast = float(np.mean(np.maximum(0.0, gx - 0.55 * gy))) / 255.0

    raw_score = (glyph_ratio * 24.0) + (horiz_stroke_contrast * 1.8)
    return float(np.clip(raw_score, 0.0, 1.0))


def _compute_webcam_and_subtitle_activity(
    video_path: Path,
    trim_start_sec: float,
    trim_end_sec: float,
    webcam_box: CropBox | None,
    *,
    sample_hz: float = 8.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Sample upper-face/head speaking motion in webcam_box and in-game subtitle presence."""
    duration = max(0.2, float(trim_end_sec) - max(0.0, float(trim_start_sec)))
    n_steps = max(2, min(720, int(round(duration * sample_hz))))
    head_activity = np.zeros(n_steps, dtype=np.float32)
    subtitle_activity = np.zeros(n_steps, dtype=np.float32)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        cap.release()
        return head_activity, subtitle_activity

    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        if not np.isfinite(fps) or fps <= 1.0:
            fps = 30.0

        prev_cam_patch: np.ndarray | None = None
        start_base = max(0.0, float(trim_start_sec))
        for step_idx in range(n_steps):
            t_sec = start_base + (step_idx + 0.5) / sample_hz
            frame_no = max(0, int(round(t_sec * fps)))
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
            ok, frame = cap.read()
            if not ok or frame is None or frame.size == 0:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            subtitle_activity[step_idx] = _detect_ingame_subtitle_frame_score(gray, webcam_box)

            if webcam_box is not None:
                fh, fw = gray.shape[:2]
                cx0 = max(0, min(fw - 8, int(round(webcam_box.x * fw))))
                cy0 = max(0, min(fh - 8, int(round(webcam_box.y * fh))))
                cx1 = max(cx0 + 8, min(fw, int(round((webcam_box.x + webcam_box.w) * fw))))
                cy1 = max(cy0 + 8, min(fh, int(round((webcam_box.y + webcam_box.h) * fh))))
                cam_crop = gray[cy0:cy1, cx0:cx1]
                ch, cw = cam_crop.shape[:2]
                if ch >= 12 and cw >= 12:
                    # Focus on upper/mid face, cheeks, brow & head (y: 12%..78%) so a boom mic
                    # blocking the lower lips never blocks head/cheek speaking cadence.
                    ry0, ry1 = int(round(0.12 * ch)), int(round(0.78 * ch))
                    rx0, rx1 = int(round(0.15 * cw)), int(round(0.85 * cw))
                    roi = cv2.resize(
                        cam_crop[max(0, ry0) : max(ry0 + 4, ry1), max(0, rx0) : max(rx0 + 4, rx1)],
                        (48, 48),
                    ).astype(np.float32)
                    if prev_cam_patch is not None:
                        diff = float(np.mean(np.abs(roi - prev_cam_patch)))
                        head_activity[step_idx] = diff
                    prev_cam_patch = roi
    finally:
        cap.release()

    if head_activity.size > 1 and float(np.max(head_activity)) > 1e-4:
        p90 = float(np.percentile(head_activity, 90))
        scale = max(2.5, p90)
        head_activity = np.clip(head_activity / scale, 0.0, 1.0)

    return head_activity, subtitle_activity


def filter_streamer_speech_segments(
    speech_segments: list[dict[str, Any]],
    audio_mono: np.ndarray,
    stereo_lr: np.ndarray | None = None,
    head_activity: np.ndarray | None = None,
    ingame_subtitle_activity: np.ndarray | None = None,
    *,
    sample_rate: int = 16000,
    sample_hz: float = 8.0,
    has_webcam: bool = False,
    sensitivity: float = 0.50,
) -> list[CaptionWord]:
    """Filter transcribed speech segments to keep only the streamer's voice, rejecting game NPCs."""
    if not speech_segments:
        return []

    evaluated: list[dict[str, Any]] = []
    for seg in speech_segments:
        s_start = max(0.0, float(seg.get("start", 0.0)))
        s_end = max(s_start + 0.08, float(seg.get("end", s_start + 0.25)))
        words = list(seg.get("words") or [])
        if not words:
            continue

        ac = _compute_acoustic_segment_features(
            audio_mono,
            stereo_lr,
            sample_rate,
            s_start,
            s_end,
        )

        idx0 = max(0, int(np.floor(s_start * sample_hz)))
        idx1 = max(idx0 + 1, int(np.ceil(s_end * sample_hz)))

        head_val = 0.0
        if head_activity is not None and head_activity.size > 0:
            sl = head_activity[min(idx0, head_activity.size - 1) : min(idx1, head_activity.size)]
            if sl.size > 0:
                head_val = float(np.mean(sl))

        sub_val = 0.0
        if ingame_subtitle_activity is not None and ingame_subtitle_activity.size > 0:
            sl_sub = ingame_subtitle_activity[
                min(idx0, ingame_subtitle_activity.size - 1) : min(
                    idx1, ingame_subtitle_activity.size
                )
            ]
            if sl_sub.size > 0:
                sub_val = float(np.mean(sl_sub))

        evaluated.append(
            {
                "start": s_start,
                "end": s_end,
                "words": words,
                "vocal_rms": ac["vocal_rms"],
                "proximity_ratio": ac["proximity_ratio"],
                "dry_presence_ratio": ac["dry_presence_ratio"],
                "center_lock_score": ac["center_lock_score"],
                "voiceprint": ac["voiceprint"],
                "head_motion": head_val,
                "subtitle_presence": sub_val,
            }
        )

    if not evaluated:
        return []

    # Reference vocal RMS (85th percentile across speech bursts)
    rms_values = np.array([item["vocal_rms"] for item in evaluated], dtype=np.float32)
    ref_rms = max(1e-7, float(np.percentile(rms_values, 85)))

    for item in evaluated:
        ratio = item["vocal_rms"] / ref_rms
        # Convert relative dB (-16 dB .. 0 dB) into [0.0 .. 1.0]
        db_rel = 20.0 * float(np.log10(max(1e-5, ratio)))
        item["rel_vocal_level"] = float(np.clip((db_rel + 15.0) / 15.0, 0.0, 1.0))
        item["anchor_rank"] = (
            0.48 * item["rel_vocal_level"]
            + 0.24 * item["center_lock_score"]
            + 0.16 * item["dry_presence_ratio"]
            + (0.12 * item["head_motion"] if has_webcam else 0.0)
            - 0.25 * item["subtitle_presence"]
        )

    # Enroll Streamer Reference Voiceprint from the strongest close-mic anchor segment(s)
    sorted_anchors = sorted(evaluated, key=lambda x: x["anchor_rank"], reverse=True)
    top_k = max(1, min(3, (len(sorted_anchors) + 1) // 2))
    centroid = np.mean(
        np.stack([item["voiceprint"] for item in sorted_anchors[:top_k]], axis=0),
        axis=0,
    )
    c_norm = float(np.linalg.norm(centroid))
    if c_norm > 1e-6:
        centroid /= c_norm

    threshold = float(np.clip(sensitivity, 0.25, 0.78))
    kept_words: list[CaptionWord] = []

    for item in evaluated:
        vp_sim = float(np.clip(np.dot(item["voiceprint"], centroid), 0.0, 1.0))
        close_mic_score = (
            0.50 * item["rel_vocal_level"]
            + 0.28 * item["center_lock_score"]
            + 0.22 * item["dry_presence_ratio"]
        )
        # Positive-only webcam head/cheek motion bonus (never penalizes mic-occluded lips)
        head_boost = (0.12 * item["head_motion"]) if has_webcam else 0.0

        # Penalize segments where an in-game subtitle banner is active at the bottom of the screen
        # and the streamer is not loudly talking over it into their close-mic
        subtitle_penalty = 0.0
        if item["subtitle_presence"] >= 0.42 and item["rel_vocal_level"] < 0.78:
            subtitle_penalty = 0.28 * item["subtitle_presence"]

        streamer_score = (
            0.54 * close_mic_score
            + 0.36 * vp_sim
            + head_boost
            - subtitle_penalty
        )
        item["streamer_score"] = streamer_score
        if streamer_score >= threshold:
            kept_words.extend(item["words"])

    return kept_words


def transcribe_clip(
    video_path: Path,
    trim_start_sec: float,
    trim_end_sec: float,
    ffmpeg_path: str,
    model_size: str = "base",
    *,
    webcam_box: CropBox | None = None,
    streamer_only: bool = False,
    sensitivity: float = 0.50,
) -> list[CaptionSegment]:
    """Transcribe trimmed clip audio locally using faster-whisper into short-form phrases."""
    src_path = Path(video_path)
    if not src_path.is_file():
        raise FileNotFoundError(f"Clip video not found for captioning: {src_path}")

    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav_path = Path(handle.name)
    handle.close()

    try:
        has_audio = _extract_wav_audio(
            src_path,
            wav_path,
            trim_start_sec=trim_start_sec,
            trim_end_sec=trim_end_sec,
            ffmpeg_path=ffmpeg_path,
            channels=2 if streamer_only else 1,
        )
        if not has_audio:
            return []

        collected_words, speech_segments = _transcribe_wav_words(
            wav_path,
            model_size,
            offset_sec=0.0,
        )
        if streamer_only and speech_segments:
            audio_mono, stereo_lr = _load_wav_channels_float32(wav_path)
            head_act, sub_act = _compute_webcam_and_subtitle_activity(
                src_path,
                trim_start_sec,
                trim_end_sec,
                webcam_box,
                sample_hz=8.0,
            )
            filtered_words = filter_streamer_speech_segments(
                speech_segments,
                audio_mono,
                stereo_lr,
                head_act,
                sub_act,
                sample_rate=16000,
                sample_hz=8.0,
                has_webcam=webcam_box is not None,
                sensitivity=sensitivity,
            )
            return _group_words_into_phrases(filtered_words)

        return _group_words_into_phrases(collected_words)
    finally:
        try:
            if wav_path.exists():
                os.unlink(wav_path)
        except OSError:
            pass


_ASS_STYLE_PREFIXES: dict[str, str] = {
    "clean_box": (
        "Style: Default,Arial,60,&H00FFFFFF,&H00FFFFFF,&H00101216,&HC0080A0D,"
        "-1,0,0,0,100,100,0,0,3,10,0"
    ),
    "classic_cc": (
        "Style: Default,Arial,58,&H00F8F8F8,&H00F8F8F8,&H000A0A0A,&H70000000,"
        "-1,0,0,0,100,100,0,0,1,3,2"
    ),
    "bold_pop": (
        "Style: Default,Arial,66,&H00FFFFFF,&H0024A8F5,&H00080808,&H90000000,"
        "-1,0,0,0,100,100,0,0,1,4,2"
    ),
}


def build_ass_subtitles(
    segments: list[CaptionSegment],
    layout_mode: str = "split_top_cam",
    split_ratio: float = 0.35,
    caption_style: str = "bold_pop",
    caption_position: str = "seam",
) -> str:
    """Build a 1080x1920 ASS subtitle document styled for seam or lower-safe placement."""
    clamped_ratio = max(0.25, min(0.55, float(split_ratio)))
    use_seam = caption_position == "seam" and layout_mode == "split_top_cam"
    if use_seam:
        seam_y = int(round(1920 * clamped_ratio))
        alignment = 5
        margin_v = 20
        pos_prefix = f"{{\\pos(540,{seam_y})}}"
    else:
        alignment = 2
        margin_v = 420 if layout_mode == "split_top_cam" else 360
        if layout_mode == "split_top_cam" and clamped_ratio >= 0.48:
            margin_v = 340
        pos_prefix = ""

    style_prefix = _ASS_STYLE_PREFIXES.get(caption_style, _ASS_STYLE_PREFIXES["bold_pop"])
    style_line = f"{style_prefix},{alignment},64,64,{margin_v},1"

    header = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            "PlayResX: 1080",
            "PlayResY: 1920",
            "WrapStyle: 1",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            (
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
                "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
                "MarginL, MarginR, MarginV, Encoding"
            ),
            style_line,
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
    )

    events: list[str] = []
    for seg in segments:
        clean_phrase = sanitize_ass_text(seg.text)
        if not clean_phrase:
            continue
        seg_start = max(0.0, float(seg.start))
        seg_end = max(seg_start + 0.08, float(seg.end))

        if caption_style == "bold_pop" and seg.words:
            clean_words = [
                CaptionWord(
                    start=max(seg_start, float(w.start)),
                    end=max(seg_start + 0.04, min(seg_end, float(w.end))),
                    word=sanitize_ass_text(w.word),
                )
                for w in seg.words
                if sanitize_ass_text(w.word)
            ]
            if clean_words:
                for idx, active in enumerate(clean_words):
                    w_start = seg_start if idx == 0 else max(seg_start, active.start)
                    if idx + 1 < len(clean_words):
                        w_end = max(w_start + 0.03, clean_words[idx + 1].start)
                    else:
                        w_end = max(w_start + 0.03, seg_end)
                    rendered_tokens: list[str] = []
                    for pos, tok in enumerate(clean_words):
                        if pos == idx:
                            rendered_tokens.append(f"{{\\c&H0024A8F5&\\b1}}{tok.word}{{\\r}}")
                        else:
                            rendered_tokens.append(tok.word)
                    events.append(
                        "Dialogue: 0,"
                        f"{_format_ass_time(w_start)},{_format_ass_time(w_end)},"
                        f"Default,,0,0,0,,{pos_prefix}{' '.join(rendered_tokens)}"
                    )
                continue

        events.append(
            f"Dialogue: 0,{_format_ass_time(seg_start)},{_format_ass_time(seg_end)},"
            f"Default,,0,0,0,,{pos_prefix}{clean_phrase}"
        )

    if not events:
        return header + "\n"
    return header + "\n" + "\n".join(events) + "\n"


def build_srt_subtitles(segments: list[CaptionSegment]) -> str:
    """Build a standard UTF-8 SubRip (.srt) caption document."""
    blocks: list[str] = []
    index = 1
    for seg in segments:
        clean_text = sanitize_ass_text(seg.text)
        if not clean_text:
            continue
        seg_start = max(0.0, float(seg.start))
        seg_end = max(seg_start + 0.08, float(seg.end))
        blocks.append(
            f"{index}\n{_format_srt_time(seg_start)} --> {_format_srt_time(seg_end)}\n{clean_text}"
        )
        index += 1
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def build_title_overlay_ass(title_text: str) -> str:
    """Build a 1080x1920 ASS overlay stamping a bold title banner onto cover thumbnails."""
    clean_title = sanitize_ass_text(title_text)[:80]
    if not clean_title:
        clean_title = "STREAM CLIP"
    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            "PlayResX: 1080",
            "PlayResY: 1920",
            "WrapStyle: 1",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            (
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
                "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
                "MarginL, MarginR, MarginV, Encoding"
            ),
            (
                "Style: Banner,Arial,64,&H00F2F4F8,&H0024A8F5,&H000D0B08,&HD0080A0D,"
                "-1,0,0,0,100,100,0,0,3,14,0,8,72,72,96,1"
            ),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            f"Dialogue: 0,0:00:00.00,0:00:15.00,Banner,,0,0,0,,{clean_title}",
            "",
        ]
    )
