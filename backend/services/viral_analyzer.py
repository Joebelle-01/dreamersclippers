"""Full-video non-live viral moment analyzer and multi-signal highlight extractor."""

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
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import yt_dlp

from backend.config import Settings, resolve_ffmpeg_path, resolve_ffprobe_path
from backend.models import CaptionSegment, CaptionWord, ClipItem, ClipRead
from backend.security import (
    build_video_folder_slug,
    safe_resolve_under,
    sanitize_chat_text,
    validate_crop_box,
    validate_video_url,
)
from backend.services.caption_service import (
    generate_hook_headline,
    sanitize_ass_text,
    slice_captions_for_window,
    transcribe_clip,
    transcribe_window_words,
)
from backend.services.hype_detector import HypeDetector
from backend.services.video_renderer import generate_thumbnail, probe_video_duration
from backend.services.vod_scanner import (
    _cut_vod_segment_sync,
    _emit_event,
    _resolve_channel_context,
    _scan_audio_timeline_sync,
    _scan_motion_timeline_sync,
    extract_non_overlapping_peaks,
)
from backend.services.webcam_detector import detect_webcam

logger = logging.getLogger(__name__)

_TWITCH_VOD_ID_RE = re.compile(r"/videos/(\d+)$")

_HOOK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\?"),
    re.compile(r"\b(wait|stop|listen|look|watch this|hold on)\b", re.IGNORECASE),
    re.compile(r"\b(why|how|secret|never|mistake|worst|best|truth)\b", re.IGNORECASE),
    re.compile(r"\b(imagine|no way|what the|oh my god|did you|plot twist)\b", re.IGNORECASE),
    re.compile(r"\b(bro|chat|here'?s|you won'?t|nobody tells)\b", re.IGNORECASE),
)

_EMOTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"!"),
    re.compile(r"\?"),
    re.compile(
        r"\b(haha+|lmao+|lol+|insane|crazy|clutch|cooked|dead|unreal|wild|destroyed|genius|impossible|omg|wtf|what)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(actually|literally|but|until|suddenly|turns out)\b",
        re.IGNORECASE,
    ),
)

_LONG_VIDEO_THRESHOLD_SEC = 1800.0


def _clamp(val: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(val)))


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolation quantile over a non-empty sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    idx = _clamp(q, 0.0, 1.0) * (len(sorted_vals) - 1)
    lo = int(math.floor(idx))
    hi = min(len(sorted_vals) - 1, lo + 1)
    frac = idx - lo
    return float(sorted_vals[lo]) * (1.0 - frac) + float(sorted_vals[hi]) * frac


def _build_per_second_signals(
    duration_sec: float,
    audio_samples: list[tuple[float, float]],
    motion_samples: list[tuple[float, float]],
    chat_samples: list[tuple[float, str, str]] | None = None,
) -> tuple[list[float], list[float], list[float], list[float]]:
    """Convert raw audio dBFS, motion, and optional chat into 0-100 per-second arrays."""
    total_secs = max(1, int(math.ceil(max(1.0, duration_sec))))

    raw_db: list[float] = [-55.0] * total_secs
    if audio_samples:
        buckets: list[list[float]] = [[] for _ in range(total_secs)]
        for ts, db in audio_samples:
            sec_i = max(0, min(total_secs - 1, int(math.floor(float(ts)))))
            buckets[sec_i].append(float(db))
        last_db = float(audio_samples[0][1])
        for i in range(total_secs):
            if buckets[i]:
                last_db = max(buckets[i])
            raw_db[i] = last_db

    sorted_db = sorted(raw_db)
    db_med = _percentile(sorted_db, 0.50)
    db_p90 = max(db_med + 4.0, _percentile(sorted_db, 0.90))

    raw_motion: list[float] = [0.0] * total_secs
    if motion_samples:
        m_buckets: list[list[float]] = [[] for _ in range(total_secs)]
        for ts, mag in motion_samples:
            sec_i = max(0, min(total_secs - 1, int(math.floor(float(ts)))))
            m_buckets[sec_i].append(float(mag))
        last_mag = 0.0
        for i in range(total_secs):
            if m_buckets[i]:
                last_mag = max(m_buckets[i])
            raw_motion[i] = last_mag

    sorted_motion = sorted(raw_motion)
    mot_med = _percentile(sorted_motion, 0.50)
    mot_p90 = max(mot_med + 4.0, _percentile(sorted_motion, 0.90))

    energy_arr: list[float] = [0.0] * total_secs
    motion_arr: list[float] = [0.0] * total_secs
    scene_spike_arr: list[float] = [0.0] * total_secs

    for i in range(total_secs):
        db = raw_db[i]
        rel_audio = _clamp(((db - db_med) / (db_p90 - db_med)) * 75.0)
        abs_audio = _clamp(((db + 55.0) / 45.0) * 100.0)
        win_start = max(0, i - 6)
        local_med = _percentile(sorted(raw_db[win_start : i + 1]), 0.50)
        jump_db = max(0.0, db - local_med)
        burst_audio = _clamp(jump_db * 4.5)
        # Gate silence strictly so near-silent (-55 dBFS) streams do not fake high relative spikes
        if db <= -52.0:
            energy_arr[i] = round(abs_audio * 0.25, 2)
        else:
            energy_arr[i] = round(_clamp(0.45 * rel_audio + 0.30 * burst_audio + 0.25 * abs_audio), 2)

        mag = raw_motion[i]
        rel_mot = _clamp(((mag - mot_med) / (mot_p90 - mot_med)) * 75.0)
        abs_mot = _clamp(mag * 4.0)
        prev_mag = raw_motion[i - 1] if i > 0 else mag
        spike_mot = _clamp(max(0.0, mag - prev_mag) * 6.5)
        motion_arr[i] = round(_clamp(0.60 * max(rel_mot, abs_mot) + 0.40 * abs_mot), 2)
        scene_spike_arr[i] = round(spike_mot, 2)

    chat_arr: list[float] = [0.0] * total_secs
    if chat_samples:
        ordered_chat = sorted(chat_samples, key=lambda item: float(item[0]))
        detector = HypeDetector(channel_login="analyzer", threshold=50.0, cooldown_sec=0.0)
        c_idx = 0
        for sec in range(total_secs):
            t_now = float(sec + 1)
            while c_idx < len(ordered_chat) and float(ordered_chat[c_idx][0]) <= t_now:
                c_ts, c_author, c_text = ordered_chat[c_idx]
                detector.ingest_chat(c_author, c_text, ts=float(c_ts))
                c_idx += 1
            chat_arr[sec] = round(detector.evaluate(now=t_now).chat_score, 2)

    return energy_arr, motion_arr, scene_spike_arr, chat_arr


def _slice_per_second(arr: list[float], start_sec: float, end_sec: float) -> list[float]:
    if not arr:
        return [0.0]
    lo = max(0, min(len(arr) - 1, int(math.floor(start_sec))))
    hi = max(lo + 1, min(len(arr), int(math.ceil(end_sec))))
    return arr[lo:hi]


