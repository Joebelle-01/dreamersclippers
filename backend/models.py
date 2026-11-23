"""SQLModel database entities and Pydantic API schemas."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, field_validator, model_validator
from pydantic import Field as PydanticField
from sqlmodel import Field, SQLModel

from backend.security import (
    CropBox,
    extract_twitch_channel_login,
    sanitize_chat_text,
    validate_crop_box,
)


def utc_now() -> datetime:
    """Return timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def _parse_json_or_none(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _parse_json_list(raw: str | None) -> list[dict[str, Any]]:
    parsed = _parse_json_or_none(raw)
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _parse_crop_json(raw: str | None) -> CropBox | None:
    parsed = _parse_json_or_none(raw)
    if isinstance(parsed, dict):
        try:
            return validate_crop_box(parsed)
        except ValueError:
            return None
    return None


class Channel(SQLModel, table=True):
    """Monitored Twitch channel and per-channel framing preset."""

    id: int | None = Field(default=None, primary_key=True)
    platform: str = Field(default="twitch")
    channel_login: str = Field(index=True, unique=True)
    display_name: str
    is_monitoring: bool = Field(default=False)
    hype_threshold: float = Field(default=68.0)
    pre_roll_sec: int = Field(default=60)
    post_roll_sec: int = Field(default=15)
    cooldown_sec: int = Field(default=35)
    weight_chat: float = Field(default=0.50)
    weight_audio: float = Field(default=0.30)
    weight_motion: float = Field(default=0.20)
    preset_layout_mode: str = Field(default="split_top_cam")
    preset_split_ratio: float = Field(default=0.35)
    preset_webcam_json: str | None = Field(default=None)
    preset_gameplay_json: str | None = Field(default=None)
    auto_monitor_online: bool = Field(default=False)
    broadcaster_id: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)


class ClipItem(SQLModel, table=True):
    """Captured raw clip and vertical export metadata."""

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    channel_id: int = Field(default=0, index=True, sa_column_kwargs={"nullable": True})
    channel_login: str = Field(index=True)
    source_type: str = Field(default="live")
    source_ref: str = Field(default="")
    title: str
    status: str = Field(default="pending_review", index=True)
    hype_score: float = Field(default=0.0)
    trigger_reason: str = Field(default="")
    trigger_chat_samples_json: str = Field(default="[]")
    hype_timeline_json: str = Field(default="[]")
    raw_video_path: str
    thumbnail_path: str | None = Field(default=None)
    export_video_path: str | None = Field(default=None)
    duration_sec: float = Field(default=0.0)
    trim_start_sec: float = Field(default=0.0)
    trim_end_sec: float = Field(default=0.0)
    layout_mode: str = Field(default="split_top_cam")
    split_ratio: float = Field(default=0.35)
    webcam_box_json: str | None = Field(default=None)
    gameplay_box_json: str | None = Field(default=None)
    webcam_confidence: float = Field(default=0.0)
    normalize_audio: bool = Field(default=True)
    captions_enabled: bool = Field(default=True)
    caption_style: str = Field(default="bold_pop")
    caption_position: str = Field(default="seam")
    captions_json: str = Field(default="[]")
    export_srt_path: str | None = Field(default=None)
    export_thumb_path: str | None = Field(default=None)
    thumb_time_sec: float | None = Field(default=None)
    thumb_title_overlay: bool = Field(default=False)
    encoder_used: str | None = Field(default=None)
    error_message: str | None = Field(default=None)
    captured_at: datetime = Field(default_factory=utc_now, index=True)
    exported_at: datetime | None = Field(default=None)


class HypeEventLog(SQLModel, table=True):
    """Persisted multi-signal hype telemetry sample."""

    id: int | None = Field(default=None, primary_key=True)
    channel_id: int = Field(index=True)
    timestamp: datetime = Field(default_factory=utc_now, index=True)
    chat_score: float = Field(default=0.0)
    audio_score: float = Field(default=0.0)
    motion_score: float = Field(default=0.0)
    fused_score: float = Field(default=0.0)
    note: str = Field(default="")


LayoutMode = Literal["split_top_cam", "blur_no_cam"]
ClipStatus = Literal["pending_review", "rendering", "exported", "discarded", "failed"]
CaptionStyle = Literal["bold_pop", "clean_box", "classic_cc"]
CaptionPosition = Literal["seam", "lower_safe"]


