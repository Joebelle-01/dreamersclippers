"""Multi-signal real-time hype detection combining Twitch chat, audio RMS, and motion."""

from __future__ import annotations

import math
import re
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.security import sanitize_chat_text, validate_twitch_channel

_LEXICON_PHRASES: tuple[str, ...] = (
    "CLIP IT",
    "NO WAY",
)

_LEXICON_TOKENS: frozenset[str] = frozenset(
    {
        "KEKW",
        "LUL",
        "LULW",
        "OMEGALUL",
        "POG",
        "POGGERS",
        "CLIP",
        "WTF",
        "OMG",
        "INSANE",
        "CLUTCH",
        "W",
        "L",
    }
)

_ELONGATED_RE = re.compile(r"([a-zA-Z])\1{2,}")
_QUESTION_BURST_RE = re.compile(r"\?{3,}")
_EXCLAIM_BURST_RE = re.compile(r"!{3,}")
_TOKEN_SPLIT_RE = re.compile(r"[A-Za-z0-9_?!]+")

_QUIET_CHAT_MIN_RATE_PER_MIN = 3.0
_QUIET_CHAT_WEIGHTS = (0.20, 0.55, 0.25)
_EXTERNAL_BOOST_WINDOW_SEC = 15.0


class HypeEvaluation(BaseModel):
    """Structured output from a single HypeDetector evaluation tick."""

    chat_score: float
    audio_score: float
    motion_score: float
    fused_score: float
    top_tokens: list[str] = Field(default_factory=list)
    unique_chatters: int
    should_trigger: bool
    reason: str
    recent_samples: list[dict[str, Any]] = Field(default_factory=list)
    weights_mode: Literal["normal", "quiet_chat"] = "normal"
    baseline_rate_per_min: float = 0.0


@dataclass(slots=True)
class _ChatEvent:
    ts: float
    author_key: str
    author_display: str
    text: str
    matched_tokens: list[str]
    lexicon_hits: int
    is_all_caps: bool
    has_elongation: bool


@dataclass(slots=True)
class _ScalarSample:
    ts: float
    value: float


@dataclass(slots=True)
class _ExternalBoost:
    ts: float
    points: float
    label: str


def _analyze_chat_text(text: str) -> tuple[list[str], int, bool, bool]:
    """Extract Twitch hype lexicon hits, ALL-CAPS flag, and elongated letter flag."""
    upper_text = text.upper()
    matched: list[str] = []
    hits = 0

    for phrase in _LEXICON_PHRASES:
        if phrase in upper_text:
            matched.append(phrase)
            hits += 1

    if _QUESTION_BURST_RE.search(text):
        matched.append("???")
        hits += 1
    if _EXCLAIM_BURST_RE.search(text):
        matched.append("!!!")
        hits += 1

    for raw_tok in _TOKEN_SPLIT_RE.findall(text):
        tok_upper = raw_tok.upper().strip("?!")
        if tok_upper in _LEXICON_TOKENS:
            matched.append(tok_upper)
            hits += 1
        elif len(tok_upper) >= 3 and set(tok_upper) == {"W"}:
            matched.append("W")
            hits += 1
        elif len(tok_upper) >= 3 and set(tok_upper) == {"L"}:
            matched.append("L")
            hits += 1

    alpha_chars = [ch for ch in text if ch.isalpha()]
    is_all_caps = False
    if len(alpha_chars) >= 3:
        upper_count = sum(1 for ch in alpha_chars if ch.isupper())
        is_all_caps = (upper_count / len(alpha_chars)) >= 0.70

    has_elongation = bool(_ELONGATED_RE.search(text))
    if has_elongation and not matched:
        for raw_tok in _TOKEN_SPLIT_RE.findall(text):
            if _ELONGATED_RE.search(raw_tok):
                matched.append(raw_tok.upper()[:24])
                break

    return matched, hits, is_all_caps, has_elongation