def build_viral_title(hook_text: str, fallback_title: str, rank: int) -> str:
    """Create a concise, capitalized hook-driven title for a viral clip."""
    clean_hook = sanitize_chat_text(hook_text, max_len=140).strip(" ,;:-")
    if len(clean_hook) >= 8:
        words = clean_hook.split()
        snippet_words: list[str] = []
        char_count = 0
        for word in words[:9]:
            next_len = len(word) if not snippet_words else char_count + 1 + len(word)
            if next_len > 64:
                break
            snippet_words.append(word)
            char_count = next_len
        snippet = " ".join(snippet_words).strip(" ,;:-")
        if len(snippet) >= 8:
            titled = snippet[0].upper() + snippet[1:]
            return sanitize_chat_text(titled, max_len=68)

    clean_fallback = sanitize_chat_text(fallback_title, max_len=50) or "Analyzed Video"
    return sanitize_chat_text(f"{clean_fallback} — Viral #{rank}", max_len=68)


def _score_hook(
    hook_text: str,
    hook_word_count: int,
    open_energy_vals: list[float],
) -> float:
    score = 0.0
    if hook_text:
        pattern_hits = 0
        for pat in _HOOK_PATTERNS:
            if pat.search(hook_text):
                pattern_hits += 1
        if "?" in hook_text:
            score += 18.0
        if "!" in hook_text:
            score += 10.0
        score += min(52.0, pattern_hits * 18.0)

    open_peak = max(open_energy_vals) if open_energy_vals else 0.0
    score += 0.30 * open_peak

    # Penalize dead-air openings where neither speech nor audio energy starts right away
    if hook_word_count == 0 and open_peak < 18.0:
        score -= 22.0
    elif hook_word_count >= 3:
        score += 12.0

    return round(_clamp(score), 2)


def _score_emotion(
    window_text: str,
    segs_in_win: list[dict[str, Any]],
    win_start: float,
    win_dur: float,
) -> float:
    if not window_text:
        return 0.0

    score = 0.0
    excl_count = window_text.count("!")
    quest_count = window_text.count("?")
    score += min(28.0, excl_count * 12.0)
    score += min(20.0, quest_count * 10.0)

    matches = 0
    for pat in _EMOTION_PATTERNS[2:]:
        matches += len(pat.findall(window_text))
    score += min(44.0, matches * 11.0)

    # Extra weight when payoff / reaction lands in the latter half of the clip
    midpoint = win_start + win_dur * 0.45
    payoff_hit = False
    for seg in segs_in_win:
        if float(seg.get("start", 0.0)) >= midpoint:
            seg_text = str(seg.get("text") or "")
            if any(pat.search(seg_text) for pat in _EMOTION_PATTERNS):
                payoff_hit = True
                break
    if payoff_hit:
        score += 18.0

    return round(_clamp(score), 2)


def _score_pacing(
    words_in_win: list[Any],
    segs_in_win: list[dict[str, Any]],
    win_start: float,
    win_end: float,
) -> float:
    if not words_in_win:
        return 0.0

    win_dur = max(1.0, win_end - win_start)
    wps = len(words_in_win) / win_dur

    if 2.0 <= wps <= 4.3:
        rate_score = 78.0
    elif 1.4 <= wps < 2.0:
        rate_score = 45.0 + ((wps - 1.4) / 0.6) * 33.0
    elif 4.3 < wps <= 5.5:
        rate_score = 78.0 - ((wps - 4.3) / 1.2) * 20.0
    elif wps < 1.4:
        rate_score = max(15.0, wps / 1.4 * 45.0)
    else:
        rate_score = 50.0

    sorted_words = sorted(words_in_win, key=lambda w: float(getattr(w, "start", 0.0)))
    max_gap = 0.0
    prev_end = win_start
    for w in sorted_words:
        w_start = float(getattr(w, "start", 0.0))
        w_end = float(getattr(w, "end", w_start))
        max_gap = max(max_gap, w_start - prev_end)
        prev_end = max(prev_end, w_end)
    max_gap = max(max_gap, win_end - prev_end)

    gap_penalty = min(35.0, max(0.0, max_gap - 2.5) * 12.0)

    cadence_bonus = 0.0
    if len(segs_in_win) >= 3:
        seg_rates = []
        for seg in segs_in_win:
            s_dur = max(0.4, float(seg.get("end", 0.0)) - float(seg.get("start", 0.0)))
            s_words = len(seg.get("words") or str(seg.get("text") or "").split())
            seg_rates.append(s_words / s_dur)
        if max(seg_rates) - min(seg_rates) >= 0.8:
            cadence_bonus = 18.0
        else:
            cadence_bonus = 8.0

    return round(_clamp(rate_score - gap_penalty + cadence_bonus), 2)


def _generate_candidate_windows(
    duration_sec: float,
    speech_segments: list[dict[str, Any]],
    energy_arr: list[float],
    motion_arr: list[float],
    chat_arr: list[float],
    min_clip_sec: float,
    max_clip_sec: float,
    max_clips: int,
) -> list[tuple[float, float, int, int]]:
    """Build candidate (start_sec, end_sec, seg_lo, seg_hi_exclusive) windows."""
    if speech_segments:
        ordered = sorted(speech_segments, key=lambda s: float(s.get("start", 0.0)))
        seen: set[tuple[int, int]] = set()
        windows: list[tuple[float, float, int, int]] = []

        for i in range(len(ordered)):
            win_start = max(0.0, float(ordered[i]["start"]) - 0.35)
            for j in range(i, len(ordered)):
                win_end = min(duration_sec, float(ordered[j]["end"]) + 0.35)
                span = win_end - win_start
                if span > max_clip_sec:
                    break
                if span >= min_clip_sec:
                    key = (round(win_start * 10), round(win_end * 10))
                    if key not in seen:
                        seen.add(key)
                        windows.append((win_start, win_end, i, j + 1))

        # Short-video fallback if the clip itself or merged sentences fall under min_clip_sec
        if not windows:
            win_start = max(0.0, float(ordered[0]["start"]) - 0.35)
            win_end = min(duration_sec, float(ordered[-1]["end"]) + 0.35)
            if win_end - win_start < 2.0:
                win_start = 0.0
                win_end = min(duration_sec, max_clip_sec)
            windows.append((win_start, win_end, 0, len(ordered)))

        return windows

    # Non-speech fallback: combine energy, motion, and optional chat into peaks
    has_chat = any(v > 0.0 for v in chat_arr)
    combined_timeline: list[dict[str, float]] = []
    for idx in range(len(energy_arr)):
        fused = (
            min(100.0, 0.25 * chat_arr[idx] + 0.45 * energy_arr[idx] + 0.30 * motion_arr[idx])
            if has_chat
            else min(100.0, 0.58 * energy_arr[idx] + 0.42 * motion_arr[idx])
        )
        combined_timeline.append(
            {
                "t": float(idx + 1),
                "chat": chat_arr[idx],
                "audio": energy_arr[idx],
                "motion": motion_arr[idx],
                "fused": round(fused, 2),
            }
        )

    target_dur = min(duration_sec, max(min_clip_sec, (min_clip_sec + max_clip_sec) * 0.5))
    peaks = extract_non_overlapping_peaks(
        combined_timeline,
        threshold=15.0,
        max_clips=max(max_clips * 3, 10),
        min_separation_sec=max(8.0, min_clip_sec * 0.65),
    )

    non_speech_windows: list[tuple[float, float, int, int]] = []
    for pt in peaks:
        t_peak = float(pt["t"])
        w_start = max(0.0, t_peak - 0.55 * target_dur)
        w_end = min(duration_sec, w_start + target_dur)
        w_start = max(0.0, w_end - target_dur)
        non_speech_windows.append((w_start, w_end, -1, -1))

    if not non_speech_windows and duration_sec > 0.0:
        w_end = min(duration_sec, target_dur)
        non_speech_windows.append((0.0, w_end, -1, -1))

    return non_speech_windows