class CaptionWord(BaseModel):
    """Single word-level caption timing relative to trimmed clip start (seconds)."""

    start: float = PydanticField(ge=0.0)
    end: float = PydanticField(ge=0.0)
    word: str


class CaptionSegment(BaseModel):
    """Grouped short-form caption phrase with optional word-level timings."""

    start: float = PydanticField(ge=0.0)
    end: float = PydanticField(ge=0.0)
    text: str
    words: list[CaptionWord] = PydanticField(default_factory=list)


class ThumbnailRequest(BaseModel):
    """Request payload to capture a 9:16 cover thumbnail at a specific timestamp."""

    at_sec: float | None = PydanticField(default=None, ge=0.0)
    title_overlay: bool | None = None
    title_text: str | None = None


class CaptionGenerateRequest(BaseModel):
    """Request payload for closed-caption generation with optional streamer-only voice filtering."""

    streamer_only: bool = True
    sensitivity: float = PydanticField(default=0.50, ge=0.15, le=0.85)


class ChannelCreate(BaseModel):
    """Payload to create a Twitch channel monitor configuration."""

    channel_login: str
    display_name: str | None = None
    hype_threshold: float = PydanticField(default=68.0, ge=10.0, le=100.0)
    pre_roll_sec: int = PydanticField(default=60, ge=5, le=180)
    post_roll_sec: int = PydanticField(default=15, ge=5, le=120)
    cooldown_sec: int = PydanticField(default=35, ge=5, le=300)
    weight_chat: float = PydanticField(default=0.50, ge=0.0, le=1.0)
    weight_audio: float = PydanticField(default=0.30, ge=0.0, le=1.0)
    weight_motion: float = PydanticField(default=0.20, ge=0.0, le=1.0)
    preset_layout_mode: LayoutMode = "split_top_cam"
    preset_split_ratio: float = PydanticField(default=0.35, ge=0.25, le=0.55)
    auto_monitor_online: bool = False
    broadcaster_id: str | None = None

    @field_validator("channel_login")
    @classmethod
    def _validate_login(cls, value: str) -> str:
        return extract_twitch_channel_login(value)


class ChannelUpdate(BaseModel):
    """Partial update payload for a monitored Twitch channel."""

    display_name: str | None = None
    hype_threshold: float | None = PydanticField(default=None, ge=10.0, le=100.0)
    pre_roll_sec: int | None = PydanticField(default=None, ge=5, le=180)
    post_roll_sec: int | None = PydanticField(default=None, ge=5, le=120)
    cooldown_sec: int | None = PydanticField(default=None, ge=5, le=300)
    weight_chat: float | None = PydanticField(default=None, ge=0.0, le=1.0)
    weight_audio: float | None = PydanticField(default=None, ge=0.0, le=1.0)
    weight_motion: float | None = PydanticField(default=None, ge=0.0, le=1.0)
    preset_layout_mode: LayoutMode | None = None
    preset_split_ratio: float | None = PydanticField(default=None, ge=0.25, le=0.55)
    preset_webcam: CropBox | dict[str, Any] | None = None
    preset_gameplay: CropBox | dict[str, Any] | None = None
    preset_webcam_json: str | None = None
    preset_gameplay_json: str | None = None
    auto_monitor_online: bool | None = None
    broadcaster_id: str | None = None


class ChannelRead(BaseModel):
    """API representation of a Channel."""

    id: int
    platform: str
    channel_login: str
    display_name: str
    is_monitoring: bool
    hype_threshold: float
    pre_roll_sec: int
    post_roll_sec: int
    cooldown_sec: int
    weight_chat: float
    weight_audio: float
    weight_motion: float
    preset_layout_mode: str
    preset_split_ratio: float
    preset_webcam_json: str | None
    preset_gameplay_json: str | None
    preset_webcam: CropBox | None = None
    preset_gameplay: CropBox | None = None
    auto_monitor_online: bool = False
    broadcaster_id: str | None = None
    created_at: datetime

    @classmethod
    def from_orm_row(cls, row: Channel) -> ChannelRead:
        """Extract primitives inside session scope to avoid DetachedInstanceError."""
        if row.id is None:
            raise ValueError("Channel row must be persisted before conversion")
        return cls(
            id=int(row.id),
            platform=str(row.platform),
            channel_login=str(row.channel_login),
            display_name=str(row.display_name),
            is_monitoring=bool(row.is_monitoring),
            hype_threshold=float(row.hype_threshold),
            pre_roll_sec=int(row.pre_roll_sec),
            post_roll_sec=int(row.post_roll_sec),
            cooldown_sec=int(row.cooldown_sec),
            weight_chat=float(row.weight_chat),
            weight_audio=float(row.weight_audio),
            weight_motion=float(row.weight_motion),
            preset_layout_mode=str(row.preset_layout_mode),
            preset_split_ratio=float(row.preset_split_ratio),
            preset_webcam_json=row.preset_webcam_json,
            preset_gameplay_json=row.preset_gameplay_json,
            preset_webcam=_parse_crop_json(row.preset_webcam_json),
            preset_gameplay=_parse_crop_json(row.preset_gameplay_json),
            auto_monitor_online=bool(row.auto_monitor_online),
            broadcaster_id=row.broadcaster_id,
            created_at=row.created_at,
        )


