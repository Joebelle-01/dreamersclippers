"""Input validation, SSRF allowlisting, crop validation, and path confinement."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

_TWITCH_CHANNEL_RE = re.compile(r"^[a-zA-Z0-9_]{3,25}$")
_TWITCH_VOD_URL_RE = re.compile(r"^https://(www\.|m\.)?twitch\.tv/videos/(\d+)$")
_TWITCH_VOD_PATH_RE = re.compile(r"^/videos/(\d{1,20})/?$")
_TWITCH_CLIP_HOST_PATH_RE = re.compile(r"^/([a-zA-Z0-9_-]{3,120})$")
_TWITCH_CHANNEL_CLIP_PATH_RE = re.compile(r"^/([a-zA-Z0-9_]{3,25})/clip/([a-zA-Z0-9_-]{3,120})$")
_ALLOWED_TWITCH_HOSTS = frozenset({"twitch.tv", "www.twitch.tv", "m.twitch.tv"})
_ALLOWED_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ALLOWED_VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".webm", ".m4v"})
_RESERVED_TWITCH_PATHS = frozenset(
    {
        "videos",
        "directory",
        "downloads",
        "jobs",
        "p",
        "clip",
        "clips",
        "moderator",
        "settings",
        "subscriptions",
        "inventory",
        "drops",
        "wallet",
        "search",
        "turbo",
        "login",
        "signup",
        "popout",
        "embed",
        "u",
        "products",
        "store",
    }
)
_EPS = 1e-6


class CropBox(BaseModel):
    """Normalized [0, 1] bounding box for webcam and gameplay framing."""

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(ge=0.05, le=1.0)
    h: float = Field(ge=0.05, le=1.0)

    @field_validator("x", "y", "w", "h", mode="before")
    @classmethod
    def _validate_finite_float(cls, value: Any) -> float:
        if isinstance(value, bool):
            raise ValueError("Crop coordinate must be a finite float, not bool")
        num = float(value)
        if not math.isfinite(num):
            raise ValueError("Crop coordinate must be finite")
        if -_EPS <= num < 0.0:
            return 0.0
        if 1.0 < num <= 1.0 + _EPS:
            return 1.0
        return num

    @model_validator(mode="after")
    def _validate_bounds(self) -> CropBox:
        if self.x + self.w > 1.0 + _EPS:
            raise ValueError("CropBox x + w must not exceed 1.0")
        if self.y + self.h > 1.0 + _EPS:
            raise ValueError("CropBox y + h must not exceed 1.0")
        if self.x + self.w > 1.0:
            self.w = max(0.05, round(1.0 - self.x, 6))
        if self.y + self.h > 1.0:
            self.h = max(0.05, round(1.0 - self.y, 6))
        if self.x + self.w > 1.0 + 1e-9 or self.y + self.h > 1.0 + 1e-9:
            raise ValueError("CropBox exceeds normalized [0, 1] bounds")
        return self


def validate_twitch_channel(name: str) -> str:
    """Validate and normalize a Twitch channel login to lowercase."""
    if not isinstance(name, str):
        raise ValueError("Twitch channel name must be a string")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("Twitch channel name contains control characters")
    if not _TWITCH_CHANNEL_RE.fullmatch(name):
        raise ValueError(
            "Invalid Twitch channel name; expected 3-25 alphanumeric or underscore characters"
        )
    return name.lower()


def extract_twitch_channel_login(raw: str) -> str:
    """Extract and validate a Twitch channel login from a username, @handle, or live URL."""
    if not isinstance(raw, str):
        raise ValueError("Twitch channel input must be a string")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError("Twitch channel input contains control characters")

    cleaned = raw.strip()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        raise ValueError("Twitch channel input must not be empty or contain spaces")

    if cleaned.startswith("@") and "/" not in cleaned and "." not in cleaned:
        return validate_twitch_channel(cleaned[1:])

    lowered = cleaned.lower()
    if "://" in cleaned or lowered.startswith(("twitch.tv/", "www.twitch.tv/", "m.twitch.tv/")):
        candidate = cleaned if "://" in cleaned else f"https://{cleaned}"
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {"https", "http"}:
            raise ValueError("Only http(s):// Twitch channel URLs are allowed")
        if parsed.username is not None or parsed.password is not None or parsed.port is not None:
            raise ValueError("Twitch channel URL must not contain credentials or custom ports")
        host = parsed.netloc.lower()
        if host not in _ALLOWED_TWITCH_HOSTS:
            raise ValueError("Host must be twitch.tv, www.twitch.tv, or m.twitch.tv")

        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) != 1:
            raise ValueError(
                "Twitch live URL must point directly to https://www.twitch.tv/<channel>"
            )
        slug = path_parts[0].lower()
        if slug in _RESERVED_TWITCH_PATHS:
            raise ValueError(f"Path '/{slug}' is a Twitch page, not a streamer channel")
        return validate_twitch_channel(slug)

    return validate_twitch_channel(cleaned)


def validate_twitch_vod_url(url: str) -> str:
    """Strictly allowlist HTTPS twitch.tv/videos/<digits> URLs to block SSRF."""
    if not isinstance(url, str):
        raise ValueError("Twitch VOD URL must be a string")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ValueError("Twitch VOD URL contains control characters")
    cleaned = url.strip()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        raise ValueError("Twitch VOD URL must not be empty or contain whitespace")

    parsed = urlsplit(cleaned)
    if parsed.scheme != "https":
        raise ValueError("Only https:// Twitch VOD URLs are allowed")
    if parsed.username is not None or parsed.password is not None or parsed.port is not None:
        raise ValueError("Twitch VOD URL must not contain credentials or custom ports")

    host = parsed.netloc.lower()
    if host not in _ALLOWED_TWITCH_HOSTS:
        raise ValueError("Host must be twitch.tv, www.twitch.tv, or m.twitch.tv")

    path_match = _TWITCH_VOD_PATH_RE.fullmatch(parsed.path)
    if not path_match:
        raise ValueError("URL path must match /videos/<digits>")

    canonical = f"https://{host}/videos/{path_match.group(1)}"
    if not _TWITCH_VOD_URL_RE.fullmatch(canonical):
        raise ValueError("Invalid Twitch VOD URL")
    return canonical


def validate_twitch_clip_url(url: str) -> str:
    """Strictly allowlist HTTPS Twitch clip URLs and strip query/fragment/trailing slash."""
    if not isinstance(url, str):
        raise ValueError("Twitch clip URL must be a string")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ValueError("Twitch clip URL contains control characters")
    cleaned = url.strip()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        raise ValueError("Twitch clip URL must not be empty or contain whitespace")

    parsed = urlsplit(cleaned)
    if parsed.scheme != "https":
        raise ValueError("Only https:// Twitch clip URLs are allowed")
    if parsed.username is not None or parsed.password is not None or parsed.port is not None:
        raise ValueError("Twitch clip URL must not contain credentials or custom ports")

    host = parsed.netloc.lower()
    trimmed_path = parsed.path.rstrip("/")

    if host == "clips.twitch.tv":
        slug_match = _TWITCH_CLIP_HOST_PATH_RE.fullmatch(trimmed_path)
        if not slug_match:
            raise ValueError("URL path must match https://clips.twitch.tv/<slug>")
        slug = slug_match.group(1)
        if slug.lower() in {"embed", "create"}:
            raise ValueError("URL must reference a specific Twitch clip slug")
        return f"https://clips.twitch.tv/{slug}"

    if host in _ALLOWED_TWITCH_HOSTS:
        clip_match = _TWITCH_CHANNEL_CLIP_PATH_RE.fullmatch(trimmed_path)
        if not clip_match:
            raise ValueError("URL path must match https://twitch.tv/<login>/clip/<slug>")
        login = clip_match.group(1).lower()
        slug = clip_match.group(2)
        if login in _RESERVED_TWITCH_PATHS:
            raise ValueError(f"Path '/{login}/clip/...' is not a valid streamer channel clip")
        return f"https://{host}/{login}/clip/{slug}"

    raise ValueError("Host must be clips.twitch.tv, twitch.tv, www.twitch.tv, or m.twitch.tv")


def validate_video_url(url: str) -> tuple[str, str]:
    """Validate an HTTPS YouTube or Twitch VOD URL and return (canonical_url, platform)."""
    if not isinstance(url, str):
        raise ValueError("Video URL must be a string")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ValueError("Video URL contains control characters")
    cleaned = url.strip()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        raise ValueError("Video URL must not be empty or contain whitespace")

    parsed = urlsplit(cleaned)
    if parsed.scheme != "https":
        raise ValueError("Only https:// video URLs are allowed")
    if parsed.username is not None or parsed.password is not None or parsed.port is not None:
        raise ValueError("Video URL must not contain credentials or custom ports")

    host = parsed.netloc.lower()
    if host in _ALLOWED_TWITCH_HOSTS:
        return validate_twitch_vod_url(cleaned), "twitch_vod"

    if host in _ALLOWED_YOUTUBE_HOSTS:
        trimmed_path = parsed.path.rstrip("/")
        video_id = ""
        if host == "youtu.be":
            parts = [p for p in trimmed_path.split("/") if p]
            if len(parts) == 1 and _YOUTUBE_ID_RE.fullmatch(parts[0]):
                video_id = parts[0]
        else:
            if trimmed_path == "/watch":
                qs = parse_qs(parsed.query, keep_blank_values=False)
                v_vals = qs.get("v", [])
                if len(v_vals) == 1 and _YOUTUBE_ID_RE.fullmatch(v_vals[0]):
                    video_id = v_vals[0]
            else:
                parts = [p for p in trimmed_path.split("/") if p]
                if len(parts) == 2 and parts[0] in {"live", "shorts"} and _YOUTUBE_ID_RE.fullmatch(parts[1]):
                    video_id = parts[1]

        if not video_id:
            raise ValueError(
                "YouTube URL must be https://www.youtube.com/watch?v=<id>, /shorts/<id>, /live/<id>, or https://youtu.be/<id>"
            )
        return f"https://www.youtube.com/watch?v={video_id}", "youtube"

    raise ValueError("Only HTTPS YouTube and Twitch VOD URLs are permitted")


def validate_video_upload_filename(filename: str) -> str:
    """Validate an uploaded video filename and return its lowercase extension."""
    if not isinstance(filename, str):
        raise ValueError("Uploaded filename must be a string")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in filename):
        raise ValueError("Uploaded filename contains control characters")
    cleaned = filename.strip()
    if not cleaned:
        raise ValueError("Uploaded filename must not be empty")
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise ValueError("Uploaded filename must not contain path separators or traversal sequences")

    base = Path(cleaned).name
    suffix = Path(base).suffix.lower()
    if not suffix or suffix not in _ALLOWED_VIDEO_EXTENSIONS:
        allowed = ", ".join(sorted(_ALLOWED_VIDEO_EXTENSIONS))
        raise ValueError(f"Unsupported video file type; allowed extensions: {allowed}")
    return suffix


def validate_crop_box(box: dict[str, Any] | CropBox) -> CropBox:
    """Validate and normalize a CropBox from a dict or existing CropBox."""
    if isinstance(box, CropBox):
        return CropBox.model_validate(box.model_dump())
    if isinstance(box, dict):
        return CropBox.model_validate(box)
    raise ValueError("Crop box must be a dict or CropBox")


def safe_resolve_under(base_dir: Path, target: str | Path) -> Path:
    """Resolve target and verify it resides strictly inside base_dir."""
    if isinstance(target, str):
        if "\x00" in target:
            raise ValueError("Target path contains null byte")
        if not target.strip():
            raise ValueError("Target path must not be empty")
    elif "\x00" in str(target):
        raise ValueError("Target path contains null byte")

    base_resolved = Path(base_dir).resolve()
    target_path = Path(target)
    if target_path.is_absolute():
        candidate = target_path.resolve()
    else:
        candidate = (base_resolved / target_path).resolve()

    if not candidate.is_relative_to(base_resolved) or candidate == base_resolved:
        raise ValueError(f"Path '{target}' resolves outside '{base_resolved}'")
    return candidate


def sanitize_chat_text(text: str, max_len: int = 240) -> str:
    """Strip ASCII/C1 control characters from chat text and truncate cleanly."""
    if not isinstance(text, str) or not text:
        return ""
    out_chars: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch in ("\r", "\n", "\t", "\v", "\f"):
            out_chars.append(" ")
        elif code < 32 or code == 127 or 0x80 <= code <= 0x9F:
            continue
        else:
            out_chars.append(ch)
    cleaned = " ".join("".join(out_chars).split())
    if max_len >= 0 and len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned


def build_video_folder_slug(
    source_type: str,
    source_ref: str,
    channel_login: str = "",
) -> str:
    """Derive a safe, deterministic subfolder slug for organizing a video's clips on disk."""
    ref = (source_ref or "").strip()
    if source_type == "youtube_vod" and ref:
        parsed = urlsplit(ref)
        v_vals = parse_qs(parsed.query).get("v", [])
        if v_vals and _YOUTUBE_ID_RE.fullmatch(v_vals[0]):
            return f"yt_{v_vals[0]}"
        parts = [p for p in parsed.path.split("/") if p]
        if parts and _YOUTUBE_ID_RE.fullmatch(parts[-1]):
            return f"yt_{parts[-1]}"

    if source_type == "vod" and ref:
        m = _TWITCH_VOD_PATH_RE.search(urlsplit(ref).path)
        if m:
            return f"vod_{m.group(1)}"

    if source_type == "video_upload" and ref:
        stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", Path(ref).stem).strip("_")[:36]
        if stem:
            return f"upload_{stem.lower()}"

    login_clean = re.sub(r"[^a-zA-Z0-9_]+", "", channel_login or "").lower()[:25]
    if login_clean:
        return f"{source_type or 'clip'}_{login_clean}"
    return "general_clips"