def _build_reason_summary(
    viral_score: float,
    hook_score: float,
    energy_score: float,
    emotion_score: float,
    pacing_score: float,
    motion_score: float,
) -> str:
    factors = [
        ("Strong hook", hook_score),
        ("High energy", energy_score),
        ("Reaction peak", emotion_score),
        ("Fast pacing", pacing_score),
        ("Visual burst", motion_score),
    ]
    factors.sort(key=lambda item: item[1], reverse=True)
    top_labels = [name for name, val in factors if val >= 55.0][:3]
    if not top_labels:
        top_labels = [name for name, _val in factors[:2]]
    return f"Viral {round(viral_score)}/100 · {' + '.join(top_labels)}"[:120]


def score_viral_candidates(
    duration_sec: float,
    audio_samples: list[tuple[float, float]],
    motion_samples: list[tuple[float, float]],
    speech_segments: list[dict[str, Any]],
    chat_samples: list[tuple[float, str, str]] | None = None,
    *,
    min_clip_sec: float = 20.0,
    max_clip_sec: float = 55.0,
    max_clips: int = 5,
    viral_threshold: float = 50.0,
    fallback_title: str = "Analyzed Video",
) -> list[dict[str, Any]]:
    """Score non-overlapping viral moment windows across full-video multi-modal signals."""
    if duration_sec <= 0.0 or max_clips <= 0:
        return []

    eff_min = max(4.0, float(min_clip_sec))
    eff_max = max(eff_min + 2.0, float(max_clip_sec))
    energy_arr, motion_arr, scene_spike_arr, chat_arr = _build_per_second_signals(
        duration_sec,
        audio_samples,
        motion_samples,
        chat_samples,
    )
    ordered_segs = (
        sorted(speech_segments, key=lambda s: float(s.get("start", 0.0)))
        if speech_segments
        else []
    )
    windows = _generate_candidate_windows(
        duration_sec,
        ordered_segs,
        energy_arr,
        motion_arr,
        chat_arr,
        eff_min,
        eff_max,
        int(max_clips),
    )

    scored: list[dict[str, Any]] = []
    has_chat = any(v > 0.0 for v in chat_arr)

    for win_start, win_end, seg_lo, seg_hi in windows:
        win_dur = max(1.0, win_end - win_start)
        win_energy = _slice_per_second(energy_arr, win_start, win_end)
        win_motion = _slice_per_second(motion_arr, win_start, win_end)
        win_spikes = _slice_per_second(scene_spike_arr, win_start, win_end)
        win_chat = _slice_per_second(chat_arr, win_start, win_end)

        peak_e = max(win_energy)
        sorted_e = sorted(win_energy)
        med_e = _percentile(sorted_e, 0.40)
        p90_e = _percentile(sorted_e, 0.90)
        jump_contrast = _clamp((peak_e - med_e) * 1.4)
        energy_score = round(_clamp(0.55 * peak_e + 0.25 * p90_e + 0.20 * jump_contrast), 2)

        peak_m = max(win_motion)
        peak_spike = max(win_spikes)
        base_motion = _clamp(0.65 * peak_m + 0.35 * peak_spike)
        max_c = max(win_chat)
        motion_score = round(
            _clamp(0.70 * base_motion + 0.30 * max_c) if has_chat else base_motion,
            2,
        )

        if seg_lo >= 0 and seg_hi > seg_lo:
            segs_in_win = ordered_segs[seg_lo:seg_hi]
            words_in_win: list[CaptionWord] = []
            for seg in segs_in_win:
                raw_words = seg.get("words")
                if raw_words:
                    words_in_win.extend(raw_words)
                else:
                    parts = str(seg.get("text") or "").split()
                    if parts:
                        s0 = float(seg.get("start", win_start))
                        s1 = max(s0 + 0.1, float(seg.get("end", s0 + 0.5)))
                        step = (s1 - s0) / len(parts)
                        for p_i, token in enumerate(parts):
                            words_in_win.append(
                                CaptionWord(
                                    start=round(s0 + p_i * step, 3),
                                    end=round(s0 + (p_i + 1) * step, 3),
                                    word=token,
                                )
                            )
            window_text = " ".join(str(seg.get("text") or "") for seg in segs_in_win).strip()

            hook_cutoff = win_start + 4.5
            hook_words = [
                str(getattr(w, "word", ""))
                for w in words_in_win
                if float(getattr(w, "start", 0.0)) <= hook_cutoff
            ]
            if not hook_words and segs_in_win:
                hook_text = str(segs_in_win[0].get("text") or "")
            else:
                hook_text = " ".join(hook_words)

            open_energy = _slice_per_second(energy_arr, win_start, min(win_end, win_start + 3.0))
            hook_score = _score_hook(hook_text, len(hook_words), open_energy)
            emotion_score = _score_emotion(window_text, segs_in_win, win_start, win_dur)
            pacing_score = _score_pacing(words_in_win, segs_in_win, win_start, win_end)

            completeness = 0.0
            last_text = str(segs_in_win[-1].get("text") or "").strip()
            if last_text.endswith((".", "!", "?")):
                completeness += 2.5
            if 25.0 <= win_dur <= 48.0:
                completeness += 2.5

            viral_score = round(
                _clamp(
                    0.28 * hook_score
                    + 0.24 * energy_score
                    + 0.22 * emotion_score
                    + 0.14 * pacing_score
                    + 0.12 * motion_score
                    + completeness
                ),
                2,
            )
        else:
            hook_text = ""
            hook_score = 0.0
            emotion_score = 0.0
            pacing_score = 0.0
            viral_score = round(
                _clamp(
                    0.25 * max_c + 0.45 * energy_score + 0.30 * _clamp(base_motion)
                    if has_chat
                    else 0.58 * energy_score + 0.42 * motion_score
                ),
                2,
            )

        if viral_score < float(viral_threshold):
            continue

        scored.append(
            {
                "start": round(win_start, 3),
                "end": round(win_end, 3),
                "viral_score": viral_score,
                "hook_score": hook_score,
                "energy_score": energy_score,
                "emotion_score": emotion_score,
                "pacing_score": pacing_score,
                "motion_score": motion_score,
                "hook_text": hook_text,
                "reason": _build_reason_summary(
                    viral_score,
                    hook_score,
                    energy_score,
                    emotion_score,
                    pacing_score,
                    motion_score,
                ),
            }
        )

    scored.sort(key=lambda item: float(item["viral_score"]), reverse=True)

    selected: list[dict[str, Any]] = []
    for cand in scored:
        a0, a1 = float(cand["start"]), float(cand["end"])
        overlaps = False
        for picked in selected:
            b0, b1 = float(picked["start"]), float(picked["end"])
            overlap = max(0.0, min(a1, b1) - max(a0, b0))
            min_len = max(0.1, min(a1 - a0, b1 - b0))
            if (overlap / min_len) >= 0.15:
                overlaps = True
                break
        if overlaps:
            continue
        selected.append(cand)
        if len(selected) >= int(max_clips):
            break

    # Attach local timeline and ranked viral titles in descending viral_score order
    lo_idx_base = 0
    for rank, item in enumerate(selected, start=1):
        w_start = float(item["start"])
        w_end = float(item["end"])
        item["title"] = build_viral_title(str(item.pop("hook_text", "")), fallback_title, rank)
        lo_s = max(lo_idx_base, int(math.floor(w_start)))
        hi_s = min(len(energy_arr) - 1, int(math.ceil(w_end)) - 1)
        local_tl: list[dict[str, float]] = []
        for sec_idx in range(max(0, lo_s), max(0, hi_s + 1)):
            c_v = chat_arr[sec_idx]
            a_v = energy_arr[sec_idx]
            m_v = motion_arr[sec_idx]
            local_fused = round(min(100.0, 0.55 * a_v + 0.30 * m_v + 0.15 * c_v), 2)
            local_tl.append(
                {
                    "t": round(float(sec_idx + 1) - w_start, 2),
                    "chat": c_v,
                    "audio": a_v,
                    "motion": m_v,
                    "fused": local_fused,
                }
            )
        item["timeline"] = local_tl

    return selected


