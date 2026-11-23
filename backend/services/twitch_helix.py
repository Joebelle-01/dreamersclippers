"""Twitch Helix API client, OAuth PKCE/state manager, and Twitch Clip importer."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from inspect import isawaitable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
from sqlmodel import Session, select

from backend.config import Settings, resolve_ffmpeg_path, resolve_ffprobe_path
from backend.models import Channel, ClipItem, ClipRead
from backend.security import (
    safe_resolve_under,
    sanitize_chat_text,
    validate_crop_box,
    validate_twitch_channel,
    validate_twitch_clip_url,
)
from backend.services.video_renderer import generate_thumbnail, probe_video_duration
from backend.services.webcam_detector import detect_webcam

logger = logging.getLogger(__name__)

_TWITCH_OAUTH_AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
_TWITCH_OAUTH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_TWITCH_OAUTH_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
_TWITCH_HELIX_BASE = "https://api.twitch.tv/helix"

_OAUTH_STATE_TTL_SEC = 600.0
_MAX_PENDING_STATES = 64

_VIEWER_SCOPES: tuple[str, ...] = ("clips:edit", "user:read:chat")
_BROADCASTER_SCOPES: tuple[str, ...] = (
    "clips:edit",
    "channel:manage:clips",
    "editor:manage:clips",
    "channel:manage:broadcast",
    "bits:read",
    "channel:read:hype_train",
    "user:read:chat",
)
_BROADCASTER_CAPABILITY_SCOPES = frozenset(
    {
        "channel:manage:clips",
        "editor:manage:clips",
        "channel:read:hype_train",
        "channel:manage:broadcast",
        "bits:read",
    }
)

_CLIP_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{4,140}$")
_TWITCH_CDN_HOST_SUFFIXES: tuple[str, ...] = (
    ".twitch.tv",
    ".ttvnw.net",
    ".cloudfront.net",
)


def _mask_client_id(client_id: str) -> str | None:
    cleaned = client_id.strip()
    if not cleaned:
        return None
    if len(cleaned) <= 8:
        return "*" * max(0, len(cleaned) - 2) + cleaned[-2:]
    return f"{cleaned[:4]}...{cleaned[-4:]}"


def validate_twitch_cdn_url(url: str) -> str:
    """Strictly validate Twitch clip CDN download URLs to prevent SSRF."""
    if not isinstance(url, str):
        raise ValueError("Clip CDN URL must be a string")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ValueError("Clip CDN URL contains control characters")
    cleaned = url.strip()
    if not cleaned:
        raise ValueError("Clip CDN URL must not be empty")

    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() != "https":
        raise ValueError("Clip CDN URL must use https://")
    if parsed.username is not None or parsed.password is not None or parsed.port is not None:
        raise ValueError("Clip CDN URL must not include credentials or custom ports")
    host = parsed.netloc.lower()
    if not host or not any(
        host == suffix.lstrip(".") or host.endswith(suffix) for suffix in _TWITCH_CDN_HOST_SUFFIXES
    ):
        raise ValueError(f"Host '{host}' is not an allowed Twitch clip CDN host")
    return cleaned


def normalize_clip_url_and_slug(clip_url_or_id: str) -> tuple[str, str]:
    """Validate a Twitch clip URL or slug and return `(canonical_url, slug)`."""
    if not isinstance(clip_url_or_id, str):
        raise ValueError("Twitch clip URL or ID must be a string")
    cleaned = clip_url_or_id.strip()
    if not cleaned:
        raise ValueError("Twitch clip URL or ID must not be empty")
    if _CLIP_SLUG_RE.fullmatch(cleaned):
        canonical = validate_twitch_clip_url(f"https://clips.twitch.tv/{cleaned}")
        return canonical, cleaned

    canonical = validate_twitch_clip_url(cleaned)
    slug = urlsplit(canonical).path.rstrip("/").split("/")[-1]
    if not _CLIP_SLUG_RE.fullmatch(slug):
        raise ValueError("Unable to extract valid Twitch clip slug")
    return canonical, slug


def _extract_twitch_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            msg = data.get("message") or data.get("error_description") or data.get("error")
            if msg:
                return sanitize_chat_text(str(msg), max_len=180)
    except (ValueError, TypeError):
        pass
    return f"HTTP {resp.status_code}"


def _resolve_ytdlp_command() -> list[str]:
    exe_name = "yt-dlp.exe" if os.name == "nt" else "yt-dlp"
    venv_candidate = Path(sys.executable).with_name(exe_name)
    if venv_candidate.is_file():
        return [str(venv_candidate)]
    which_hit = shutil.which("yt-dlp")
    if which_hit:
        return [which_hit]
    return [sys.executable, "-m", "yt_dlp"]


def _download_clip_with_ytdlp_sync(clip_url: str, dest_path: Path, ffmpeg_path: str) -> Path:
    """Download a validated Twitch clip URL using yt-dlp subprocess (`shell=False`)."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = dest_path.parent / f"clipdl_{uuid4().hex[:8]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        cmd = [
            *_resolve_ytdlp_command(),
            "--no-playlist",
            "--quiet",
            "--no-warnings",
            "--merge-output-format",
            "mp4",
            "-f",
            "best[ext=mp4]/bestvideo+bestaudio/best",
            "-o",
            str(tmp_dir / "%(id)s.%(ext)s"),
        ]
        if ffmpeg_path:
            cmd.extend(["--ffmpeg-location", str(Path(ffmpeg_path).parent)])
        cmd.append(clip_url)

        proc = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        candidates = [
            *sorted(tmp_dir.glob("*.mp4")),
            *sorted(tmp_dir.glob("*.mkv")),
            *sorted(tmp_dir.glob("*.webm")),
        ]
        for cand in candidates:
            if cand.is_file() and cand.stat().st_size > 0:
                cand.replace(dest_path)
                return dest_path
        detail = (proc.stderr or "").strip() or f"yt-dlp exited with code {proc.returncode}"
        raise RuntimeError(f"Failed to download Twitch clip: {detail}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


class TwitchHelixClient:
    """Async Twitch Helix + OAuth client with encrypted/local disk persistence."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        default_creds = settings.data_dir / "twitch_credentials.json"
        self.credentials_path = Path(
            getattr(settings, "twitch_credentials_path", None) or default_creds
        )
        self._lock = asyncio.Lock()
        self._pending_states: dict[str, tuple[float, str, str]] = {}
        self._eventsub_connected = False

        self._client_id = str(getattr(settings, "twitch_client_id", "") or "").strip()
        self._client_secret = str(getattr(settings, "twitch_client_secret", "") or "").strip()
        self._app_access_token = ""
        self._app_expires_at = 0.0
        self._user_access_token = ""
        self._user_refresh_token = ""
        self._user_expires_at = 0.0
        self._user_id = ""
        self._user_login = ""
        self._scopes: list[str] = []

        self._load_credentials()

    @property
    def client_id(self) -> str:
        """Return configured Twitch application Client ID."""
        return self._client_id

    @property
    def user_id(self) -> str:
        """Return authenticated Twitch user ID if OAuth is connected."""
        return self._user_id

    @property
    def user_login(self) -> str:
        """Return authenticated Twitch user login if OAuth is connected."""
        return self._user_login

    @property
    def scopes(self) -> list[str]:
        """Return granted user OAuth scopes."""
        return list(self._scopes)

    @property
    def redirect_uri(self) -> str:
        """Return local OAuth callback URL."""
        return f"http://{self.settings.host}:{self.settings.port}/api/twitch/oauth/callback"

    @property
    def has_broadcaster_scopes(self) -> bool:
        """Return True if connected user token includes broadcaster/editor clip or EventSub scopes."""
        return any(scope in _BROADCASTER_CAPABILITY_SCOPES for scope in self._scopes)

    @property
    def eventsub_connected(self) -> bool:
        """Return whether EventSub WebSocket is currently connected."""
        return self._eventsub_connected

    @eventsub_connected.setter
    def eventsub_connected(self, value: bool) -> None:
        self._eventsub_connected = bool(value)

    def set_eventsub_connected(self, connected: bool) -> None:
        """Update cached EventSub connection state."""
        self._eventsub_connected = bool(connected)

    def has_app_credentials(self) -> bool:
        """Return True when Twitch Client ID and Client Secret are configured."""
        return bool(self._client_id and self._client_secret)

    def has_user_token(self) -> bool:
        """Return True when a Twitch user OAuth access token is present."""
        return bool(self._user_access_token)

    def _load_credentials(self) -> None:
        if not self.credentials_path.is_file():
            return
        try:
            raw = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            logger.warning("Ignoring unreadable Twitch credentials file at %s", self.credentials_path)
            return
        if not isinstance(raw, dict):
            return

        file_cid = str(raw.get("client_id") or "").strip()
        file_csec = str(raw.get("client_secret") or "").strip()
        if file_cid:
            self._client_id = file_cid
        if file_csec:
            self._client_secret = file_csec
        self._app_access_token = str(raw.get("app_access_token") or "")
        self._app_expires_at = float(raw.get("app_expires_at") or 0.0)
        self._user_access_token = str(raw.get("user_access_token") or "")
        self._user_refresh_token = str(raw.get("user_refresh_token") or "")
        self._user_expires_at = float(raw.get("user_expires_at") or 0.0)
        self._user_id = str(raw.get("user_id") or "")
        self._user_login = str(raw.get("user_login") or "")
        raw_scopes = raw.get("scopes")
        if isinstance(raw_scopes, list):
            self._scopes = [str(s) for s in raw_scopes if s]

    def _save_credentials(self) -> None:
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "app_access_token": self._app_access_token,
            "app_expires_at": self._app_expires_at,
            "user_access_token": self._user_access_token,
            "user_refresh_token": self._user_refresh_token,
            "user_expires_at": self._user_expires_at,
            "user_id": self._user_id,
            "user_login": self._user_login,
            "scopes": self._scopes,
        }
        tmp_path = self.credentials_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(self.credentials_path)

    def get_status(self) -> dict[str, Any]:
        """Return safe non-secret Twitch integration status."""
        now = time.time()
        return {
            "configured": self.has_app_credentials(),
            "client_id_masked": _mask_client_id(self._client_id),
            "app_token_ready": bool(self._app_access_token and self._app_expires_at > now),
            "user_connected": self.has_user_token(),
            "user_login": self._user_login or None,
            "user_id": self._user_id or None,
            "scopes": list(self._scopes),
            "has_broadcaster_scopes": self.has_broadcaster_scopes,
            "eventsub_connected": self._eventsub_connected,
            "redirect_uri": self.redirect_uri,
        }

    async def save_app_credentials(self, client_id: str, client_secret: str) -> dict[str, Any]:
        """Validate Twitch Client ID/Secret via client_credentials grant and persist safely."""
        clean_id = client_id.strip()
        clean_secret = client_secret.strip()
        if not clean_id or not clean_secret:
            raise ValueError("Twitch Client ID and Client Secret are required")
        if any(ch.isspace() or ord(ch) < 32 for ch in clean_id + clean_secret):
            raise ValueError("Twitch credentials must not contain whitespace or control characters")

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                _TWITCH_OAUTH_TOKEN_URL,
                data={
                    "client_id": clean_id,
                    "client_secret": clean_secret,
                    "grant_type": "client_credentials",
                },
            )
        if resp.status_code != 200:
            raise ValueError(f"Twitch credential validation failed: {_extract_twitch_error(resp)}")

        data = resp.json()
        access_token = str(data.get("access_token") or "")
        if not access_token:
            raise ValueError("Twitch OAuth response did not include an access_token")
        expires_in = int(data.get("expires_in") or 3600)

        async with self._lock:
            self._client_id = clean_id
            self._client_secret = clean_secret
            self._app_access_token = access_token
            self._app_expires_at = time.time() + max(60, expires_in - 60)
            self._save_credentials()

        return self.get_status()

    def build_oauth_authorize_url(
        self,
        mode: Literal["viewer", "broadcaster"],
        redirect_uri: str | None = None,
    ) -> tuple[str, str]:
        """Build a CSRF-protected Twitch OAuth authorization URL and state token."""
        if not self.has_app_credentials():
            raise ValueError("Save Twitch Client ID and Client Secret before starting OAuth")
        if mode not in ("viewer", "broadcaster"):
            raise ValueError("OAuth mode must be 'viewer' or 'broadcaster'")

        active_redirect = redirect_uri or self.redirect_uri
        now = time.time()
        for existing_state, (exp, _, _) in list(self._pending_states.items()):
            if exp <= now:
                self._pending_states.pop(existing_state, None)
        while len(self._pending_states) >= _MAX_PENDING_STATES:
            oldest = next(iter(self._pending_states))
            self._pending_states.pop(oldest, None)

        state = secrets.token_urlsafe(32)
        self._pending_states[state] = (now + _OAUTH_STATE_TTL_SEC, mode, active_redirect)

        scopes = _BROADCASTER_SCOPES if mode == "broadcaster" else _VIEWER_SCOPES
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": active_redirect,
                "response_type": "code",
                "scope": " ".join(scopes),
                "state": state,
                "force_verify": "true",
            }
        )
        return f"{_TWITCH_OAUTH_AUTHORIZE_URL}?{query}", state

    async def handle_oauth_callback(
        self,
        code: str,
        state: str,
        redirect_uri: str | None = None,
    ) -> dict[str, Any]:
        """Verify single-use CSRF state, exchange authorization code, and persist user tokens."""
        clean_state = (state or "").strip()
        clean_code = (code or "").strip()
        pending = self._pending_states.pop(clean_state, None) if clean_state else None
        if pending is None:
            raise ValueError("Invalid or already-consumed OAuth state")
        expires_at, _mode, stored_redirect = pending
        if time.time() > expires_at:
            raise ValueError("OAuth state expired; restart connection")
        if not clean_code:
            raise ValueError("Missing Twitch OAuth authorization code")
        if not self.has_app_credentials():
            raise ValueError("Twitch application credentials are not configured")

        active_redirect = redirect_uri or stored_redirect
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                _TWITCH_OAUTH_TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": clean_code,
                    "grant_type": "authorization_code",
                    "redirect_uri": active_redirect,
                },
            )
            if token_resp.status_code != 200:
                raise ValueError(
                    f"Twitch OAuth code exchange failed: {_extract_twitch_error(token_resp)}"
                )
            token_data = token_resp.json()
            access_token = str(token_data.get("access_token") or "")
            refresh_token = str(token_data.get("refresh_token") or "")
            expires_in = int(token_data.get("expires_in") or 3600)
            if not access_token:
                raise ValueError("Twitch OAuth exchange did not return an access_token")

            val_resp = await client.get(
                _TWITCH_OAUTH_VALIDATE_URL,
                headers={"Authorization": f"OAuth {access_token}"},
            )
            if val_resp.status_code != 200:
                raise ValueError(
                    f"Twitch token validation failed: {_extract_twitch_error(val_resp)}"
                )
            val_data = val_resp.json()

        user_login = str(val_data.get("login") or "").strip().lower()
        user_id = str(val_data.get("user_id") or "").strip()
        raw_scopes = val_data.get("scopes") or token_data.get("scope") or []
        scopes = [str(s) for s in raw_scopes if s]

        async with self._lock:
            self._user_access_token = access_token
            self._user_refresh_token = refresh_token
            self._user_expires_at = time.time() + max(60, expires_in - 60)
            self._user_login = user_login
            self._user_id = user_id
            self._scopes = scopes
            self._save_credentials()

        return self.get_status()

    def disconnect_user(self) -> dict[str, Any]:
        """Clear stored user OAuth tokens while keeping app credentials intact."""
        self._user_access_token = ""
        self._user_refresh_token = ""
        self._user_expires_at = 0.0
        self._user_id = ""
        self._user_login = ""
        self._scopes = []
        self._eventsub_connected = False
        self._save_credentials()
        return self.get_status()

    async def _ensure_app_token(self, *, force_refresh: bool = False) -> str:
        if not self.has_app_credentials():
            raise RuntimeError("Twitch application credentials are not configured")
        now = time.time()
        if not force_refresh and self._app_access_token and self._app_expires_at > (now + 30.0):
            return self._app_access_token

        async with self._lock:
            now = time.time()
            if not force_refresh and self._app_access_token and self._app_expires_at > (now + 30.0):
                return self._app_access_token
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    _TWITCH_OAUTH_TOKEN_URL,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "grant_type": "client_credentials",
                    },
                )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Failed to acquire Twitch app token: {_extract_twitch_error(resp)}"
                )
            data = resp.json()
            token = str(data.get("access_token") or "")
            if not token:
                raise RuntimeError("Twitch app token response missing access_token")
            expires_in = int(data.get("expires_in") or 3600)
            self._app_access_token = token
            self._app_expires_at = time.time() + max(60, expires_in - 60)
            self._save_credentials()
            return token

    async def ensure_user_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid Twitch user OAuth access token, refreshing if needed."""
        if not self._user_access_token and not self._user_refresh_token:
            raise RuntimeError("Twitch user OAuth is not connected")
        now = time.time()
        if not force_refresh and self._user_access_token and self._user_expires_at > (now + 30.0):
            return self._user_access_token

        async with self._lock:
            now = time.time()
            if not force_refresh and self._user_access_token and self._user_expires_at > (now + 30.0):
                return self._user_access_token
            if not self._user_refresh_token:
                if self._user_access_token and not force_refresh:
                    return self._user_access_token
                raise RuntimeError("Twitch user token expired and no refresh_token is available")

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    _TWITCH_OAUTH_TOKEN_URL,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": self._user_refresh_token,
                    },
                )
            if resp.status_code != 200:
                err_msg = _extract_twitch_error(resp)
                self._user_access_token = ""
                self._user_refresh_token = ""
                self._user_expires_at = 0.0
                self._scopes = []
                self._save_credentials()
                raise RuntimeError(f"Twitch user token refresh failed: {err_msg}")

            data = resp.json()
            access_token = str(data.get("access_token") or "")
            if not access_token:
                raise RuntimeError("Twitch refresh response missing access_token")
            refresh_token = str(data.get("refresh_token") or self._user_refresh_token)
            expires_in = int(data.get("expires_in") or 3600)
            self._user_access_token = access_token
            self._user_refresh_token = refresh_token
            self._user_expires_at = time.time() + max(60, expires_in - 60)
            if isinstance(data.get("scope"), list):
                self._scopes = [str(s) for s in data["scope"] if s]
            self._save_credentials()
            return access_token

    async def _helix_request(
        self,
        method: str,
        path: str,
        *,
        require_user: bool = False,
        params: list[tuple[str, str]] | dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{_TWITCH_HELIX_BASE}{path}"
        for attempt in range(2):
            force = attempt == 1
            token = (
                await self.ensure_user_token(force_refresh=force)
                if require_user
                else await self._ensure_app_token(force_refresh=force)
            )
            headers = {
                "Client-Id": self._client_id,
                "Authorization": f"Bearer {token}",
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                )
            if resp.status_code == 401 and attempt == 0:
                continue
            if resp.status_code < 200 or resp.status_code >= 300:
                raise RuntimeError(f"Twitch Helix {path} failed: {_extract_twitch_error(resp)}")
            if not resp.content:
                return {}
            parsed = resp.json()
            return parsed if isinstance(parsed, dict) else {"data": parsed}
        raise RuntimeError(f"Twitch Helix {path} unauthorized after token refresh")

    async def get_users(self, logins: list[str]) -> list[dict[str, Any]]:
        """Resolve Twitch user objects for one or more channel logins."""
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in logins:
            try:
                login = validate_twitch_channel(raw.strip())
            except ValueError:
                continue
            if login not in seen:
                seen.add(login)
                normalized.append(login)
        if not normalized:
            return []

        results: list[dict[str, Any]] = []
        for idx in range(0, len(normalized), 100):
            chunk = normalized[idx : idx + 100]
            payload = await self._helix_request(
                "GET",
                "/users",
                params=[("login", login) for login in chunk],
            )
            items = payload.get("data") or []
            if isinstance(items, list):
                results.extend(item for item in items if isinstance(item, dict))
        return results

    async def get_streams(self, user_logins: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch live Twitch stream metadata keyed by lowercase `user_login`."""
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in user_logins:
            try:
                login = validate_twitch_channel(raw.strip())
            except ValueError:
                continue
            if login not in seen:
                seen.add(login)
                normalized.append(login)
        if not normalized:
            return {}

        streams: dict[str, dict[str, Any]] = {}
        for idx in range(0, len(normalized), 100):
            chunk = normalized[idx : idx + 100]
            payload = await self._helix_request(
                "GET",
                "/streams",
                params=[("user_login", login) for login in chunk],
            )
            items = payload.get("data") or []
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        login_key = str(item.get("user_login") or "").lower()
                        if login_key:
                            streams[login_key] = item
        return streams

    async def get_clips(
        self,
        broadcaster_id: str,
        started_at: str | None = None,
        ended_at: str | None = None,
        first: int = 20,
    ) -> list[dict[str, Any]]:
        """Fetch top Twitch clips for a broadcaster inside an optional RFC3339 time window."""
        params: list[tuple[str, str]] = [
            ("broadcaster_id", str(broadcaster_id)),
            ("first", str(max(1, min(100, int(first))))),
        ]
        if started_at:
            params.append(("started_at", started_at))
        if ended_at:
            params.append(("ended_at", ended_at))
        payload = await self._helix_request("GET", "/clips", params=params)
        items = payload.get("data") or []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def get_clip_by_id(self, clip_id: str) -> dict[str, Any] | None:
        """Fetch metadata for a single Twitch clip ID/slug."""
        payload = await self._helix_request("GET", "/clips", params={"id": clip_id})
        items = payload.get("data") or []
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        return None

    async def get_clips_download(
        self,
        broadcaster_id: str,
        editor_id: str,
        clip_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Fetch authenticated Twitch clip CDN download URLs for owned/edited channels."""
        clean_ids = [cid.strip() for cid in clip_ids if cid and cid.strip()][:10]
        if not clean_ids:
            return []
        params: list[tuple[str, str]] = [
            ("broadcaster_id", str(broadcaster_id)),
            ("editor_id", str(editor_id)),
            *[("clip_id", cid) for cid in clean_ids],
        ]
        payload = await self._helix_request(
            "GET",
            "/clips/download",
            require_user=True,
            params=params,
        )
        items = payload.get("data") or []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def create_clip(
        self,
        broadcaster_id: str,
        title: str | None = None,
        duration: float | None = None,
    ) -> dict[str, Any]:
        """Create a live Twitch clip on the channel using User OAuth (`clips:edit`)."""
        params: dict[str, Any] = {"broadcaster_id": str(broadcaster_id)}
        if title:
            clean_title = sanitize_chat_text(title, max_len=100)
            if clean_title:
                params["title"] = clean_title
        if duration is not None:
            params["duration"] = round(max(5.0, min(60.0, float(duration))), 1)
        payload = await self._helix_request("POST", "/clips", require_user=True, params=params)
        items = payload.get("data") or []
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        raise RuntimeError("Twitch Create Clip returned no clip data")

    async def create_clip_from_vod(
        self,
        broadcaster_id: str,
        editor_id: str,
        video_id: str,
        vod_offset: int,
        duration: float,
        title: str,
    ) -> dict[str, Any]:
        """Create a Twitch clip from an archived VOD using User OAuth."""
        clean_title = sanitize_chat_text(title, max_len=100) or "StreamClippers Clip"
        params = {
            "broadcaster_id": str(broadcaster_id),
            "editor_id": str(editor_id),
            "vod_id": str(video_id),
            "vod_offset": max(0, int(vod_offset)),
            "duration": round(max(5.0, min(60.0, float(duration))), 1),
            "title": clean_title,
        }
        payload = await self._helix_request("POST", "/clips/vod", require_user=True, params=params)
        items = payload.get("data") or []
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        raise RuntimeError("Twitch Create Clip from VOD returned no clip data")

    async def create_stream_marker(
        self,
        user_id: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a live Twitch stream marker on the broadcaster's stream."""
        body: dict[str, Any] = {"user_id": str(user_id)}
        if description:
            clean_desc = sanitize_chat_text(description, max_len=140)
            if clean_desc:
                body["description"] = clean_desc
        payload = await self._helix_request(
            "POST",
            "/streams/markers",
            require_user=True,
            json_body=body,
        )
        items = payload.get("data") or []
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        raise RuntimeError("Twitch Create Stream Marker returned no marker data")

    async def create_eventsub_subscription(
        self,
        sub_type: str,
        version: str,
        condition: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        """Register an EventSub WebSocket subscription for the current WS session."""
        return await self._helix_request(
            "POST",
            "/eventsub/subscriptions",
            require_user=True,
            json_body={
                "type": sub_type,
                "version": version,
                "condition": condition,
                "transport": {
                    "method": "websocket",
                    "session_id": session_id,
                },
            },
        )


async def _emit_clip_created(
    broadcast_fn: Callable[..., Any] | None,
    payload: dict[str, Any],
) -> None:
    if broadcast_fn is None:
        return
    try:
        res = broadcast_fn("clip_created", payload)
        if isawaitable(res):
            await res
    except TypeError:
        res = broadcast_fn({"type": "clip_created", "payload": payload})
        if isawaitable(res):
            await res
    except Exception:
        logger.exception("Failed to broadcast imported Twitch clip")


async def _download_cdn_mp4(cdn_url: str, dest_path: Path) -> Path:
    """Download an HTTPS Twitch CDN MP4 while strictly re-validating redirect targets."""
    current_url = validate_twitch_cdn_url(cdn_url)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(".part")

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
            for _ in range(4):
                async with client.stream("GET", current_url) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise ValueError("Twitch CDN redirect missing Location header")
                        current_url = validate_twitch_cdn_url(location)
                        continue
                    if resp.status_code != 200:
                        raise RuntimeError(f"Twitch CDN download failed (HTTP {resp.status_code})")
                    with tmp_path.open("wb") as handle:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            handle.write(chunk)
                    if not tmp_path.is_file() or tmp_path.stat().st_size == 0:
                        raise RuntimeError("Downloaded Twitch CDN clip is empty")
                    tmp_path.replace(dest_path)
                    return dest_path
        raise RuntimeError("Too many redirects while downloading Twitch CDN clip")
    finally:
        with suppress(OSError):
            tmp_path.unlink(missing_ok=True)


async def import_twitch_clip_to_review(
    clip_url_or_id: str,
    broadcaster_login: str | None,
    prefer_portrait: bool,
    settings: Settings,
    session_factory: Callable[[], Session],
    broadcast_fn: Callable[..., Any] | None = None,
    helix_client: TwitchHelixClient | None = None,
) -> ClipItem:
    """Import a Twitch clip (via Helix CDN download or yt-dlp fallback) into the Review Queue."""
    canonical_url, slug = normalize_clip_url_and_slug(clip_url_or_id)
    ffmpeg_path = settings.ffmpeg_path or resolve_ffmpeg_path()
    ffprobe_path = settings.ffprobe_path or resolve_ffprobe_path(ffmpeg_path)
    if not ffmpeg_path or not ffprobe_path:
        raise RuntimeError("FFmpeg and ffprobe executables are required to import clips")

    settings.ensure_data_dirs()

    clip_meta: dict[str, Any] | None = None
    if helix_client is not None and helix_client.has_app_credentials():
        with suppress(RuntimeError):
            clip_meta = await helix_client.get_clip_by_id(slug)

    resolved_login = ""
    if broadcaster_login:
        with suppress(ValueError):
            resolved_login = validate_twitch_channel(broadcaster_login.strip())
    if not resolved_login and clip_meta:
        meta_login = str(clip_meta.get("broadcaster_name") or "").strip().lower()
        with suppress(ValueError):
            resolved_login = validate_twitch_channel(meta_login)
    if not resolved_login:
        path_parts = [p for p in urlsplit(canonical_url).path.split("/") if p]
        if len(path_parts) >= 3 and path_parts[1] == "clip":
            with suppress(ValueError):
                resolved_login = validate_twitch_channel(path_parts[0])
    if not resolved_login:
        resolved_login = "twitch_clip"

    broadcaster_id = str(clip_meta.get("broadcaster_id") or "") if clip_meta else ""
    clip_title = sanitize_chat_text(
        str((clip_meta or {}).get("title") or f"Twitch Clip {slug}"),
        max_len=120,
    )
    view_count = int((clip_meta or {}).get("view_count") or 0)

    clip_id = str(uuid4())
    raw_clip_path = safe_resolve_under(settings.clips_dir, f"{clip_id}.mp4")
    thumb_path = safe_resolve_under(settings.thumbs_dir, f"{clip_id}.jpg")

    downloaded_via_helix = False
    if (
        helix_client is not None
        and helix_client.has_user_token()
        and helix_client.has_broadcaster_scopes
        and broadcaster_id
        and helix_client.user_id
    ):
        with suppress(RuntimeError, ValueError):
            downloads = await helix_client.get_clips_download(
                broadcaster_id,
                helix_client.user_id,
                [slug],
            )
            if downloads:
                entry = downloads[0]
                primary = (
                    entry.get("portrait_download_url")
                    if prefer_portrait
                    else entry.get("landscape_download_url")
                )
                fallback = (
                    entry.get("landscape_download_url")
                    if prefer_portrait
                    else entry.get("portrait_download_url")
                )
                chosen_url = str(primary or fallback or "").strip()
                if chosen_url:
                    await _download_cdn_mp4(chosen_url, raw_clip_path)
                    downloaded_via_helix = True

    if not downloaded_via_helix:
        await asyncio.to_thread(_download_clip_with_ytdlp_sync, canonical_url, raw_clip_path, ffmpeg_path)

    duration_sec = await asyncio.to_thread(probe_video_duration, raw_clip_path, ffprobe_path)
    if duration_sec <= 0.0:
        duration_sec = float((clip_meta or {}).get("duration") or 10.0)

    await asyncio.to_thread(
        generate_thumbnail,
        raw_clip_path,
        thumb_path,
        ffmpeg_path,
        min(1.0, max(0.2, duration_sec * 0.25)),
    )

    channel_id = 0
    preset_layout_mode = "split_top_cam"
    preset_split_ratio = float(settings.default_split_ratio)
    preset_webcam_json: str | None = None
    preset_gameplay_json: str | None = None

    with session_factory() as session:
        ch = session.exec(select(Channel).where(Channel.channel_login == resolved_login)).first()
        if ch is not None:
            channel_id = int(ch.id or 0)
            preset_layout_mode = str(ch.preset_layout_mode)
            preset_split_ratio = float(ch.preset_split_ratio)
            preset_webcam_json = ch.preset_webcam_json
            preset_gameplay_json = ch.preset_gameplay_json

    used_preset = False
    webcam_box_json: str | None = None
    gameplay_box_json: str | None = None
    layout_mode = preset_layout_mode
    webcam_confidence = 0.0

    if preset_webcam_json:
        try:
            validated_cam = validate_crop_box(json.loads(preset_webcam_json))
            webcam_box_json = json.dumps(validated_cam.model_dump())
            if preset_gameplay_json:
                validated_game = validate_crop_box(json.loads(preset_gameplay_json))
                gameplay_box_json = json.dumps(validated_game.model_dump())
            else:
                gameplay_box_json = json.dumps({"x": 0.22, "y": 0.0, "w": 0.56, "h": 1.0})
            webcam_confidence = 1.0
            used_preset = True
        except (ValueError, TypeError):
            used_preset = False

    if not used_preset:
        detection = await asyncio.to_thread(detect_webcam, raw_clip_path, ffmpeg_path)
        layout_mode = str(detection.layout_mode)
        webcam_confidence = float(detection.confidence)
        webcam_box_json = (
            json.dumps(detection.webcam_box.model_dump())
            if detection.webcam_box is not None
            else None
        )
        gameplay_box_json = json.dumps(detection.gameplay_box.model_dump())

    reason = f"Imported Twitch clip ({view_count:,} views)" if view_count > 0 else "Imported Twitch clip"
    hype_score = min(100.0, round(72.0 + (min(view_count, 5000) / 5000.0) * 24.0, 2))

    clip_row = ClipItem(
        id=clip_id,
        channel_id=channel_id,
        channel_login=resolved_login,
        source_type="twitch_clip",
        source_ref=canonical_url,
        title=clip_title,
        status="pending_review",
        hype_score=hype_score,
        trigger_reason=reason,
        trigger_chat_samples_json="[]",
        hype_timeline_json="[]",
        raw_video_path=str(raw_clip_path),
        thumbnail_path=str(thumb_path),
        duration_sec=float(duration_sec),
        trim_start_sec=0.0,
        trim_end_sec=float(duration_sec),
        layout_mode=layout_mode,
        split_ratio=preset_split_ratio,
        webcam_box_json=webcam_box_json,
        gameplay_box_json=gameplay_box_json,
        webcam_confidence=webcam_confidence,
        normalize_audio=True,
        caption_position="seam",
    )

    with session_factory() as session:
        session.add(clip_row)
        session.commit()
        session.refresh(clip_row)
        clip_payload = ClipRead.from_orm_row(clip_row).model_dump(mode="json")

    await _emit_clip_created(broadcast_fn, clip_payload)
    return clip_row
