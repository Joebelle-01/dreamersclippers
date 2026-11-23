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

import numpy as np

from backend.config import resolve_ffmpeg_path
from backend.models import CaptionSegment, CaptionWord

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
) -> bool:
    """Extract 16 kHz mono PCM WAV for Whisper; returns False if clip has no audio stream."""
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
            "1",
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


def _load_wav_float32(wav_path: Path) -> np.ndarray:
    """Load a 16 kHz mono s16le WAV file into a 1D float32 array in [-1.0, 1.0].

    Passing a float32 NumPy array directly to faster-whisper's WhisperModel.transcribe()
    bypasses faster_whisper.audio.decode_audio(), which calls av.open(..., metadata_errors="ignore")
    and fails on PyAV 19+ where metadata_errors was removed.
    """
    with wave.open(str(wav_path), "rb") as wf:
        frame_count = wf.getnframes()
        if frame_count <= 0:
            return np.zeros(0, dtype=np.float32)
        pcm_bytes = wf.readframes(frame_count)
    if not pcm_bytes:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0


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


def transcribe_clip(
    video_path: Path,
    trim_start_sec: float,
    trim_end_sec: float,
    ffmpeg_path: str,
    model_size: str = "base",
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
        )
        if not has_audio:
            return []

        collected_words, _segments = _transcribe_wav_words(wav_path, model_size, offset_sec=0.0)
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