def _select_shortlist_zones(
    duration_sec: float,
    energy_arr: list[float],
    motion_arr: list[float],
    chat_arr: list[float],
    max_clips: int,
) -> list[tuple[float, float]]:
    """Shortlist merged 90s zones for long (>30m) videos so Whisper stays fast."""
    timeline = [
        {
            "t": float(idx + 1),
            "fused": round(
                min(100.0, 0.55 * energy_arr[idx] + 0.30 * motion_arr[idx] + 0.15 * chat_arr[idx]),
                2,
            ),
        }
        for idx in range(len(energy_arr))
    ]
    peaks = extract_non_overlapping_peaks(
        timeline,
        threshold=10.0,
        max_clips=max(6, int(max_clips) * 3),
        min_separation_sec=45.0,
    )
    zones: list[tuple[float, float]] = []
    for pt in peaks:
        t_center = float(pt["t"])
        zones.append((max(0.0, t_center - 45.0), min(duration_sec, t_center + 45.0)))

    # Add evenly spaced fallback windows so calm talking moments are still sampled
    sample_count = min(6, max(2, int(duration_sec // 600)))
    step = duration_sec / (sample_count + 1)
    for k in range(1, sample_count + 1):
        center = step * k
        zones.append((max(0.0, center - 45.0), min(duration_sec, center + 45.0)))

    zones.sort(key=lambda z: z[0])
    merged: list[tuple[float, float]] = []
    for start, end in zones:
        if end - start < 1.0:
            continue
        if not merged or start > merged[-1][1] + 5.0:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


async def analyze_local_video(
    video_path: Path,
    source_label: str,
    *,
    channel_id: int | None = None,
    source_type: str = "video_upload",
    source_ref: str = "",
    max_clips: int = 5,
    min_clip_sec: float = 20.0,
    max_clip_sec: float = 55.0,
    viral_threshold: float = 50.0,
    use_speech_analysis: bool = True,
    info: dict[str, Any] | None = None,
    chat_samples: list[tuple[float, str, str]] | None = None,
    settings: Settings,
    session_factory: Callable[[], Any],
    broadcast_fn: Callable[..., Any] | None = None,
    job_id: str | None = None,
    cleanup_dir: Path | None = None,
) -> dict[str, Any]:
    """Analyze a local video file end-to-end, score viral moments, and save ClipItems."""
    active_job_id = job_id or str(uuid4())
    ffmpeg_path = settings.ffmpeg_path or resolve_ffmpeg_path()
    ffprobe_path = settings.ffprobe_path or resolve_ffprobe_path(ffmpeg_path)
    if not ffmpeg_path or not ffprobe_path:
        raise RuntimeError("FFmpeg and ffprobe executables must be available for video analysis")

    settings.ensure_data_dirs()
    fallback_title = sanitize_chat_text(
        str((info or {}).get("title") or Path(source_label).stem or "Analyzed Video"),
        max_len=80,
    )

    try:
        duration_sec = await asyncio.to_thread(probe_video_duration, video_path, ffprobe_path)
        if duration_sec <= 0.0 and info:
            duration_sec = float(info.get("duration") or 0.0)
        if duration_sec <= 0.0:
            raise RuntimeError("Video file has zero duration or could not be probed")

        channel_ctx = _resolve_channel_context(session_factory, channel_id, info or {}, settings)
        channel_login = channel_ctx["channel_login"]
        if channel_id is None and source_type == "video_upload" and channel_login == "twitch_vod":
            channel_login = "local_video"

        await _emit_event(
            broadcast_fn,
            "vod_progress",
            {
                "job_id": active_job_id,
                "vod_url": source_label,
                "stage": "analyzing_signals",
                "progress": 30,
                "status": "running",
                "message": "Scanning whole-video audio loudness and visual motion",
            },
        )

        audio_samples, motion_samples = await asyncio.gather(
            asyncio.to_thread(_scan_audio_timeline_sync, video_path, ffmpeg_path),
            asyncio.to_thread(_scan_motion_timeline_sync, video_path, ffmpeg_path),
        )

        all_words = []
        speech_segments: list[dict[str, Any]] = []
        if use_speech_analysis:
            await _emit_event(
                broadcast_fn,
                "vod_progress",
                {
                    "job_id": active_job_id,
                    "vod_url": source_label,
                    "stage": "analyzing_speech",
                    "progress": 55,
                    "status": "running",
                    "message": "Transcribing speech to detect viral hooks and punchlines",
                },
            )
            if duration_sec <= _LONG_VIDEO_THRESHOLD_SEC:
                words, segs = await asyncio.to_thread(
                    transcribe_window_words,
                    video_path,
                    0.0,
                    duration_sec,
                    ffmpeg_path,
                    settings.whisper_model,
                )
                all_words.extend(words)
                speech_segments.extend(segs)
            else:
                e_arr, m_arr, _sp, c_arr = _build_per_second_signals(
                    duration_sec,
                    audio_samples,
                    motion_samples,
                    chat_samples,
                )
                zones = _select_shortlist_zones(duration_sec, e_arr, m_arr, c_arr, max_clips)
                for z_start, z_end in zones:
                    words, segs = await asyncio.to_thread(
                        transcribe_window_words,
                        video_path,
                        z_start,
                        z_end,
                        ffmpeg_path,
                        settings.whisper_model,
                    )
                    all_words.extend(words)
                    speech_segments.extend(segs)

        await _emit_event(
            broadcast_fn,
            "vod_progress",
            {
                "job_id": active_job_id,
                "vod_url": source_label,
                "stage": "scoring",
                "progress": 70,
                "status": "running",
                "message": "Ranking highest-potential viral clips",
            },
        )

        candidates = score_viral_candidates(
            duration_sec,
            audio_samples,
            motion_samples,
            speech_segments,
            chat_samples,
            min_clip_sec=min_clip_sec,
            max_clip_sec=max_clip_sec,
            max_clips=max_clips,
            viral_threshold=viral_threshold,
            fallback_title=fallback_title,
        )

        total_cands = max(1, len(candidates))
        await _emit_event(
            broadcast_fn,
            "vod_progress",
            {
                "job_id": active_job_id,
                "vod_url": source_label,
                "stage": "extracting",
                "progress": 80,
                "status": "running",
                "clips_found": len(candidates),
                "message": f"Cutting {len(candidates)} viral moment(s)",
            },
        )

        folder_slug = build_video_folder_slug(source_type, source_ref or source_label, channel_login)
        clip_folder_dir = safe_resolve_under(settings.clips_dir, folder_slug)
        clip_folder_dir.mkdir(parents=True, exist_ok=True)

        created_ids: list[str] = []
        for idx, cand in enumerate(candidates, start=1):
            clip_start = max(0.0, float(cand["start"]))
            clip_end = min(duration_sec, float(cand["end"]))
            if clip_end - clip_start < 1.5:
                continue

            clip_id = str(uuid4())
            raw_clip_path = safe_resolve_under(clip_folder_dir, f"{clip_id}.mp4")
            await asyncio.to_thread(
                _cut_vod_segment_sync,
                video_path,
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

            clip_captions = slice_captions_for_window(all_words, clip_start, clip_end)
            captions_payload = [seg.model_dump() for seg in clip_captions]

            local_chat = [
                {"author": author, "text": text, "ts": round(ts - clip_start, 2)}
                for ts, author, text in (chat_samples or [])
                if clip_start <= ts <= clip_end
            ][:15]

            cand_title = str(cand["title"])
            default_hook = generate_hook_headline(
                channel_login,
                cand_title,
                clip_captions,
                mode="context",
            )
            clip_row = ClipItem(
                id=clip_id,
                channel_id=int(channel_ctx["channel_id"]),
                channel_login=channel_login,
                source_type=source_type,
                source_ref=source_ref or source_label,
                title=cand_title,
                status="pending_review",
                hype_score=float(cand["viral_score"]),
                trigger_reason=str(cand["reason"]),
                trigger_chat_samples_json=json.dumps(local_chat),
                hype_timeline_json=json.dumps(cand.get("timeline") or []),
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
                captions_enabled=True,
                caption_position="lower_safe",
                captions_json=json.dumps(captions_payload),
                hook_enabled=True,
                hook_text=default_hook,
                hook_style="tiktok_white",
                hook_duration_mode="full",
            )

            with session_factory() as session:
                session.add(clip_row)
                session.commit()
                session.refresh(clip_row)
                clip_payload = ClipRead.from_orm_row(clip_row).model_dump(mode="json")

            created_ids.append(clip_id)
            await _emit_event(broadcast_fn, "clip_created", clip_payload)

            pct = min(95, 80 + round((idx / total_cands) * 15))
            await _emit_event(
                broadcast_fn,
                "vod_progress",
                {
                    "job_id": active_job_id,
                    "vod_url": source_label,
                    "stage": "extracting",
                    "progress": pct,
                    "status": "running",
                    "clips_found": len(created_ids),
                    "message": f"Queued viral clip {idx} of {total_cands}",
                },
            )

        await _emit_event(
            broadcast_fn,
            "vod_progress",
            {
                "job_id": active_job_id,
                "vod_url": source_label,
                "stage": "complete",
                "progress": 100,
                "status": "completed",
                "clips_found": len(created_ids),
                "message": f"Analysis complete: {len(created_ids)} viral clip(s) queued",
            },
        )

        return {
            "job_id": active_job_id,
            "source": source_label,
            "source_type": source_type,
            "used_speech_analysis": bool(use_speech_analysis and speech_segments),
            "clips_created": created_ids,
        }
    finally:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


_TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
_DEFAULT_TWITCH_WEB_GQL_ID = "".join(("kimne78kx3", "ncx6brgo4m", "v6wki5h1ko"))
_VTT_TIME_RE = re.compile(
    r"(?:(\d{1,2}):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d{1,2}):)?(\d{2}):(\d{2})[.,](\d{3})"
)
_VTT_TAG_RE = re.compile(r"<[^>]+>")
_CHAPTER_HYPE_RE = re.compile(
    r"\b(highlight|clutch|win|boss|final|insane|crazy|funny|best|fail|fight|reaction|climax)\b",
    re.IGNORECASE,
)


def _is_safe_remote_media_url(url: str) -> bool:
    """Verify a subtitle or stream URL uses HTTPS and has no embedded credentials."""
    if not isinstance(url, str) or not url.strip():
        return False
    parsed = urlsplit(url.strip())
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        return False
    return bool(parsed.netloc)


def _extract_remote_video_info_sync(video_url: str, ffmpeg_path: str) -> dict[str, Any]:
    """Fetch remote video metadata, heatmap, caption tracks, and stream URLs with download=False."""
    ydl_opts: dict[str, Any] = {
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = str(Path(ffmpeg_path).parent)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        if not isinstance(info, dict):
            raise RuntimeError("yt-dlp did not return remote video metadata")
        return info


def _parse_youtube_json3_captions(
    payload: dict[str, Any],
) -> tuple[list[CaptionWord], list[dict[str, Any]]]:
    """Parse YouTube json3 timed captions into word tokens and sentence segments."""
    events = payload.get("events")
    if not isinstance(events, list):
        return [], []

    collected_words: list[CaptionWord] = []
    speech_segments: list[dict[str, Any]] = []

    for ev in events:
        if not isinstance(ev, dict):
            continue
        segs = ev.get("segs")
        if not isinstance(segs, list) or not segs:
            continue
        t_start_ms = float(ev.get("tStartMs") or 0.0)
        d_dur_ms = float(ev.get("dDurationMs") or 2000.0)
        ev_start = max(0.0, t_start_ms / 1000.0)
        ev_end = max(ev_start + 0.2, (t_start_ms + d_dur_ms) / 1000.0)

        cue_words: list[CaptionWord] = []
        raw_tokens: list[tuple[float, str]] = []
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            utf8 = str(seg.get("utf8") or "").strip()
            if not utf8:
                continue
            offset_ms = float(seg.get("tOffsetMs") or 0.0)
            token_start = max(0.0, (t_start_ms + offset_ms) / 1000.0)
            for part in utf8.split():
                clean_part = sanitize_ass_text(part)
                if clean_part:
                    raw_tokens.append((token_start, clean_part))

        if not raw_tokens:
            continue

        for idx, (w_start, token) in enumerate(raw_tokens):
            if idx + 1 < len(raw_tokens) and raw_tokens[idx + 1][0] > w_start + 0.04:
                w_end = min(ev_end, raw_tokens[idx + 1][0])
            else:
                step = max(0.08, (ev_end - w_start) / max(1, len(raw_tokens) - idx))
                w_end = w_start + min(0.6, step)
            cw = CaptionWord(start=round(w_start, 3), end=round(max(w_start + 0.04, w_end), 3), word=token)
            collected_words.append(cw)
            cue_words.append(cw)

        if cue_words:
            seg_text = " ".join(w.word for w in cue_words)
            speech_segments.append(
                {
                    "start": cue_words[0].start,
                    "end": cue_words[-1].end,
                    "text": seg_text,
                    "words": cue_words,
                }
            )

    return collected_words, speech_segments


def _vtt_hms_to_sec(h: str | None, m: str, s: str, ms: str) -> float:
    hours = int(h) if h else 0
    return hours * 3600.0 + int(m) * 60.0 + int(s) + int(ms) / 1000.0


def _parse_webvtt_captions(vtt_text: str) -> tuple[list[CaptionWord], list[dict[str, Any]]]:
    """Parse WebVTT subtitle cues into word tokens and sentence segments."""
    if not vtt_text:
        return [], []

    collected_words: list[CaptionWord] = []
    speech_segments: list[dict[str, Any]] = []
    lines = vtt_text.splitlines()
    i = 0
    while i < len(lines):
        match = _VTT_TIME_RE.search(lines[i])
        if not match:
            i += 1
            continue
        c_start = _vtt_hms_to_sec(match.group(1), match.group(2), match.group(3), match.group(4))
        c_end = max(
            c_start + 0.15,
            _vtt_hms_to_sec(match.group(5), match.group(6), match.group(7), match.group(8)),
        )
        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            stripped = _VTT_TAG_RE.sub("", lines[i]).strip()
            if stripped:
                text_lines.append(stripped)
            i += 1
        cue_text = sanitize_ass_text(" ".join(text_lines))
        if not cue_text:
            continue
        # Deduplicate rolling WebVTT auto-caption lines that repeat previous text
        if speech_segments and speech_segments[-1]["text"] == cue_text:
            continue
        parts = cue_text.split()
        step = (c_end - c_start) / max(1, len(parts))
        cue_words: list[CaptionWord] = []
        for idx, part in enumerate(parts):
            w_s = round(c_start + idx * step, 3)
            w_e = round(max(w_s + 0.04, c_start + (idx + 1) * step), 3)
            cw = CaptionWord(start=w_s, end=w_e, word=part)
            collected_words.append(cw)
            cue_words.append(cw)
        if cue_words:
            speech_segments.append(
                {
                    "start": cue_words[0].start,
                    "end": cue_words[-1].end,
                    "text": cue_text,
                    "words": cue_words,
                }
            )
    return collected_words, speech_segments


def _pick_caption_track_url(info: dict[str, Any]) -> tuple[str, str] | None:
    """Select the best HTTPS json3 or vtt caption track from yt-dlp metadata."""
    for field in ("subtitles", "automatic_captions"):
        tracks_by_lang = info.get(field)
        if not isinstance(tracks_by_lang, dict) or not tracks_by_lang:
            continue
        preferred_langs = ["en", "en-orig", "en-US", "en-GB"]
        for k in tracks_by_lang:
            if str(k).lower().startswith("en") and k not in preferred_langs:
                preferred_langs.append(str(k))
        for k in tracks_by_lang:
            if k not in preferred_langs:
                preferred_langs.append(str(k))

        for lang in preferred_langs:
            track_list = tracks_by_lang.get(lang)
            if not isinstance(track_list, list):
                continue
            for wanted_ext in ("json3", "vtt"):
                for item in track_list:
                    if not isinstance(item, dict):
                        continue
                    ext = str(item.get("ext") or "").lower()
                    url = str(item.get("url") or "").strip()
                    if ext == wanted_ext and _is_safe_remote_media_url(url):
                        return url, ext
    return None


async def _fetch_remote_youtube_transcript(
    info: dict[str, Any],
) -> tuple[list[CaptionWord], list[dict[str, Any]]]:
    """Download lightweight YouTube timed captions (~300 KB) over HTTPS without downloading video."""
    picked = _pick_caption_track_url(info)
    if picked is None:
        return [], []
    url, ext = picked
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return [], []
            if ext == "json3":
                return _parse_youtube_json3_captions(resp.json())
            return _parse_webvtt_captions(resp.text)
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.info("Remote YouTube caption fetch skipped: %s", exc)
        return [], []


async def _fetch_twitch_gql_chat_sampled(
    vod_id: str,
    duration_sec: float,
) -> list[tuple[float, str, str]]:
    """Fetch Twitch VOD chat replay across the entire VOD duration (stride-sampling long streams)."""
    client_id = (
        os.environ.get("STREAMCLIPPERS_TWITCH_GQL_CLIENT_ID", "").strip()
        or _DEFAULT_TWITCH_WEB_GQL_ID
    )
    headers = {"Client-Id": client_id, "Content-Type": "application/json"}
    query = (
        "query($videoID: ID!, $cursor: Cursor, $offset: Int) { "
        "video(id: $videoID) { comments(after: $cursor, contentOffsetSeconds: $offset) { "
        "edges { cursor node { contentOffsetSeconds commenter { displayName login } "
        "message { fragments { text } } } } pageInfo { hasNextPage } } } }"
    )

    if duration_sec <= 1200.0:
        offsets = [0]
        pages_per_offset = 18
    else:
        # Sample 28 evenly spaced checkpoints across a multi-hour VOD so all 4+ hours are covered
        step = max(60, int(duration_sec // 28))
        offsets = [min(int(duration_sec - 10), k * step) for k in range(28)]
        pages_per_offset = 2

    seen: set[tuple[int, str, str]] = set()
    messages: list[tuple[float, str, str]] = []

    async def _fetch_from_offset(client: httpx.AsyncClient, start_offset: int) -> None:
        cursor: str | None = None
        for _page in range(pages_per_offset):
            variables: dict[str, Any] = {"videoID": vod_id}
            if cursor is not None:
                variables["cursor"] = cursor
            else:
                variables["offset"] = max(0, int(start_offset))
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
                text = sanitize_chat_text(
                    "".join(str(f.get("text") or "") for f in frags),
                    max_len=240,
                )
                if author and text:
                    dedup_key = (int(round(offset * 10)), author, text)
                    if dedup_key not in seen:
                        seen.add(dedup_key)
                        messages.append((offset, author, text))
                cursor = edge.get("cursor")
            if not (comments.get("pageInfo") or {}).get("hasNextPage") or not cursor:
                break

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for batch_start in range(0, len(offsets), 6):
                batch = offsets[batch_start : batch_start + 6]
                await asyncio.gather(*(_fetch_from_offset(client, off) for off in batch))
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.info("Skipping sampled Twitch GQL chat replay for VOD %s: %s", vod_id, exc)

    messages.sort(key=lambda item: item[0])
    return messages


def _synthesize_remote_signals(
    duration_sec: float,
    info: dict[str, Any],
    speech_segments: list[dict[str, Any]],
    chat_samples: list[tuple[float, str, str]],
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Build 1-second audio dBFS and motion timelines from remote heatmap, chapters, speech, and chat."""
    total_secs = max(1, int(math.ceil(duration_sec)))
    db_arr: list[float] = [-38.0] * total_secs
    mot_arr: list[float] = [5.0] * total_secs

    heatmap = info.get("heatmap")
    if isinstance(heatmap, list) and heatmap:
        for pt in heatmap:
            if not isinstance(pt, dict):
                continue
            s0 = max(0, int(math.floor(float(pt.get("start_time") or 0.0))))
            s1 = min(total_secs, max(s0 + 1, int(math.ceil(float(pt.get("end_time") or (s0 + 1))))))
            val = _clamp(float(pt.get("value") or 0.0), 0.0, 1.0)
            h_db = -40.0 + val * 32.0
            h_mot = 4.0 + val * 40.0
            for sec in range(s0, s1):
                db_arr[sec] = max(db_arr[sec], h_db)
                mot_arr[sec] = max(mot_arr[sec], h_mot)

    chapters = info.get("chapters")
    if isinstance(chapters, list) and chapters:
        for ch in chapters:
            if not isinstance(ch, dict):
                continue
            c_title = str(ch.get("title") or "")
            s0 = max(0, int(math.floor(float(ch.get("start_time") or 0.0))))
            s1 = min(total_secs, s0 + 45)
            boost_db = -14.0 if _CHAPTER_HYPE_RE.search(c_title) else -22.0
            boost_mot = 30.0 if _CHAPTER_HYPE_RE.search(c_title) else 18.0
            for sec in range(s0, s1):
                db_arr[sec] = max(db_arr[sec], boost_db)
                mot_arr[sec] = max(mot_arr[sec], boost_mot)

    for seg in speech_segments:
        text = str(seg.get("text") or "")
        if not text:
            continue
        s0 = max(0, min(total_secs - 1, int(math.floor(float(seg.get("start", 0.0))))))
        s1 = min(total_secs, max(s0 + 1, int(math.ceil(float(seg.get("end", s0 + 1))))))
        dur = max(0.5, float(seg.get("end", s0 + 1)) - float(seg.get("start", s0)))
        wps = len(text.split()) / dur
        hook_hits = sum(1 for pat in _HOOK_PATTERNS if pat.search(text))
        emo_hits = sum(1 for pat in _EMOTION_PATTERNS if pat.search(text))
        intensity = min(1.0, 0.22 * hook_hits + 0.22 * emo_hits + max(0.0, (wps - 1.8) * 0.16))
        if intensity > 0.05:
            s_db = -34.0 + intensity * 25.0
            s_mot = 8.0 + intensity * 32.0
            for sec in range(s0, s1):
                db_arr[sec] = max(db_arr[sec], s_db)
                mot_arr[sec] = max(mot_arr[sec], s_mot)

    if chat_samples:
        chat_counts: list[int] = [0] * total_secs
        for ts, _author, text in chat_samples:
            sec_i = max(0, min(total_secs - 1, int(math.floor(float(ts)))))
            weight = 2 if any(pat.search(text) for pat in _EMOTION_PATTERNS) else 1
            for k in range(max(0, sec_i - 2), min(total_secs, sec_i + 3)):
                chat_counts[k] += weight
        max_count = max(chat_counts) if chat_counts else 0
        if max_count > 0:
            for sec, cnt in enumerate(chat_counts):
                if cnt > 0:
                    ratio = min(1.0, cnt / max(3.0, max_count * 0.75))
                    db_arr[sec] = max(db_arr[sec], -35.0 + ratio * 26.0)
                    mot_arr[sec] = max(mot_arr[sec], 8.0 + ratio * 34.0)

    # If a remote video had no heatmap, captions,chapters, or chat, seed periodic highlight anchors
    if not heatmap and not speech_segments and not chat_samples and total_secs > 10:
        anchor_count = min(10, max(3, total_secs // 120))
        stride = total_secs / (anchor_count + 1)
        for k in range(1, anchor_count + 1):
            center = int(stride * k)
            for sec in range(max(0, center - 8), min(total_secs, center + 8)):
                db_arr[sec] = max(db_arr[sec], -12.0)
                mot_arr[sec] = max(mot_arr[sec], 34.0)

    audio_samples = [(sec + 0.5, round(db_arr[sec], 2)) for sec in range(total_secs)]
    motion_samples = [(sec + 0.5, round(mot_arr[sec], 2)) for sec in range(total_secs)]
    return audio_samples, motion_samples


def _resolve_direct_stream_urls(info: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract direct HTTPS video and audio stream URLs from yt-dlp metadata if available."""
    req_fmts = info.get("requested_formats")
    if isinstance(req_fmts, list) and len(req_fmts) >= 2:
        v_url = str(req_fmts[0].get("url") or "").strip()
        a_url = str(req_fmts[1].get("url") or "").strip()
        if _is_safe_remote_media_url(v_url) and _is_safe_remote_media_url(a_url):
            return v_url, a_url

    single_url = str(info.get("url") or "").strip()
    if _is_safe_remote_media_url(single_url):
        return single_url, None

    formats = info.get("formats")
    if isinstance(formats, list) and formats:
        combined = [
            f
            for f in formats
            if isinstance(f, dict)
            and f.get("vcodec") not in (None, "none")
            and f.get("acodec") not in (None, "none")
            and (f.get("height") or 0) <= 1080
            and _is_safe_remote_media_url(str(f.get("url") or ""))
        ]
        if combined:
            best = combined[-1]
            return str(best["url"]).strip(), None
    return None, None


def _download_clip_section_sync(
    canonical_url: str,
    info: dict[str, Any],
    clip_start: float,
    clip_end: float,
    out_path: Path,
    work_dir: Path,
    ffmpeg_path: str,
) -> Path:
    """Download ONLY [clip_start, clip_end] via FFmpeg HTTP Range/HLS seeking or yt-dlp download_ranges."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    start_sec = max(0.0, float(clip_start))
    duration_sec = max(1.5, float(clip_end) - start_sec)

    v_url, a_url = _resolve_direct_stream_urls(info)
    if v_url:
        ff_args = [
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start_sec:.3f}",
            "-t",
            f"{duration_sec:.3f}",
            "-i",
            v_url,
        ]
        if a_url:
            ff_args.extend(
                [
                    "-ss",
                    f"{start_sec:.3f}",
                    "-t",
                    f"{duration_sec:.3f}",
                    "-i",
                    a_url,
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                ]
            )
        ff_args.extend(
            [
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
            ]
        )
        proc = subprocess.run(
            ff_args,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        if proc.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 1024:
            return out_path

    # Tier 2 fallback: yt-dlp partial range download (downloads only fragments in [start_sec, clip_end])
    section_dir = work_dir / f"sec_{uuid4().hex[:8]}"
    section_dir.mkdir(parents=True, exist_ok=True)
    ydl_opts: dict[str, Any] = {
        "outtmpl": str(section_dir / "range.%(ext)s"),
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "download_ranges": yt_dlp.utils.download_range_func(None, [(start_sec, start_sec + duration_sec)]),
    }
    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = str(Path(ffmpeg_path).parent)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([canonical_url])
        produced = [
            *sorted(section_dir.glob("*.mp4")),
            *sorted(section_dir.glob("*.mkv")),
            *sorted(section_dir.glob("*.webm")),
            *sorted(section_dir.glob("*.ts")),
        ]
        for cand in produced:
            if cand.is_file() and cand.stat().st_size > 0:
                if cand.suffix.lower() == ".mp4":
                    shutil.move(str(cand), str(out_path))
                    return out_path
                return _cut_vod_segment_sync(cand, out_path, 0.0, duration_sec, ffmpeg_path)
    finally:
        shutil.rmtree(section_dir, ignore_errors=True)

    raise RuntimeError(f"Failed to download clip range [{start_sec:.1f}s..{clip_end:.1f}s]")


async def analyze_video_url(
    video_url: str,
    *,
    channel_id: int | None = None,
    max_clips: int = 5,
    min_clip_sec: float = 20.0,
    max_clip_sec: float = 55.0,
    viral_threshold: float = 50.0,
    use_speech_analysis: bool = True,
    settings: Settings,
    session_factory: Callable[[], Any],
    broadcast_fn: Callable[..., Any] | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Scan a YouTube or Twitch VOD URL remotely without full download and cut top clips."""
    canonical_url, platform = validate_video_url(video_url)
    active_job_id = job_id or str(uuid4())
    ffmpeg_path = settings.ffmpeg_path or resolve_ffmpeg_path()
    ffprobe_path = settings.ffprobe_path or resolve_ffprobe_path(ffmpeg_path)
    if not ffmpeg_path or not ffprobe_path:
        raise RuntimeError("FFmpeg and ffprobe executables must be available for video analysis")

    settings.ensure_data_dirs()
    work_dir = safe_resolve_under(settings.buffers_dir, f"analyze_{platform}_{uuid4().hex[:8]}")
    work_dir.mkdir(parents=True, exist_ok=True)
    source_type = "youtube_vod" if platform == "youtube" else "vod"

    try:
        await _emit_event(
            broadcast_fn,
            "vod_progress",
            {
                "job_id": active_job_id,
                "vod_url": canonical_url,
                "stage": "probing_remote",
                "progress": 12,
                "status": "running",
                "message": "Fetching remote video metadata, heatmap, and stream index (no full download)",
            },
        )

        info = await asyncio.to_thread(_extract_remote_video_info_sync, canonical_url, ffmpeg_path)
        duration_sec = float(info.get("duration") or 0.0)
        if duration_sec <= 0.0:
            raise RuntimeError("Remote video has zero duration or is an active unarchived live stream")

        channel_ctx = _resolve_channel_context(session_factory, channel_id, info, settings)
        channel_login = channel_ctx["channel_login"]
        fallback_title = sanitize_chat_text(
            str(info.get("title") or f"{platform.replace('_', ' ').title()} Video"),
            max_len=80,
        )

        await _emit_event(
            broadcast_fn,
            "vod_progress",
            {
                "job_id": active_job_id,
                "vod_url": canonical_url,
                "stage": "analyzing_remote",
                "progress": 35,
                "status": "running",
                "message": "Scanning online captions, replay heatmap, and chat spikes across full video",
            },
        )

        all_words: list[CaptionWord] = []
        speech_segments: list[dict[str, Any]] = []
        chat_samples: list[tuple[float, str, str]] = []

        if platform == "youtube":
            all_words, speech_segments = await _fetch_remote_youtube_transcript(info)
        elif platform == "twitch_vod":
            vod_match = _TWITCH_VOD_ID_RE.search(canonical_url)
            if vod_match:
                chat_samples = await _fetch_twitch_gql_chat_sampled(vod_match.group(1), duration_sec)

        audio_samples, motion_samples = _synthesize_remote_signals(
            duration_sec,
            info,
            speech_segments,
            chat_samples,
        )

        await _emit_event(
            broadcast_fn,
            "vod_progress",
            {
                "job_id": active_job_id,
                "vod_url": canonical_url,
                "stage": "scoring",
                "progress": 60,
                "status": "running",
                "message": "Ranking highest-potential viral moments across stream",
            },
        )

        candidates = score_viral_candidates(
            duration_sec,
            audio_samples,
            motion_samples,
            speech_segments if use_speech_analysis else [],
            chat_samples,
            min_clip_sec=min_clip_sec,
            max_clip_sec=max_clip_sec,
            max_clips=max_clips,
            viral_threshold=viral_threshold,
            fallback_title=fallback_title,
        )

        # Fallback if a video has no heatmap/chat/captions above threshold: pick best available windows
        if not candidates and duration_sec >= 5.0:
            candidates = score_viral_candidates(
                duration_sec,
                audio_samples,
                motion_samples,
                speech_segments if use_speech_analysis else [],
                chat_samples,
                min_clip_sec=min_clip_sec,
                max_clip_sec=max_clip_sec,
                max_clips=max_clips,
                viral_threshold=10.0,
                fallback_title=fallback_title,
            )

        total_cands = max(1, len(candidates))
        await _emit_event(
            broadcast_fn,
            "vod_progress",
            {
                "job_id": active_job_id,
                "vod_url": canonical_url,
                "stage": "extracting",
                "progress": 70,
                "status": "running",
                "clips_found": len(candidates),
                "message": f"Downloading only the {len(candidates)} short viral clip range(s)",
            },
        )

        folder_slug = build_video_folder_slug(source_type, canonical_url, channel_login)
        clip_folder_dir = safe_resolve_under(settings.clips_dir, folder_slug)
        clip_folder_dir.mkdir(parents=True, exist_ok=True)

        created_ids: list[str] = []
        for idx, cand in enumerate(candidates, start=1):
            clip_start = max(0.0, float(cand["start"]))
            clip_end = min(duration_sec, float(cand["end"]))
            if clip_end - clip_start < 1.5:
                continue

            clip_id = str(uuid4())
            raw_clip_path = safe_resolve_under(clip_folder_dir, f"{clip_id}.mp4")

            await asyncio.to_thread(
                _download_clip_section_sync,
                canonical_url,
                info,
                clip_start,
                clip_end,
                raw_clip_path,
                work_dir,
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

            clip_captions: list[CaptionSegment] = []
            if all_words:
                clip_captions = slice_captions_for_window(all_words, clip_start, clip_end)
            if not clip_captions and use_speech_analysis:
                try:
                    clip_captions = await asyncio.to_thread(
                        transcribe_clip,
                        raw_clip_path,
                        0.0,
                        clip_dur,
                        ffmpeg_path,
                        settings.whisper_model,
                    )
                except Exception as exc:
                    logger.warning("Per-clip Whisper captioning skipped for %s: %s", clip_id, exc)

            clip_title = str(cand["title"])
            if clip_captions and "— Viral #" in clip_title:
                first_hook = " ".join(seg.text for seg in clip_captions[:2])
                clip_title = build_viral_title(first_hook, fallback_title, idx)

            captions_payload = [seg.model_dump() for seg in clip_captions]
            local_chat = [
                {"author": author, "text": text, "ts": round(ts - clip_start, 2)}
                for ts, author, text in chat_samples
                if clip_start <= ts <= clip_end
            ][:15]

            default_hook = generate_hook_headline(
                channel_login,
                clip_title,
                clip_captions,
                mode="context",
            )
            clip_row = ClipItem(
                id=clip_id,
                channel_id=int(channel_ctx["channel_id"]),
                channel_login=channel_login,
                source_type=source_type,
                source_ref=canonical_url,
                title=clip_title,
                status="pending_review",
                hype_score=float(cand["viral_score"]),
                trigger_reason=str(cand["reason"]),
                trigger_chat_samples_json=json.dumps(local_chat),
                hype_timeline_json=json.dumps(cand.get("timeline") or []),
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
                captions_enabled=True,
                caption_position="lower_safe",
                captions_json=json.dumps(captions_payload),
                hook_enabled=True,
                hook_text=default_hook,
                hook_style="tiktok_white",
                hook_duration_mode="full",
            )

            with session_factory() as session:
                session.add(clip_row)
                session.commit()
                session.refresh(clip_row)
                clip_payload = ClipRead.from_orm_row(clip_row).model_dump(mode="json")

            created_ids.append(clip_id)
            await _emit_event(broadcast_fn, "clip_created", clip_payload)

            pct = min(96, 70 + round((idx / total_cands) * 26))
            await _emit_event(
                broadcast_fn,
                "vod_progress",
                {
                    "job_id": active_job_id,
                    "vod_url": canonical_url,
                    "stage": "extracting",
                    "progress": pct,
                    "status": "running",
                    "clips_found": len(created_ids),
                    "message": f"Downloaded viral clip {idx} of {total_cands}",
                },
            )

        await _emit_event(
            broadcast_fn,
            "vod_progress",
            {
                "job_id": active_job_id,
                "vod_url": canonical_url,
                "stage": "complete",
                "progress": 100,
                "status": "completed",
                "clips_found": len(created_ids),
                "message": f"Analysis complete: {len(created_ids)} viral clip(s) queued",
            },
        )

        return {
            "job_id": active_job_id,
            "source": canonical_url,
            "source_type": source_type,
            "remote_range_mode": True,
            "used_speech_analysis": bool(use_speech_analysis),
            "clips_created": created_ids,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
