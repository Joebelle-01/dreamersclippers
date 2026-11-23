"""Application configuration and FFmpeg/GPU capability discovery."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_CAPABILITY_CACHE: dict[str, tuple[bool, str | None]] = {}


def _is_local_bin_shim(path: Path) -> bool:
    """Return True if path lives under ~/.local/bin (often a stripped ffmpeg build)."""
    try:
        local_bin = (Path.home() / ".local" / "bin").resolve()
        return path.resolve().parent == local_bin
    except OSError:
        return False


def _find_winget_gyan_binary(binary_name: str) -> str | None:
    """Resolve full WinGet Gyan.FFmpeg binary before falling back to PATH."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    packages_dir = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    if not packages_dir.is_dir():
        return None
    matches = sorted(packages_dir.glob(f"Gyan.FFmpeg*/**/bin/{binary_name}"), reverse=True)
    for candidate in matches:
        if candidate.is_file():
            return str(candidate)
    return None


def _find_path_binary(binary_name: str) -> str | None:
    """Search PATH while deprioritizing stripped ~/.local/bin binaries."""
    path_env = os.environ.get("PATH", "")
    fallback_local: str | None = None
    for entry in path_env.split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / binary_name
        if not candidate.is_file():
            continue
        if _is_local_bin_shim(candidate):
            if fallback_local is None:
                fallback_local = str(candidate)
            continue
        return str(candidate)
    return fallback_local


def resolve_ffmpeg_path(preferred: str | None = None) -> str:
    """Resolve the preferred full FFmpeg binary path."""
    if preferred:
        pref_path = Path(preferred)
        if pref_path.is_file():
            return str(pref_path)

    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    winget = _find_winget_gyan_binary(exe_name)
    if winget:
        return winget

    found = _find_path_binary(exe_name)
    return found or ""


def resolve_ffprobe_path(ffmpeg_path: str | None = None, preferred: str | None = None) -> str:
    """Resolve ffprobe matching the resolved FFmpeg distribution."""
    if preferred:
        pref_path = Path(preferred)
        if pref_path.is_file():
            return str(pref_path)

    exe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    if ffmpeg_path:
        sibling = Path(ffmpeg_path).with_name(exe_name)
        if sibling.is_file():
            return str(sibling)

    winget = _find_winget_gyan_binary(exe_name)
    if winget:
        return winget

    found = _find_path_binary(exe_name)
    return found or ""


def probe_nvenc(ffmpeg_path: str) -> bool:
    """Check whether FFmpeg exposes the h264_nvenc encoder."""
    if not ffmpeg_path or not Path(ffmpeg_path).is_file():
        return False
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "h264_nvenc" in proc.stdout


def probe_gpu_name() -> str | None:
    """Query the active NVIDIA GPU name via nvidia-smi if available."""
    exe_name = "nvidia-smi.exe" if os.name == "nt" else "nvidia-smi"
    smi_path = _find_path_binary(exe_name) or exe_name
    try:
        proc = subprocess.run(
            [smi_path, "--query-gpu=name", "--format=csv,noheader"],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


class Settings(BaseSettings):
    """StreamClippers configuration loaded from STREAMCLIPPERS_* environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="STREAMCLIPPERS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / "data"
    buffers_dir: Path = PROJECT_ROOT / "data" / "buffers"
    clips_dir: Path = PROJECT_ROOT / "data" / "clips"
    exports_dir: Path = PROJECT_ROOT / "data" / "exports"
    thumbs_dir: Path = PROJECT_ROOT / "data" / "thumbs"
    db_path: Path = PROJECT_ROOT / "data" / "streamclippers.db"

    host: str = "127.0.0.1"
    port: int = 8430

    hype_threshold: float = 68.0
    pre_roll_sec: int = 25
    post_roll_sec: int = 15
    cooldown_sec: int = 35
    weight_chat: float = 0.50
    weight_audio: float = 0.30
    weight_motion: float = 0.20
    default_split_ratio: float = 0.35

    ring_segment_seconds: int = 4
    ring_segment_wrap: int = 35
    whisper_model: str = "base"

    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:5174",
            "http://localhost:5174",
            "http://127.0.0.1:8430",
        ]
    )

    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    nvenc_available: bool = False
    gpu_name: str | None = None

    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    twitch_credentials_path: Path = PROJECT_ROOT / "data" / "twitch_credentials.json"

    @model_validator(mode="after")
    def _derive_paths_and_tools(self) -> Settings:
        if "data_dir" in self.model_fields_set:
            if "buffers_dir" not in self.model_fields_set:
                self.buffers_dir = self.data_dir / "buffers"
            if "clips_dir" not in self.model_fields_set:
                self.clips_dir = self.data_dir / "clips"
            if "exports_dir" not in self.model_fields_set:
                self.exports_dir = self.data_dir / "exports"
            if "thumbs_dir" not in self.model_fields_set:
                self.thumbs_dir = self.data_dir / "thumbs"
            if "db_path" not in self.model_fields_set:
                self.db_path = self.data_dir / "streamclippers.db"
            if "twitch_credentials_path" not in self.model_fields_set:
                self.twitch_credentials_path = self.data_dir / "twitch_credentials.json"

        if not self.ffmpeg_path:
            self.ffmpeg_path = resolve_ffmpeg_path()
        if not self.ffprobe_path:
            self.ffprobe_path = resolve_ffprobe_path(self.ffmpeg_path)

        cache_key = self.ffmpeg_path
        if cache_key not in _CAPABILITY_CACHE:
            _CAPABILITY_CACHE[cache_key] = (
                probe_nvenc(self.ffmpeg_path),
                probe_gpu_name(),
            )
        cached_nvenc, cached_gpu = _CAPABILITY_CACHE[cache_key]
        if "nvenc_available" not in self.model_fields_set:
            self.nvenc_available = cached_nvenc
        if "gpu_name" not in self.model_fields_set:
            self.gpu_name = cached_gpu
        return self

    def probe_capabilities(self, *, force: bool = False) -> None:
        """Refresh FFmpeg, ffprobe, NVENC, and GPU detection."""
        if force:
            self.ffmpeg_path = resolve_ffmpeg_path(self.ffmpeg_path or None)
            self.ffprobe_path = resolve_ffprobe_path(self.ffmpeg_path, self.ffprobe_path or None)
            _CAPABILITY_CACHE[self.ffmpeg_path] = (
                probe_nvenc(self.ffmpeg_path),
                probe_gpu_name(),
            )
        cached_nvenc, cached_gpu = _CAPABILITY_CACHE.get(
            self.ffmpeg_path,
            (probe_nvenc(self.ffmpeg_path), probe_gpu_name()),
        )
        self.nvenc_available = cached_nvenc
        self.gpu_name = cached_gpu

    def ensure_data_dirs(self) -> None:
        """Create required runtime data directories."""
        for directory in (
            self.data_dir,
            self.buffers_dir,
            self.clips_dir,
            self.exports_dir,
            self.thumbs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    """Construct a fresh Settings instance with resolved runtime paths."""
    return Settings()