class ClipUpdate(BaseModel):
    """Partial update payload for clip trim, layout, captions, thumbnail, and crop boxes."""

    title: str | None = None
    status: ClipStatus | None = None
    trim_start_sec: float | None = PydanticField(default=None, ge=0.0)
    trim_end_sec: float | None = PydanticField(default=None, ge=0.0)
    layout_mode: LayoutMode | None = None
    split_ratio: float | None = PydanticField(default=None, ge=0.25, le=0.55)
    normalize_audio: bool | None = None
    captions_enabled: bool | None = None
    caption_style: CaptionStyle | None = None
    caption_position: CaptionPosition | None = None
    captions: list[CaptionSegment] | None = None
    captions_json: str | None = None
    thumb_time_sec: float | None = PydanticField(default=None, ge=0.0)
    thumb_title_overlay: bool | None = None
    webcam_box: CropBox | dict[str, Any] | None = None
    gameplay_box: CropBox | dict[str, Any] | None = None
    webcam_box_json: str | None = None
    gameplay_box_json: str | None = None


class ClipRead(BaseModel):
    """API representation of a ClipItem."""

    id: str
    channel_id: int
    channel_login: str
    source_type: str
    source_ref: str
    title: str
    status: str
    hype_score: float
    trigger_reason: str
    trigger_chat_samples_json: str
    hype_timeline_json: str
    trigger_chat_samples: list[dict[str, Any]] = PydanticField(default_factory=list)
    hype_timeline: list[dict[str, Any]] = PydanticField(default_factory=list)
    raw_video_path: str
    thumbnail_path: str | None
    export_video_path: str | None
    duration_sec: float
    trim_start_sec: float
    trim_end_sec: float
    layout_mode: str
    split_ratio: float
    webcam_box_json: str | None
    gameplay_box_json: str | None
    webcam_box: CropBox | None = None
    gameplay_box: CropBox | None = None
    webcam_confidence: float
    normalize_audio: bool
    captions_enabled: bool = True
    caption_style: str = "bold_pop"
    caption_position: str = "seam"
    captions_json: str = "[]"
    captions: list[dict[str, Any]] = PydanticField(default_factory=list)
    export_srt_path: str | None = None
    export_thumb_path: str | None = None
    thumb_time_sec: float | None = None
    thumb_title_overlay: bool = False
    encoder_used: str | None
    error_message: str | None
    captured_at: datetime
    exported_at: datetime | None

    @classmethod
    def from_orm_row(cls, row: ClipItem) -> ClipRead:
        """Extract primitives inside session scope to avoid DetachedInstanceError."""
        return cls(
            id=str(row.id),
            channel_id=int(row.channel_id or 0),
            channel_login=str(row.channel_login),
            source_type=str(row.source_type),
            source_ref=str(row.source_ref),
            title=str(row.title),
            status=str(row.status),
            hype_score=float(row.hype_score),
            trigger_reason=str(row.trigger_reason),
            trigger_chat_samples_json=str(row.trigger_chat_samples_json),
            hype_timeline_json=str(row.hype_timeline_json),
            trigger_chat_samples=_parse_json_list(row.trigger_chat_samples_json),
            hype_timeline=_parse_json_list(row.hype_timeline_json),
            raw_video_path=str(row.raw_video_path),
            thumbnail_path=row.thumbnail_path,
            export_video_path=row.export_video_path,
            duration_sec=float(row.duration_sec),
            trim_start_sec=float(row.trim_start_sec),
            trim_end_sec=float(row.trim_end_sec),
            layout_mode=str(row.layout_mode),
            split_ratio=float(row.split_ratio),
            webcam_box_json=row.webcam_box_json,
            gameplay_box_json=row.gameplay_box_json,
            webcam_box=_parse_crop_json(row.webcam_box_json),
            gameplay_box=_parse_crop_json(row.gameplay_box_json),
            webcam_confidence=float(row.webcam_confidence),
            normalize_audio=bool(row.normalize_audio),
            captions_enabled=bool(row.captions_enabled),
            caption_style=str(row.caption_style or "bold_pop"),
            caption_position=str(row.caption_position or "seam"),
            captions_json=str(row.captions_json or "[]"),
            captions=_parse_json_list(row.captions_json),
            export_srt_path=row.export_srt_path,
            export_thumb_path=row.export_thumb_path,
            thumb_time_sec=row.thumb_time_sec,
            thumb_title_overlay=bool(row.thumb_title_overlay),
            encoder_used=row.encoder_used,
            error_message=row.error_message,
            captured_at=row.captured_at,
            exported_at=row.exported_at,
        )