def _mean_and_std(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return mean, math.sqrt(variance)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _z_to_score(z_value: float, *, offset: float = 0.3, scale: float = 2.0) -> float:
    if z_value <= offset:
        return 0.0
    raw = 100.0 * (1.0 - math.exp(-(z_value - offset) / scale))
    return round(max(0.0, min(100.0, raw)), 2)


class HypeDetector:
    """Rolling 60s baseline + burst detector across chat, audio dBFS, and motion."""

    def __init__(
        self,
        channel_login: str,
        threshold: float = 68.0,
        weight_chat: float = 0.5,
        weight_audio: float = 0.3,
        weight_motion: float = 0.2,
        cooldown_sec: float = 35.0,
        min_unique_chatters: int = 3,
    ) -> None:
        self.channel_login = validate_twitch_channel(channel_login)
        self.threshold = float(threshold)
        self.weight_chat = float(weight_chat)
        self.weight_audio = float(weight_audio)
        self.weight_motion = float(weight_motion)
        self.cooldown_sec = max(0.0, float(cooldown_sec))
        self.min_unique_chatters = max(1, int(min_unique_chatters))

        self._window_sec = 65.0
        self._chat_events: deque[_ChatEvent] = deque()
        self._audio_samples: deque[_ScalarSample] = deque()
        self._motion_samples: deque[_ScalarSample] = deque()
        self._external_boosts: deque[_ExternalBoost] = deque()

        self._latest_ts: float | None = None
        self._last_trigger_ts: float | None = None
        self._in_hype_window = False
        self._last_high_eval_ts: float | None = None

    def _note_ts(self, ts: float | None) -> float:
        resolved = float(ts if ts is not None else time.time())
        if self._latest_ts is None or resolved > self._latest_ts:
            self._latest_ts = resolved
        return resolved

    def _prune(self, ref_ts: float) -> None:
        cutoff = ref_ts - self._window_sec
        while self._chat_events and self._chat_events[0].ts < cutoff:
            self._chat_events.popleft()
        while self._audio_samples and self._audio_samples[0].ts < cutoff:
            self._audio_samples.popleft()
        while self._motion_samples and self._motion_samples[0].ts < cutoff:
            self._motion_samples.popleft()
        while self._external_boosts and self._external_boosts[0].ts < cutoff:
            self._external_boosts.popleft()

    def ingest_chat(self, author: str, text: str, ts: float | None = None) -> None:
        """Record one sanitized Twitch chat message."""
        clean_text = sanitize_chat_text(text, max_len=240)
        clean_author = sanitize_chat_text(author, max_len=64)
        if not clean_text or not clean_author:
            return
        event_ts = self._note_ts(ts)
        matched, hits, is_caps, has_elong = _analyze_chat_text(clean_text)
        self._chat_events.append(
            _ChatEvent(
                ts=event_ts,
                author_key=clean_author.strip().lower(),
                author_display=clean_author,
                text=clean_text,
                matched_tokens=matched,
                lexicon_hits=hits,
                is_all_caps=is_caps,
                has_elongation=has_elong,
            )
        )
        self._prune(self._latest_ts or event_ts)

    def ingest_audio_rms(self, db_level: float, ts: float | None = None) -> None:
        """Record an audio RMS measurement in dBFS."""
        if not math.isfinite(db_level):
            db_level = -90.0
        clamped = max(-90.0, min(0.0, float(db_level)))
        sample_ts = self._note_ts(ts)
        self._audio_samples.append(_ScalarSample(ts=sample_ts, value=clamped))
        self._prune(self._latest_ts or sample_ts)

    def ingest_motion(self, motion_score: float, ts: float | None = None) -> None:
        """Record a frame-to-frame visual motion magnitude score."""
        if not math.isfinite(motion_score):
            return
        clamped = max(0.0, float(motion_score))
        sample_ts = self._note_ts(ts)
        self._motion_samples.append(_ScalarSample(ts=sample_ts, value=clamped))
        self._prune(self._latest_ts or sample_ts)

    def ingest_external_boost(
        self, points: float, label: str, ts: float | None = None
    ) -> None:
        """Inject an external Signal #4 hype boost (clips, EventSub, viewer spike) with 15s decay."""
        if not math.isfinite(points) or points <= 0.0:
            return
        clean_label = sanitize_chat_text(label, max_len=48) or "External boost"
        boost_ts = self._note_ts(ts)
        self._external_boosts.append(
            _ExternalBoost(
                ts=boost_ts,
                points=max(0.0, min(100.0, float(points))),
                label=clean_label,
            )
        )
        self._prune(self._latest_ts or boost_ts)

    def _score_chat(
        self, now: float
    ) -> tuple[float, int, list[str], float, list[dict[str, Any]], bool]:
        """Score chat bursts using 2s buckets vs 60s baseline + lexicon + anti-spam gate."""
        burst_cutoff = now - 3.5
        recent_events = [ev for ev in self._chat_events if burst_cutoff <= ev.ts <= now]
        unique_chatters = len({ev.author_key for ev in recent_events})
        author_counts = Counter(ev.author_key for ev in recent_events)
        unique_hype_chatters = len(
            {
                ev.author_key
                for ev in recent_events
                if ev.lexicon_hits > 0 or ev.is_all_caps or ev.has_elongation
            }
        )

        token_counter: Counter[str] = Counter()
        for ev in recent_events:
            for tok in ev.matched_tokens:
                token_counter[tok] += 1
        top_tokens = [tok for tok, _ in token_counter.most_common(8)]

        recent_samples = [
            {"author": ev.author_display, "text": ev.text, "ts": round(ev.ts, 3)}
            for ev in recent_events[-12:]
        ]

        # 30 buckets of 2s across [now - 60.0, now]
        buckets = [0] * 30
        baseline_count = 0
        for ev in self._chat_events:
            age = now - ev.ts
            if 0.0 <= age <= 60.0:
                idx = min(29, max(0, int((60.0 - age) // 2.0)))
                buckets[idx] += 1
                if age > 2.0:
                    baseline_count += 1

        baseline_rate_per_min = baseline_count * (60.0 / 58.0)
        baseline_buckets = [float(b) for b in buckets[:-2]]  # first 56s (28 x 2s buckets)
        current_2s = float(buckets[-1])
        prev_2s = float(buckets[-2])
        active_bucket = max(current_2s, 0.85 * prev_2s)

        if active_bucket <= 0.0 and not recent_events:
            return 0.0, 0, top_tokens, round(baseline_rate_per_min, 2), recent_samples, False

        mean_b, std_b = _mean_and_std(baseline_buckets)
        scale_b = max(std_b, 0.75, math.sqrt(max(mean_b, 0.25)))
        rate_z = max(0.0, (active_bucket - mean_b) / scale_b)
        rate_component = 65.0 * (1.0 - math.exp(-rate_z / 2.2))

        msg_count = max(1, len(recent_events))
        msgs_with_lex = sum(1 for ev in recent_events if ev.lexicon_hits > 0)
        total_hits = sum(ev.lexicon_hits for ev in recent_events)
        lex_density = min(1.0, (msgs_with_lex / msg_count) * 0.65 + (total_hits / (msg_count * 1.5)) * 0.35)
        caps_ratio = sum(1 for ev in recent_events if ev.is_all_caps) / msg_count
        elong_ratio = sum(1 for ev in recent_events if ev.has_elongation) / msg_count

        lex_component = 22.0 * lex_density + 8.0 * caps_ratio + 5.0 * elong_ratio
        raw_score = min(100.0, rate_component + lex_component)

        max_author_msgs = max(author_counts.values(), default=0)
        spam_dominated = (
            len(recent_events) >= 5
            and (max_author_msgs / len(recent_events)) > 0.60
            and unique_hype_chatters < self.min_unique_chatters
        )
        if unique_chatters < self.min_unique_chatters or spam_dominated:
            effective_authors = unique_hype_chatters if spam_dominated else unique_chatters
            gate_cap = 15.0 * (effective_authors / self.min_unique_chatters)
            return (
                round(min(raw_score, gate_cap), 2),
                unique_chatters,
                top_tokens,
                round(baseline_rate_per_min, 2),
                recent_samples,
                True,
            )

        return (
            round(raw_score, 2),
            unique_chatters,
            top_tokens,
            round(baseline_rate_per_min, 2),
            recent_samples,
            False,
        )

    def _score_audio(self, now: float) -> float:
        """Score audio dBFS spikes using 60s rolling median / MAD z-score."""
        recent = [s.value for s in self._audio_samples if (now - 2.5) <= s.ts <= now]
        if not recent:
            return 0.0
        baseline = [s.value for s in self._audio_samples if (now - 60.0) <= s.ts < (now - 2.5)]
        if len(baseline) < 2:
            baseline = [s.value for s in self._audio_samples if (now - 60.0) <= s.ts <= now]
        if len(baseline) < 2:
            return 0.0

        peak_recent = max(recent)
        med = _median(baseline)
        abs_devs = [abs(v - med) for v in baseline]
        mad = _median(abs_devs)
        sigma_mad = max(1.4826 * mad, 2.0)

        z_audio = max(0.0, (peak_recent - med) / sigma_mad)
        score = _z_to_score(z_audio, offset=0.3, scale=2.0)
        if peak_recent < -38.0:
            quiet_atten = max(0.0, (peak_recent + 55.0) / 17.0)
            score = round(score * quiet_atten, 2)
        return score

    def _score_motion(self, now: float) -> float:
        """Score motion bursts using 60s baseline mean/std z-score."""
        recent = [s.value for s in self._motion_samples if (now - 2.5) <= s.ts <= now]
        if not recent:
            return 0.0
        baseline = [s.value for s in self._motion_samples if (now - 60.0) <= s.ts < (now - 2.5)]
        if len(baseline) < 2:
            baseline = [s.value for s in self._motion_samples if (now - 60.0) <= s.ts <= now]
        if len(baseline) < 2:
            return 0.0

        peak_recent = max(recent)
        mean_m, std_m = _mean_and_std(baseline)
        sigma_floor = max(std_m, 0.15 * max(mean_m, 0.02), 1e-4)
        z_motion = max(0.0, (peak_recent - mean_m) / sigma_floor)
        return _z_to_score(z_motion, offset=0.3, scale=2.0)

    def _active_weights(
        self, baseline_rate_per_min: float
    ) -> tuple[tuple[float, float, float], Literal["normal", "quiet_chat"]]:
        if baseline_rate_per_min < _QUIET_CHAT_MIN_RATE_PER_MIN:
            return _QUIET_CHAT_WEIGHTS, "quiet_chat"
        total = self.weight_chat + self.weight_audio + self.weight_motion
        if total <= 0.0:
            return (0.50, 0.30, 0.20), "normal"
        return (
            self.weight_chat / total,
            self.weight_audio / total,
            self.weight_motion / total,
        ), "normal"

    def evaluate(self, now: float | None = None) -> HypeEvaluation:
        """Evaluate current multi-signal hype score and trigger state."""
        eval_ts = float(now if now is not None else time.time())
        self._prune(eval_ts)

        chat_score, unique_chatters, top_tokens, baseline_rate, recent_samples, chat_gated = (
            self._score_chat(eval_ts)
        )
        audio_score = self._score_audio(eval_ts)
        motion_score = self._score_motion(eval_ts)

        (w_chat, w_audio, w_motion), weights_mode = self._active_weights(baseline_rate)
        base_fused = w_chat * chat_score + w_audio * audio_score + w_motion * motion_score

        active_boosts: list[tuple[float, str]] = []
        for boost in self._external_boosts:
            age = eval_ts - boost.ts
            if 0.0 <= age < _EXTERNAL_BOOST_WINDOW_SEC:
                decayed = boost.points * (1.0 - (age / _EXTERNAL_BOOST_WINDOW_SEC))
                if decayed > 0.0:
                    active_boosts.append((decayed, boost.label))

        boost_points = sum(pts for pts, _ in active_boosts)
        boost_labels: list[str] = []
        seen_labels: set[str] = set()
        for _, lbl in active_boosts:
            if lbl not in seen_labels:
                seen_labels.add(lbl)
                boost_labels.append(lbl)

        for lbl in boost_labels:
            if lbl not in top_tokens:
                top_tokens.append(lbl)

        fused = round(max(0.0, min(100.0, base_fused + boost_points)), 2)

        # Hysteresis release if score dropped cleanly or evaluation skipped past an old active peak.
        hysteresis_floor = self.threshold * 0.75
        if fused < hysteresis_floor:
            self._in_hype_window = False
        elif self._last_high_eval_ts is not None and (eval_ts - self._last_high_eval_ts) > 8.0:
            self._in_hype_window = False

        should_trigger = False
        contributors: list[str] = []
        if chat_score >= 40.0 and unique_chatters >= self.min_unique_chatters:
            contributors.append("Chat")
        if audio_score >= 40.0:
            contributors.append("Audio")
        if motion_score >= 40.0:
            contributors.append("Motion")
        for lbl in boost_labels:
            contributors.append(lbl)
        contrib_label = " + ".join(contributors) if contributors else "Multi-signal"

        if fused >= self.threshold:
            self._last_high_eval_ts = eval_ts
            in_cooldown = (
                self._last_trigger_ts is not None
                and (eval_ts - self._last_trigger_ts) < self.cooldown_sec
            )
            if in_cooldown:
                last_trigger_ts = self._last_trigger_ts
                if last_trigger_ts is None:
                    raise RuntimeError("Cooldown state is missing its trigger timestamp")
                remaining = max(0.0, self.cooldown_sec - (eval_ts - last_trigger_ts))
                reason = f"Cooldown active ({remaining:.1f}s remaining)"
            elif self._in_hype_window:
                reason = "Sustained hype window (hysteresis lock)"
            else:
                should_trigger = True
                self._last_trigger_ts = eval_ts
                self._in_hype_window = True
                token_hint = f" ({', '.join(top_tokens[:3])})" if top_tokens else ""
                mode_hint = " [quiet-chat weights]" if weights_mode == "quiet_chat" else ""
                reason = f"{contrib_label} spike{token_hint}{mode_hint}"
        else:
            boost_suffix = f" [boost: {', '.join(boost_labels)}]" if boost_labels else ""
            if chat_gated and len(recent_samples) >= 3:
                reason = (
                    f"Chat spam gated ({unique_chatters}/{self.min_unique_chatters} unique chatters)"
                    f"{boost_suffix}"
                )
            else:
                reason = f"Below threshold ({fused:.1f} < {self.threshold:.1f}){boost_suffix}"

        return HypeEvaluation(
            chat_score=chat_score,
            audio_score=audio_score,
            motion_score=motion_score,
            fused_score=fused,
            top_tokens=top_tokens,
            unique_chatters=unique_chatters,
            should_trigger=should_trigger,
            reason=reason,
            recent_samples=recent_samples,
            weights_mode=weights_mode,
            baseline_rate_per_min=baseline_rate,
        )