class VodScanRequest(BaseModel):
    """Request payload to queue a Twitch VOD scan."""

    vod_url: str
    channel_id: int | None = None
    max_clips: int = PydanticField(default=5, ge=1, le=20)
    hype_threshold: float = PydanticField(default=65.0, ge=10.0, le=100.0)


class SystemStatusResponse(BaseModel):
    """Runtime hardware, binary, and queue counters."""

    ffmpeg_path: str
    ffprobe_path: str
    nvenc_available: bool
    gpu_name: str | None
    active_monitors: int
    pending_clips: int
    exported_clips: int
    data_dir: str


class TwitchConfigRequest(BaseModel):
    """Credentials payload for Twitch Helix app authentication."""

    client_id: str = PydanticField(min_length=1, max_length=128)
    client_secret: str = PydanticField(min_length=1, max_length=256)

    @field_validator("client_id", "client_secret")
    @classmethod
    def _clean_credential(cls, value: str) -> str:
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("Twitch credential contains control characters")
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Twitch credential must not be blank")
        return cleaned


class TwitchClipImportRequest(BaseModel):
    """Request payload to import a Twitch clip into the review queue."""

    clip_url: str
    broadcaster_login: str | None = None
    prefer_portrait: bool = False


class PublishTwitchClipRequest(BaseModel):
    """Request payload to create a clip on Twitch (live or from a VOD offset)."""

    title: str | None = None
    duration: float | None = PydanticField(default=None, ge=5.0, le=60.0)
    vod_id: str | None = None
    vod_offset: int | None = PydanticField(default=None, ge=0)

    @field_validator("title")
    @classmethod
    def _sanitize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = sanitize_chat_text(value, max_len=100)
        return cleaned or None

    @field_validator("vod_id")
    @classmethod
    def _validate_vod_id(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        cleaned = value.strip()
        if not cleaned.isdigit() or len(cleaned) > 20:
            raise ValueError("vod_id must be a numeric Twitch VOD identifier")
        return cleaned


class StreamMarkerRequest(BaseModel):
    """Request payload to place a Twitch stream marker on a live broadcast."""

    description: str | None = PydanticField(default=None, max_length=140)

    @field_validator("description")
    @classmethod
    def _sanitize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = sanitize_chat_text(value, max_len=140)
        return cleaned or None


class ClipNowRequest(BaseModel):
    """Optional duration override payload when manually triggering Clip Now."""

    duration_sec: int | None = PydanticField(default=None, ge=5, le=180)


class VideoAnalyzeUrlRequest(BaseModel):
    """Request payload to analyze a recorded Twitch VOD or YouTube video for viral clips."""

    video_url: str
    channel_id: int | None = None
    max_clips: int = PydanticField(default=5, ge=1, le=20)
    min_clip_sec: int = PydanticField(default=20, ge=8, le=120)
    max_clip_sec: int = PydanticField(default=55, ge=15, le=180)
    viral_threshold: float = PydanticField(default=50.0, ge=10.0, le=100.0)
    use_speech_analysis: bool = True

    @model_validator(mode="after")
    def _validate_duration_range(self) -> VideoAnalyzeUrlRequest:
        if self.max_clip_sec < self.min_clip_sec + 5:
            raise ValueError("max_clip_sec must be at least 5 seconds greater than min_clip_sec")
        return self
