"""FastAPI application, REST endpoints, WebSocket stream, and Range media server."""

from __future__ import annotations

import asyncio
import html
import inspect
import json
import logging
import re
import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

import httpx
import uvicorn
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from sqlmodel import Session, select

from backend.config import Settings, get_settings
from backend.db import dispose_engines, get_session_factory, init_db
from backend.models import (
    CaptionSegment,
    Channel,
    ChannelCreate,
    ChannelRead,
    ChannelUpdate,
    ClipItem,
    ClipNowRequest,
    ClipRead,
    ClipUpdate,
    PublishTwitchClipRequest,
    StreamMarkerRequest,
    SystemStatusResponse,
    ThumbnailRequest,
    TwitchClipImportRequest,
    TwitchConfigRequest,
    VideoAnalyzeUrlRequest,
    VodScanRequest,
    utc_now,
)
from backend.security import (
    CropBox,
    safe_resolve_under,
    sanitize_chat_text,
    validate_crop_box,
    validate_twitch_channel,
    validate_twitch_clip_url,
    validate_twitch_vod_url,
    validate_video_upload_filename,
    validate_video_url,
)
from backend.services.caption_service import (
    build_ass_subtitles,
    build_srt_subtitles,
    sanitize_ass_text,
    transcribe_clip,
)
from backend.services.live_manager import LiveManager
from backend.services.twitch_helix import TwitchHelixClient, import_twitch_clip_to_review
from backend.services.video_renderer import render_vertical_clip, render_vertical_thumbnail
from backend.services.viral_analyzer import analyze_local_video, analyze_video_url
from backend.services.vod_scanner import scan_twitch_vod
from backend.services.webcam_detector import detect_webcam

logger = logging.getLogger(__name__)

_RANGE_HEADER_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_CHUNK_SIZE = 256 * 1024


def _parse_crop_input(
    box_val: CropBox | dict[str, Any] | None,
    json_val: str | None,
    *,
    field_name: str,
) -> tuple[bool, str | None]:
    """Parse and strictly validate either an object CropBox or JSON string."""
    if box_val is not None:
        try:
            validated = validate_crop_box(box_val)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid {field_name}: {exc}") from exc
        return True, json.dumps(validated.model_dump())

    if json_val is not None:
        if json_val == "":
            return True, None
        try:
            parsed = json.loads(json_val)
            validated = validate_crop_box(parsed)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid {field_name}_json: {exc}") from exc
        return True, json.dumps(validated.model_dump())

    return False, None


def _load_crop_from_json(raw: str | None) -> CropBox | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return validate_crop_box(parsed)
    except (ValueError, TypeError):
        return None


def _parse_captions_input(
    captions: list[CaptionSegment] | None,
    captions_json: str | None,
) -> tuple[bool, str]:
    """Validate caption segments from structured payload or JSON string."""
    if captions is not None:
        normalized: list[dict[str, Any]] = []
        for seg in captions:
            clean_text = sanitize_ass_text(seg.text)
            if not clean_text:
                continue
            seg_dict = seg.model_dump()
            seg_dict["text"] = clean_text
            normalized.append(seg_dict)
        return True, json.dumps(normalized)

    if captions_json is not None:
        if captions_json == "":
            return True, "[]"
        try:
            parsed = json.loads(captions_json)
            if not isinstance(parsed, list):
                raise ValueError("captions_json must be a JSON array")
            validated = [CaptionSegment.model_validate(item).model_dump() for item in parsed]
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid captions_json: {exc}") from exc
        return True, json.dumps(validated)

    return False, "[]"


def _load_caption_segments(raw_json: str | None) -> list[CaptionSegment]:
    if not raw_json:
        return []
    try:
        parsed = json.loads(raw_json)
        if not isinstance(parsed, list):
            return []
        return [CaptionSegment.model_validate(item) for item in parsed]
    except (ValueError, TypeError):
        return []


def _build_oauth_callback_html(
    *,
    heading: str,
    body_html: str,
    payload: dict[str, Any],
    allowed_origins: list[str],
    auto_close: bool,
) -> str:
    """Build a safe, XSS-hardened HTML page that notifies trusted opener origins."""
    encoded_payload = json.dumps(payload)
    encoded_origins = json.dumps(allowed_origins)
    close_stmt = "setTimeout(function(){window.close();},400);" if auto_close else ""
    return (
        "<!doctype html><html><body style='font-family:sans-serif;background:#181512;"
        f"color:#f2ede4;padding:24px'><h2>{html.escape(heading)}</h2>"
        f"{body_html}"
        "<p><a href='/' style='color:#e5a93c'>Return to StreamClippers</a></p>"
        "<script>(function(){if(window.opener){var msg="
        f"{encoded_payload};var origins={encoded_origins};"
        "for(var i=0;i<origins.length;i++){try{window.opener.postMessage(msg,origins[i]);"
        "}catch(e){}}try{window.opener.postMessage(msg,window.location.origin);}catch(e){}"
        f"{close_stmt}}})();</script></body></html>"
    )


async def _resolve_channel_broadcaster_id(
    channel_id: int,
    login: str,
    cached_broadcaster_id: str | None,
    helix_client: TwitchHelixClient,
    session_factory: Callable[[], Session],
) -> str:
    """Return cached Twitch broadcaster_id or resolve and persist it via Helix."""
    if cached_broadcaster_id:
        return cached_broadcaster_id

    users = await helix_client.get_users([login])
    if not users:
        raise HTTPException(
            status_code=404,
            detail=f"Twitch broadcaster '{login}' not found",
        )
    broadcaster_id = str(users[0].get("id") or "")
    if not broadcaster_id:
        raise HTTPException(status_code=404, detail="Broadcaster ID unavailable")

    with session_factory() as session:
        row = session.get(Channel, channel_id)
        if row is not None:
            row.broadcaster_id = broadcaster_id
            session.add(row)
            session.commit()
    return broadcaster_id


def _raise_twitch_http_error(exc: httpx.HTTPStatusError, action: str) -> None:
    """Translate Twitch HTTP errors into bounded FastAPI HTTPExceptions."""
    code = exc.response.status_code
    mapped = code if code in (400, 401, 403, 404) else 502
    raise HTTPException(status_code=mapped, detail=f"{action} ({code})") from exc


def _file_chunk_iterator(file_path: Path, start: int, length: int) -> Any:
    with file_path.open("rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _serve_media_with_range(request: Request, file_path: Path, media_type: str) -> Response:
    """Serve media file with RFC 9110 HTTP Range (206 Partial Content) support."""
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")

    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(
            path=file_path,
            media_type=media_type,
            headers={"Accept-Ranges": "bytes"},
        )

    match = _RANGE_HEADER_RE.fullmatch(range_header.strip())
    if not match:
        raise HTTPException(
            status_code=416,
            detail="Invalid Range header",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    start_str, end_str = match.group(1), match.group(2)
    if not start_str and not end_str:
        raise HTTPException(
            status_code=416,
            detail="Empty Range bounds",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    if file_size == 0:
        raise HTTPException(
            status_code=416,
            detail="Cannot satisfy Range on empty file",
            headers={"Content-Range": "bytes */0"},
        )

    if not start_str:
        suffix_len = int(end_str)
        if suffix_len <= 0:
            raise HTTPException(
                status_code=416,
                detail="Invalid suffix range",
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        start_idx = max(0, file_size - suffix_len)
        end_idx = file_size - 1
    else:
        start_idx = int(start_str)
        end_idx = int(end_str) if end_str else file_size - 1
        end_idx = min(end_idx, file_size - 1)

    if start_idx >= file_size or start_idx > end_idx:
        raise HTTPException(
            status_code=416,
            detail="Requested range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    content_length = end_idx - start_idx + 1
    headers = {
        "Content-Range": f"bytes {start_idx}-{end_idx}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
    }
    return StreamingResponse(
        _file_chunk_iterator(file_path, start_idx, content_length),
        status_code=206,
        media_type=media_type,
        headers=headers,
    )


def create_app(custom_settings: Settings | None = None) -> FastAPI:
    """Create and configure the StreamClippers FastAPI application."""
    initial_settings = custom_settings or get_settings()

    @asynccontextmanager
    async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
        active_settings = (
            custom_settings
            or getattr(fastapi_app.state, "custom_settings", None)
            or get_settings()
        )
        active_settings.ensure_data_dirs()
        active_settings.probe_capabilities(force=True)
        init_db(active_settings.db_path)
        session_factory = get_session_factory(active_settings.db_path)

        # Reset stale monitoring flags left over from an ungraceful exit.
        with session_factory() as session:
            stale_channels = session.exec(
                select(Channel).where(Channel.is_monitoring.is_(True))  # type: ignore[attr-defined]
            ).all()
            if stale_channels:
                for channel in stale_channels:
                    channel.is_monitoring = False
                    session.add(channel)
                session.commit()

        ws_clients: set[WebSocket] = set()
        background_tasks: set[asyncio.Task[Any]] = set()
        render_inflight: set[str] = set()
        vod_jobs: dict[str, dict[str, Any]] = {}

        async def broadcast_event(
            event_or_type: dict[str, Any] | str,
            payload: dict[str, Any] | None = None,
        ) -> None:
            if isinstance(event_or_type, dict):
                message = {
                    "type": str(event_or_type.get("type", "event")),
                    "payload": event_or_type.get("payload", {}),
                }
            else:
                message = {
                    "type": str(event_or_type),
                    "payload": payload if payload is not None else {},
                }

            encoded = json.dumps(message, default=str)
            stale_sockets: list[WebSocket] = []
            for client in list(ws_clients):
                try:
                    await client.send_text(encoded)
                except (WebSocketDisconnect, RuntimeError, Exception):
                    stale_sockets.append(client)
            for client in stale_sockets:
                ws_clients.discard(client)

        helix_client = TwitchHelixClient(active_settings)
        lm_params = inspect.signature(LiveManager).parameters
        if "helix_client" in lm_params:
            live_manager = LiveManager(
                active_settings,
                session_factory,
                broadcast_event,
                helix_client=helix_client,
            )
        else:
            live_manager = LiveManager(active_settings, session_factory, broadcast_event)

        fastapi_app.state.settings = active_settings
        fastapi_app.state.session_factory = session_factory
        fastapi_app.state.ws_clients = ws_clients
        fastapi_app.state.background_tasks = background_tasks
        fastapi_app.state.render_inflight = render_inflight
        fastapi_app.state.vod_jobs = vod_jobs
        fastapi_app.state.broadcast_event = broadcast_event
        fastapi_app.state.helix_client = helix_client
        fastapi_app.state.live_manager = live_manager

        if hasattr(live_manager, "start_background_services"):
            try:
                await live_manager.start_background_services()
            except Exception as exc:
                logger.warning("LiveManager background services deferred (%s)", exc)

        try:
            yield
        finally:
            pending = list(background_tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            with suppress(Exception):
                await live_manager.shutdown()

            for client in list(ws_clients):
                with suppress(Exception):
                    await client.close()
            ws_clients.clear()
            dispose_engines()

    fastapi_app = FastAPI(
        title="StreamClippers Studio API",
        version="0.1.0",
        lifespan=lifespan,
    )
    fastapi_app.state.custom_settings = custom_settings

    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=initial_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    def _state_settings(request: Request) -> Settings:
        return request.app.state.settings

    def _state_session_factory(request: Request) -> Callable[[], Session]:
        return request.app.state.session_factory

    def _state_live_manager(request: Request) -> LiveManager:
        return request.app.state.live_manager

    def _state_helix_client(request: Request) -> TwitchHelixClient:
        return request.app.state.helix_client

    async def _safe_refresh_helix(live_manager: LiveManager) -> None:
        if hasattr(live_manager, "refresh_helix_subscriptions"):
            try:
                await live_manager.refresh_helix_subscriptions()
            except Exception as exc:
                logger.warning("Helix subscription refresh skipped (%s)", exc)

    @fastapi_app.get("/api/system/status", response_model=SystemStatusResponse)
    async def get_system_status(request: Request) -> SystemStatusResponse:
        settings = _state_settings(request)
        session_factory = _state_session_factory(request)
        live_manager = _state_live_manager(request)

        with session_factory() as session:
            clips = session.exec(select(ClipItem.status)).all()
            pending_count = sum(1 for status in clips if status == "pending_review")
            exported_count = sum(1 for status in clips if status == "exported")

        return SystemStatusResponse(
            ffmpeg_path=settings.ffmpeg_path,
            ffprobe_path=settings.ffprobe_path,
            nvenc_available=settings.nvenc_available,
            gpu_name=settings.gpu_name,
            active_monitors=len(live_manager.active_channel_ids()),
            pending_clips=pending_count,
            exported_clips=exported_count,
            data_dir=str(settings.data_dir),
        )

    @fastapi_app.get("/api/channels", response_model=list[ChannelRead])
    async def list_channels(request: Request) -> list[ChannelRead]:
        session_factory = _state_session_factory(request)
        with session_factory() as session:
            rows = session.exec(select(Channel).order_by(Channel.id)).all()  # type: ignore[arg-type]
            return [ChannelRead.from_orm_row(row) for row in rows]

    @fastapi_app.post("/api/channels", response_model=ChannelRead, status_code=201)
    async def create_channel(payload: ChannelCreate, request: Request) -> ChannelRead:
        session_factory = _state_session_factory(request)
        raw_display = payload.display_name.strip() if payload.display_name else ""
        if (
            not raw_display
            or "://" in raw_display
            or raw_display.lower().startswith(("twitch.tv/", "www.twitch.tv/", "m.twitch.tv/"))
        ):
            display = payload.channel_login
        else:
            display = sanitize_chat_text(raw_display, max_len=64) or payload.channel_login

        with session_factory() as session:
            existing = session.exec(
                select(Channel).where(Channel.channel_login == payload.channel_login)
            ).first()
            if existing is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"Channel '{payload.channel_login}' already exists",
                )

            channel = Channel(
                platform="twitch",
                channel_login=payload.channel_login,
                display_name=display,
                hype_threshold=payload.hype_threshold,
                pre_roll_sec=payload.pre_roll_sec,
                post_roll_sec=payload.post_roll_sec,
                cooldown_sec=payload.cooldown_sec,
                weight_chat=payload.weight_chat,
                weight_audio=payload.weight_audio,
                weight_motion=payload.weight_motion,
                preset_layout_mode=payload.preset_layout_mode,
                preset_split_ratio=payload.preset_split_ratio,
                auto_monitor_online=payload.auto_monitor_online,
                broadcaster_id=payload.broadcaster_id,
            )
            session.add(channel)
            session.commit()
            session.refresh(channel)
            created = ChannelRead.from_orm_row(channel)

        await _safe_refresh_helix(_state_live_manager(request))
        return created

    @fastapi_app.patch("/api/channels/{channel_id}", response_model=ChannelRead)
    async def update_channel(
        channel_id: int,
        payload: ChannelUpdate,
        request: Request,
    ) -> ChannelRead:
        session_factory = _state_session_factory(request)
        has_webcam, webcam_json = _parse_crop_input(
            payload.preset_webcam,
            payload.preset_webcam_json,
            field_name="preset_webcam",
        )
        has_gameplay, gameplay_json = _parse_crop_input(
            payload.preset_gameplay,
            payload.preset_gameplay_json,
            field_name="preset_gameplay",
        )

        with session_factory() as session:
            channel = session.get(Channel, channel_id)
            if channel is None:
                raise HTTPException(status_code=404, detail="Channel not found")

            if payload.display_name is not None:
                sanitized_name = sanitize_chat_text(payload.display_name, max_len=64)
                if sanitized_name:
                    channel.display_name = sanitized_name
            if payload.hype_threshold is not None:
                channel.hype_threshold = payload.hype_threshold
            if payload.pre_roll_sec is not None:
                channel.pre_roll_sec = payload.pre_roll_sec
            if payload.post_roll_sec is not None:
                channel.post_roll_sec = payload.post_roll_sec
            if payload.cooldown_sec is not None:
                channel.cooldown_sec = payload.cooldown_sec
            if payload.weight_chat is not None:
                channel.weight_chat = payload.weight_chat
            if payload.weight_audio is not None:
                channel.weight_audio = payload.weight_audio
            if payload.weight_motion is not None:
                channel.weight_motion = payload.weight_motion
            if payload.preset_layout_mode is not None:
                channel.preset_layout_mode = payload.preset_layout_mode
            if payload.preset_split_ratio is not None:
                channel.preset_split_ratio = payload.preset_split_ratio
            if has_webcam:
                channel.preset_webcam_json = webcam_json
            if has_gameplay:
                channel.preset_gameplay_json = gameplay_json
            if payload.auto_monitor_online is not None:
                channel.auto_monitor_online = payload.auto_monitor_online
            if payload.broadcaster_id is not None:
                channel.broadcaster_id = payload.broadcaster_id

            session.add(channel)
            session.commit()
            session.refresh(channel)
            updated = ChannelRead.from_orm_row(channel)

        await _safe_refresh_helix(_state_live_manager(request))
        return updated

    @fastapi_app.delete("/api/channels/{channel_id}")
    async def delete_channel(channel_id: int, request: Request) -> dict[str, Any]:
        session_factory = _state_session_factory(request)
        live_manager = _state_live_manager(request)

        with session_factory() as session:
            channel = session.get(Channel, channel_id)
            if channel is None:
                raise HTTPException(status_code=404, detail="Channel not found")

        if channel_id in live_manager.active_channel_ids():
            await live_manager.stop_monitor(channel_id)

        with session_factory() as session:
            channel = session.get(Channel, channel_id)
            if channel is not None:
                session.delete(channel)
                session.commit()

        await _safe_refresh_helix(live_manager)
        return {"deleted": True, "id": channel_id}

    @fastapi_app.post("/api/channels/{channel_id}/start", response_model=ChannelRead)
    async def start_channel_monitor(channel_id: int, request: Request) -> ChannelRead:
        session_factory = _state_session_factory(request)
        live_manager = _state_live_manager(request)

        with session_factory() as session:
            channel = session.get(Channel, channel_id)
            if channel is None:
                raise HTTPException(status_code=404, detail="Channel not found")

        try:
            await live_manager.start_monitor(channel_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        with session_factory() as session:
            channel = session.get(Channel, channel_id)
            if channel is None:
                raise HTTPException(status_code=404, detail="Channel not found")
            if not channel.is_monitoring:
                channel.is_monitoring = True
                session.add(channel)
                session.commit()
                session.refresh(channel)
            return ChannelRead.from_orm_row(channel)

    @fastapi_app.post("/api/channels/{channel_id}/stop", response_model=ChannelRead)
    async def stop_channel_monitor(channel_id: int, request: Request) -> ChannelRead:
        session_factory = _state_session_factory(request)
        live_manager = _state_live_manager(request)

        with session_factory() as session:
            channel = session.get(Channel, channel_id)
            if channel is None:
                raise HTTPException(status_code=404, detail="Channel not found")

        await live_manager.stop_monitor(channel_id)

        with session_factory() as session:
            channel = session.get(Channel, channel_id)
            if channel is None:
                raise HTTPException(status_code=404, detail="Channel not found")
            if channel.is_monitoring:
                channel.is_monitoring = False
                session.add(channel)
                session.commit()
                session.refresh(channel)
            return ChannelRead.from_orm_row(channel)

    @fastapi_app.post("/api/channels/{channel_id}/clip-now", response_model=ClipRead)
    async def clip_channel_now(
        channel_id: int,
        request: Request,
        payload: ClipNowRequest | None = None,
    ) -> ClipRead:
        session_factory = _state_session_factory(request)
        live_manager = _state_live_manager(request)

        with session_factory() as session:
            channel = session.get(Channel, channel_id)
            if channel is None:
                raise HTTPException(status_code=404, detail="Channel not found")

        try:
            clip = await live_manager.clip_now(
                channel_id,
                duration_sec=payload.duration_sec if payload is not None else None,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        with session_factory() as session:
            db_clip = session.get(ClipItem, clip.id)
            if db_clip is not None:
                return ClipRead.from_orm_row(db_clip)
        return ClipRead.from_orm_row(clip)

    @fastapi_app.post("/api/vods/scan")
    async def start_vod_scan(payload: VodScanRequest, request: Request) -> dict[str, str]:
        settings = _state_settings(request)
        session_factory = _state_session_factory(request)
        broadcast_event = request.app.state.broadcast_event
        background_tasks: set[asyncio.Task[Any]] = request.app.state.background_tasks
        vod_jobs: dict[str, dict[str, Any]] = request.app.state.vod_jobs

        try:
            safe_url = validate_twitch_vod_url(payload.vod_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if payload.channel_id is not None:
            with session_factory() as session:
                if session.get(Channel, payload.channel_id) is None:
                    raise HTTPException(status_code=404, detail="Referenced channel not found")

        job_id = str(uuid4())
        scan_settings = settings.model_copy(update={"hype_threshold": payload.hype_threshold})
        vod_jobs[job_id] = {"job_id": job_id, "status": "started", "vod_url": safe_url}

        async def _run_vod_scan() -> None:
            try:
                sig = inspect.signature(scan_twitch_vod)
                kwargs: dict[str, Any] = {}
                if "hype_threshold" in sig.parameters:
                    kwargs["hype_threshold"] = payload.hype_threshold
                if "job_id" in sig.parameters:
                    kwargs["job_id"] = job_id
                result = await scan_twitch_vod(
                    safe_url,
                    payload.channel_id,
                    payload.max_clips,
                    scan_settings,
                    session_factory,
                    broadcast_event,
                    **kwargs,
                )
                vod_jobs[job_id] = {"job_id": job_id, "status": "completed", "result": result}
            except asyncio.CancelledError:
                vod_jobs[job_id] = {"job_id": job_id, "status": "cancelled"}
                raise
            except Exception as exc:
                vod_jobs[job_id] = {"job_id": job_id, "status": "failed", "error": str(exc)}
                await broadcast_event(
                    "vod_progress",
                    {
                        "job_id": job_id,
                        "vod_url": safe_url,
                        "status": "failed",
                        "progress": 0,
                        "message": str(exc),
                    },
                )

        task = asyncio.create_task(_run_vod_scan())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return {"job_id": job_id, "status": "started"}

    def _schedule_video_job(
        request: Request,
        *,
        job_id: str,
        source_label: str,
        coro_fn: Callable[[], Any],
    ) -> None:
        broadcast_event = request.app.state.broadcast_event
        background_tasks: set[asyncio.Task[Any]] = request.app.state.background_tasks
        vod_jobs: dict[str, dict[str, Any]] = request.app.state.vod_jobs

        async def _runner() -> None:
            try:
                result = await coro_fn()
                vod_jobs[job_id] = {
                    **vod_jobs.get(job_id, {"job_id": job_id}),
                    "status": "completed",
                    "result": result,
                }
            except asyncio.CancelledError:
                vod_jobs[job_id] = {
                    **vod_jobs.get(job_id, {"job_id": job_id}),
                    "status": "cancelled",
                }
                raise
            except Exception as exc:
                vod_jobs[job_id] = {
                    **vod_jobs.get(job_id, {"job_id": job_id}),
                    "status": "failed",
                    "error": str(exc),
                }
                await broadcast_event(
                    "vod_progress",
                    {
                        "job_id": job_id,
                        "vod_url": source_label,
                        "status": "failed",
                        "progress": 0,
                        "message": str(exc),
                    },
                )

        task = asyncio.create_task(_runner())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    @fastapi_app.post("/api/videos/analyze-url")
    async def analyze_video_by_url(
        payload: VideoAnalyzeUrlRequest,
        request: Request,
    ) -> dict[str, str]:
        settings = _state_settings(request)
        session_factory = _state_session_factory(request)
        broadcast_event = request.app.state.broadcast_event
        vod_jobs: dict[str, dict[str, Any]] = request.app.state.vod_jobs

        try:
            canonical_url, platform = validate_video_url(payload.video_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if payload.channel_id is not None:
            with session_factory() as session:
                if session.get(Channel, payload.channel_id) is None:
                    raise HTTPException(status_code=404, detail="Referenced channel not found")

        job_id = str(uuid4())
        source_type = "youtube_vod" if platform == "youtube" else "vod"
        vod_jobs[job_id] = {
            "job_id": job_id,
            "status": "started",
            "source": canonical_url,
            "source_type": source_type,
            "platform": platform,
        }

        _schedule_video_job(
            request,
            job_id=job_id,
            source_label=canonical_url,
            coro_fn=lambda: analyze_video_url(
                canonical_url,
                channel_id=payload.channel_id,
                max_clips=payload.max_clips,
                min_clip_sec=payload.min_clip_sec,
                max_clip_sec=payload.max_clip_sec,
                viral_threshold=payload.viral_threshold,
                use_speech_analysis=payload.use_speech_analysis,
                settings=settings,
                session_factory=session_factory,
                broadcast_fn=broadcast_event,
                job_id=job_id,
            ),
        )
        return {"job_id": job_id, "status": "started", "platform": platform}

    @fastapi_app.post("/api/videos/analyze-upload")
    async def analyze_video_by_upload(
        request: Request,
        file: Annotated[UploadFile, File()],
        channel_id: Annotated[int | None, Form()] = None,
        max_clips: Annotated[int, Form()] = 5,
        min_clip_sec: Annotated[int, Form()] = 20,
        max_clip_sec: Annotated[int, Form()] = 55,
        viral_threshold: Annotated[float, Form()] = 50.0,
        use_speech_analysis: Annotated[bool, Form()] = True,
    ) -> dict[str, str]:
        settings = _state_settings(request)
        session_factory = _state_session_factory(request)
        broadcast_event = request.app.state.broadcast_event
        vod_jobs: dict[str, dict[str, Any]] = request.app.state.vod_jobs

        raw_filename = file.filename or ""
        try:
            ext = validate_video_upload_filename(raw_filename)
        except ValueError as exc:
            await file.close()
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if not (1 <= max_clips <= 20):
            await file.close()
            raise HTTPException(status_code=422, detail="max_clips must be between 1 and 20")
        if not (8 <= min_clip_sec <= 120):
            await file.close()
            raise HTTPException(status_code=422, detail="min_clip_sec must be between 8 and 120")
        if not (15 <= max_clip_sec <= 180) or max_clip_sec < min_clip_sec + 5:
            await file.close()
            raise HTTPException(
                status_code=422,
                detail="max_clip_sec must be 15-180 and at least min_clip_sec + 5",
            )
        if not (10.0 <= viral_threshold <= 100.0):
            await file.close()
            raise HTTPException(status_code=422, detail="viral_threshold must be between 10 and 100")

        if channel_id is not None:
            with session_factory() as session:
                if session.get(Channel, channel_id) is None:
                    await file.close()
                    raise HTTPException(status_code=404, detail="Referenced channel not found")

        job_id = str(uuid4())
        settings.ensure_data_dirs()
        upload_dir = safe_resolve_under(settings.buffers_dir, f"upload_{job_id}")
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest_path = safe_resolve_under(upload_dir, f"source{ext}")

        total_bytes = 0
        try:
            with dest_path.open("wb") as out_fh:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > settings.max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail="Uploaded video exceeds maximum allowed size",
                        )
                    out_fh.write(chunk)
        except Exception:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise
        finally:
            await file.close()

        if total_bytes == 0:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise HTTPException(status_code=422, detail="Uploaded video file is empty")

        clean_label = sanitize_chat_text(Path(raw_filename).name, max_len=120) or f"upload{ext}"
        vod_jobs[job_id] = {
            "job_id": job_id,
            "status": "started",
            "source": clean_label,
            "source_type": "video_upload",
        }

        _schedule_video_job(
            request,
            job_id=job_id,
            source_label=clean_label,
            coro_fn=lambda: analyze_local_video(
                dest_path,
                clean_label,
                channel_id=channel_id,
                source_type="video_upload",
                source_ref=clean_label,
                max_clips=max_clips,
                min_clip_sec=min_clip_sec,
                max_clip_sec=max_clip_sec,
                viral_threshold=viral_threshold,
                use_speech_analysis=use_speech_analysis,
                settings=settings,
                session_factory=session_factory,
                broadcast_fn=broadcast_event,
                job_id=job_id,
                cleanup_dir=upload_dir,
            ),
        )
        return {"job_id": job_id, "status": "started", "source_type": "video_upload"}

    @fastapi_app.get("/api/videos/jobs/{job_id}")
    async def get_video_analysis_job(job_id: str, request: Request) -> dict[str, Any]:
        vod_jobs: dict[str, dict[str, Any]] = request.app.state.vod_jobs
        job = vod_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Video analysis job not found")
        return job

    @fastapi_app.get("/api/clips", response_model=list[ClipRead])
    async def list_clips(
        request: Request,
        status: Annotated[str | None, Query()] = None,
        channel_id: Annotated[int | None, Query()] = None,
    ) -> list[ClipRead]:
        session_factory = _state_session_factory(request)
        with session_factory() as session:
            stmt = select(ClipItem).order_by(ClipItem.captured_at.desc())  # type: ignore[attr-defined]
            if status and status != "all":
                stmt = stmt.where(ClipItem.status == status)
            if channel_id is not None:
                stmt = stmt.where(ClipItem.channel_id == channel_id)
            rows = session.exec(stmt).all()
            return [ClipRead.from_orm_row(row) for row in rows]

    @fastapi_app.get("/api/clips/{clip_id}", response_model=ClipRead)
    async def get_clip(clip_id: str, request: Request) -> ClipRead:
        session_factory = _state_session_factory(request)
        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")
            return ClipRead.from_orm_row(clip)

    @fastapi_app.patch("/api/clips/{clip_id}", response_model=ClipRead)
    async def update_clip(clip_id: str, payload: ClipUpdate, request: Request) -> ClipRead:
        session_factory = _state_session_factory(request)
        has_webcam, webcam_json = _parse_crop_input(
            payload.webcam_box,
            payload.webcam_box_json,
            field_name="webcam_box",
        )
        has_gameplay, gameplay_json = _parse_crop_input(
            payload.gameplay_box,
            payload.gameplay_box_json,
            field_name="gameplay_box",
        )
        has_captions, captions_json = _parse_captions_input(
            payload.captions,
            payload.captions_json,
        )

        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")

            new_start = (
                payload.trim_start_sec
                if payload.trim_start_sec is not None
                else float(clip.trim_start_sec)
            )
            new_end = (
                payload.trim_end_sec
                if payload.trim_end_sec is not None
                else float(clip.trim_end_sec)
            )

            if payload.trim_start_sec is not None or payload.trim_end_sec is not None:
                if clip.duration_sec > 0.0:
                    if clip.duration_sec < new_end <= clip.duration_sec + 0.1:
                        new_end = float(clip.duration_sec)
                    elif new_end > clip.duration_sec + 0.1:
                        raise HTTPException(
                            status_code=422,
                            detail="trim_end_sec exceeds clip duration",
                        )
                if new_end <= new_start:
                    raise HTTPException(
                        status_code=422,
                        detail="trim_end_sec must be greater than trim_start_sec",
                    )
                clip.trim_start_sec = new_start
                clip.trim_end_sec = new_end

            if payload.title is not None:
                cleaned_title = sanitize_chat_text(payload.title, max_len=140)
                if cleaned_title:
                    clip.title = cleaned_title
            if payload.status is not None:
                clip.status = payload.status
            if payload.layout_mode is not None:
                clip.layout_mode = payload.layout_mode
            if payload.split_ratio is not None:
                clip.split_ratio = payload.split_ratio
            if payload.normalize_audio is not None:
                clip.normalize_audio = payload.normalize_audio
            if payload.captions_enabled is not None:
                clip.captions_enabled = payload.captions_enabled
            if payload.caption_style is not None:
                clip.caption_style = payload.caption_style
            if payload.caption_position is not None:
                clip.caption_position = payload.caption_position
            if has_captions:
                clip.captions_json = captions_json
            if payload.thumb_time_sec is not None:
                clip.thumb_time_sec = payload.thumb_time_sec
            if payload.thumb_title_overlay is not None:
                clip.thumb_title_overlay = payload.thumb_title_overlay
            if has_webcam:
                clip.webcam_box_json = webcam_json
            if has_gameplay:
                clip.gameplay_box_json = gameplay_json

            session.add(clip)
            session.commit()
            session.refresh(clip)
            return ClipRead.from_orm_row(clip)

    @fastapi_app.delete("/api/clips/{clip_id}")
    async def delete_clip(clip_id: str, request: Request) -> dict[str, Any]:
        settings = _state_settings(request)
        session_factory = _state_session_factory(request)

        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")
            raw_path = clip.raw_video_path
            export_path = clip.export_video_path
            export_srt_path = clip.export_srt_path
            thumb_path = clip.thumbnail_path
            export_thumb_path = clip.export_thumb_path
            session.delete(clip)
            session.commit()

        for base_dir, target_path in (
            (settings.clips_dir, raw_path),
            (settings.exports_dir, export_path),
            (settings.exports_dir, export_srt_path),
            (settings.thumbs_dir, thumb_path),
            (settings.thumbs_dir, export_thumb_path),
        ):
            if not target_path:
                continue
            with suppress(ValueError, OSError):
                resolved = safe_resolve_under(base_dir, target_path)
                if resolved.is_file():
                    resolved.unlink(missing_ok=True)

        return {"deleted": True, "id": clip_id}

    @fastapi_app.post("/api/clips/{clip_id}/detect-webcam", response_model=ClipRead)
    async def detect_clip_webcam(clip_id: str, request: Request) -> ClipRead:
        settings = _state_settings(request)
        session_factory = _state_session_factory(request)

        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")
            raw_video_path = clip.raw_video_path

        try:
            raw_path = safe_resolve_under(settings.clips_dir, raw_video_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not raw_path.is_file():
            raise HTTPException(status_code=404, detail="Raw clip video file not found")

        detection = await asyncio.to_thread(detect_webcam, raw_path, settings.ffmpeg_path)

        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")
            clip.webcam_confidence = float(detection.confidence)
            clip.layout_mode = str(detection.layout_mode)
            clip.webcam_box_json = (
                json.dumps(detection.webcam_box.model_dump())
                if detection.webcam_box is not None
                else None
            )
            clip.gameplay_box_json = json.dumps(detection.gameplay_box.model_dump())
            session.add(clip)
            session.commit()
            session.refresh(clip)
            return ClipRead.from_orm_row(clip)

    @fastapi_app.post("/api/clips/{clip_id}/save-preset", response_model=ChannelRead)
    async def save_clip_preset_to_channel(clip_id: str, request: Request) -> ChannelRead:
        session_factory = _state_session_factory(request)

        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")
            channel = session.get(Channel, clip.channel_id)
            if channel is None:
                raise HTTPException(
                    status_code=404,
                    detail="No linked channel found to store framing preset",
                )

            channel.preset_layout_mode = clip.layout_mode
            channel.preset_split_ratio = clip.split_ratio
            channel.preset_webcam_json = clip.webcam_box_json
            channel.preset_gameplay_json = clip.gameplay_box_json
            session.add(channel)
            session.commit()
            session.refresh(channel)
            return ChannelRead.from_orm_row(channel)

    @fastapi_app.post("/api/clips/{clip_id}/captions/generate", response_model=ClipRead)
    async def generate_clip_captions(clip_id: str, request: Request) -> ClipRead:
        settings = _state_settings(request)
        session_factory = _state_session_factory(request)

        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")
            raw_video_path = clip.raw_video_path
            trim_start = float(clip.trim_start_sec)
            trim_end = (
                float(clip.trim_end_sec)
                if clip.trim_end_sec > clip.trim_start_sec
                else float(clip.duration_sec)
            )

        try:
            raw_path = safe_resolve_under(settings.clips_dir, raw_video_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not raw_path.is_file():
            raise HTTPException(status_code=404, detail="Raw clip video file not found")

        segments = await asyncio.to_thread(
            transcribe_clip,
            raw_path,
            trim_start,
            trim_end,
            settings.ffmpeg_path,
            settings.whisper_model,
        )
        encoded_captions = json.dumps([seg.model_dump() for seg in segments])

        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")
            clip.captions_json = encoded_captions
            clip.captions_enabled = True
            session.add(clip)
            session.commit()
            session.refresh(clip)
            return ClipRead.from_orm_row(clip)

    @fastapi_app.post("/api/clips/{clip_id}/thumbnail", response_model=ClipRead)
    async def generate_clip_vertical_thumbnail(
        clip_id: str,
        payload: ThumbnailRequest,
        request: Request,
    ) -> ClipRead:
        settings = _state_settings(request)
        session_factory = _state_session_factory(request)

        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")
            raw_video_path = clip.raw_video_path
            trim_start = float(clip.trim_start_sec)
            trim_end = (
                float(clip.trim_end_sec)
                if clip.trim_end_sec > clip.trim_start_sec
                else float(clip.duration_sec)
            )
            at_sec = (
                float(payload.at_sec)
                if payload.at_sec is not None
                else (
                    float(clip.thumb_time_sec)
                    if clip.thumb_time_sec is not None
                    else (trim_start + trim_end) / 2.0
                )
            )
            title_overlay = (
                bool(payload.title_overlay)
                if payload.title_overlay is not None
                else bool(clip.thumb_title_overlay)
            )
            custom_title = (
                sanitize_chat_text(payload.title_text, max_len=80)
                if payload.title_text
                else ""
            )
            title_text = (custom_title or str(clip.title)) if title_overlay else None
            layout_mode = str(clip.layout_mode)
            split_ratio = float(clip.split_ratio)
            webcam_box = _load_crop_from_json(clip.webcam_box_json)
            gameplay_box = _load_crop_from_json(clip.gameplay_box_json)

        try:
            raw_path = safe_resolve_under(settings.clips_dir, raw_video_path)
            thumb_out = safe_resolve_under(settings.thumbs_dir, f"{clip_id}_9x16.jpg")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not raw_path.is_file():
            raise HTTPException(status_code=404, detail="Raw clip video file not found")

        await asyncio.to_thread(
            render_vertical_thumbnail,
            raw_path,
            thumb_out,
            at_sec,
            layout_mode,
            split_ratio,
            webcam_box,
            gameplay_box,
            settings.ffmpeg_path,
            title_text,
        )

        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")
            clip.export_thumb_path = str(thumb_out)
            clip.thumb_time_sec = float(at_sec)
            clip.thumb_title_overlay = bool(title_overlay)
            session.add(clip)
            session.commit()
            session.refresh(clip)
            return ClipRead.from_orm_row(clip)

    @fastapi_app.post("/api/clips/{clip_id}/render")
    async def render_clip_endpoint(
        clip_id: str,
        background_tasks: BackgroundTasks,
        request: Request,
    ) -> dict[str, str]:
        settings = _state_settings(request)
        session_factory = _state_session_factory(request)
        broadcast_event = request.app.state.broadcast_event
        render_inflight: set[str] = request.app.state.render_inflight

        if clip_id in render_inflight:
            raise HTTPException(status_code=409, detail="Clip render is already in progress")

        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")
            if clip.status == "rendering":
                raise HTTPException(status_code=409, detail="Clip is already rendering")

            try:
                raw_path = safe_resolve_under(settings.clips_dir, clip.raw_video_path)
                export_path = safe_resolve_under(settings.exports_dir, f"{clip_id}_vertical.mp4")
                ass_temp_path = safe_resolve_under(settings.exports_dir, f"{clip_id}.ass")
                srt_out_path = safe_resolve_under(settings.exports_dir, f"{clip_id}.srt")
                export_thumb_out = safe_resolve_under(settings.thumbs_dir, f"{clip_id}_9x16.jpg")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            if not raw_path.is_file():
                raise HTTPException(status_code=404, detail="Raw clip video file not found")

            trim_start = float(clip.trim_start_sec)
            trim_end = float(clip.trim_end_sec) if clip.trim_end_sec > clip.trim_start_sec else float(
                clip.duration_sec
            )
            layout_mode = str(clip.layout_mode)
            split_ratio = float(clip.split_ratio)
            webcam_box = _load_crop_from_json(clip.webcam_box_json)
            gameplay_box = _load_crop_from_json(clip.gameplay_box_json)
            normalize_audio = bool(clip.normalize_audio)
            captions_enabled = bool(clip.captions_enabled)
            caption_style = str(clip.caption_style or "bold_pop")
            clip_caption_position = str(clip.caption_position or "seam")
            captions_json_str = str(clip.captions_json or "[]")
            thumb_time_sec = clip.thumb_time_sec
            thumb_title_overlay = bool(clip.thumb_title_overlay)
            clip_title = str(clip.title)

            clip.status = "rendering"
            clip.error_message = None
            session.add(clip)
            session.commit()

        render_inflight.add(clip_id)

        async def _run_render_job() -> None:
            ass_written: Path | None = None
            srt_path_str: str | None = None
            try:
                await broadcast_event(
                    "render_status",
                    {"clip_id": clip_id, "status": "rendering"},
                )

                if captions_enabled:
                    segments = _load_caption_segments(captions_json_str)
                    if not segments:
                        await broadcast_event(
                            "render_status",
                            {
                                "clip_id": clip_id,
                                "status": "rendering",
                                "stage": "Transcribing captions",
                            },
                        )
                        segments = await asyncio.to_thread(
                            transcribe_clip,
                            raw_path,
                            trim_start,
                            trim_end,
                            settings.ffmpeg_path,
                            settings.whisper_model,
                        )
                        if segments:
                            encoded_captions = json.dumps([s.model_dump() for s in segments])
                            with session_factory() as session:
                                row = session.get(ClipItem, clip_id)
                                if row is not None:
                                    row.captions_json = encoded_captions
                                    session.add(row)
                                    session.commit()

                    if segments:
                        ass_text = build_ass_subtitles(
                            segments,
                            layout_mode=layout_mode,
                            split_ratio=split_ratio,
                            caption_style=caption_style,
                            caption_position=clip_caption_position,
                        )
                        ass_temp_path.parent.mkdir(parents=True, exist_ok=True)
                        ass_temp_path.write_text(ass_text, encoding="utf-8")
                        ass_written = ass_temp_path

                        srt_text = build_srt_subtitles(segments)
                        if srt_text:
                            srt_out_path.parent.mkdir(parents=True, exist_ok=True)
                            srt_out_path.write_text(srt_text, encoding="utf-8")
                            srt_path_str = str(srt_out_path)

                result = await asyncio.to_thread(
                    render_vertical_clip,
                    raw_path,
                    export_path,
                    trim_start,
                    trim_end,
                    layout_mode,
                    split_ratio,
                    webcam_box,
                    gameplay_box,
                    normalize_audio,
                    settings.ffmpeg_path,
                    settings.nvenc_available,
                    ass_written,
                )

                target_thumb_sec = (
                    float(thumb_time_sec)
                    if thumb_time_sec is not None
                    else (trim_start + trim_end) / 2.0
                )
                title_overlay_text = clip_title if thumb_title_overlay else None
                await asyncio.to_thread(
                    render_vertical_thumbnail,
                    raw_path,
                    export_thumb_out,
                    target_thumb_sec,
                    layout_mode,
                    split_ratio,
                    webcam_box,
                    gameplay_box,
                    settings.ffmpeg_path,
                    title_overlay_text,
                )

                with session_factory() as session:
                    row = session.get(ClipItem, clip_id)
                    if row is not None:
                        row.status = "exported"
                        row.export_video_path = str(result.output_path)
                        row.export_srt_path = srt_path_str
                        row.export_thumb_path = str(export_thumb_out)
                        row.thumb_time_sec = target_thumb_sec
                        row.encoder_used = str(result.encoder_used)
                        row.error_message = None
                        row.exported_at = utc_now()
                        session.add(row)
                        session.commit()

                await broadcast_event(
                    "render_status",
                    {
                        "clip_id": clip_id,
                        "status": "exported",
                        "export_video_path": str(result.output_path),
                        "export_srt_path": srt_path_str,
                        "export_thumb_path": str(export_thumb_out),
                        "encoder_used": str(result.encoder_used),
                    },
                )
            except Exception as exc:
                with session_factory() as session:
                    row = session.get(ClipItem, clip_id)
                    if row is not None:
                        row.status = "failed"
                        row.error_message = str(exc)
                        session.add(row)
                        session.commit()
                await broadcast_event(
                    "render_status",
                    {
                        "clip_id": clip_id,
                        "status": "failed",
                        "error": str(exc),
                    },
                )
            finally:
                if ass_written is not None:
                    with suppress(OSError):
                        ass_written.unlink(missing_ok=True)
                render_inflight.discard(clip_id)

        background_tasks.add_task(_run_render_job)
        return {"clip_id": clip_id, "status": "rendering"}

    @fastapi_app.get("/api/media/raw/{clip_id}")
    async def get_raw_media(clip_id: str, request: Request) -> Response:
        settings = _state_settings(request)
        session_factory = _state_session_factory(request)
        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")
            raw_path_str = clip.raw_video_path

        try:
            resolved = safe_resolve_under(settings.clips_dir, raw_path_str)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return _serve_media_with_range(request, resolved, "video/mp4")

    @fastapi_app.get("/api/media/export/{clip_id}")
    async def get_export_media(clip_id: str, request: Request) -> Response:
        settings = _state_settings(request)
        session_factory = _state_session_factory(request)
        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")
            export_path_str = clip.export_video_path

        if not export_path_str:
            raise HTTPException(status_code=404, detail="Exported video not available")

        try:
            resolved = safe_resolve_under(settings.exports_dir, export_path_str)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return _serve_media_with_range(request, resolved, "video/mp4")

    @fastapi_app.get("/api/media/thumb/{clip_id}")
    async def get_thumb_media(clip_id: str, request: Request) -> Response:
        settings = _state_settings(request)
        session_factory = _state_session_factory(request)
        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")
            thumb_path_str = clip.thumbnail_path

        if not thumb_path_str:
            raise HTTPException(status_code=404, detail="Thumbnail not available")

        try:
            resolved = safe_resolve_under(settings.thumbs_dir, thumb_path_str)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="Thumbnail file not found")
        media_type = "image/png" if resolved.suffix.lower() == ".png" else "image/jpeg"
        return FileResponse(path=resolved, media_type=media_type)

    @fastapi_app.get("/api/media/export-thumb/{clip_id}")
    async def get_export_thumb_media(clip_id: str, request: Request) -> Response:
        settings = _state_settings(request)
        session_factory = _state_session_factory(request)
        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")
            thumb_path_str = clip.export_thumb_path

        if not thumb_path_str:
            raise HTTPException(status_code=404, detail="9:16 cover thumbnail not available")

        try:
            resolved = safe_resolve_under(settings.thumbs_dir, thumb_path_str)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="9:16 cover thumbnail file not found")
        return FileResponse(
            path=resolved,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    @fastapi_app.get("/api/media/srt/{clip_id}")
    async def get_export_srt_media(clip_id: str, request: Request) -> Response:
        settings = _state_settings(request)
        session_factory = _state_session_factory(request)
        with session_factory() as session:
            clip = session.get(ClipItem, clip_id)
            if clip is None:
                raise HTTPException(status_code=404, detail="Clip not found")
            srt_path_str = clip.export_srt_path
            login = str(clip.channel_login)

        if not srt_path_str:
            raise HTTPException(status_code=404, detail="Subtitles file not available")

        try:
            resolved = safe_resolve_under(settings.exports_dir, srt_path_str)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="Subtitles file not found")
        return FileResponse(
            path=resolved,
            media_type="text/plain; charset=utf-8",
            filename=f"{login}_{clip_id[:8]}.srt",
        )

    @fastapi_app.get("/api/twitch/status")
    async def get_twitch_status(request: Request) -> dict[str, Any]:
        helix_client = _state_helix_client(request)
        return helix_client.get_status()

    @fastapi_app.put("/api/twitch/config")
    async def update_twitch_config(
        payload: TwitchConfigRequest,
        request: Request,
    ) -> dict[str, Any]:
        helix_client = _state_helix_client(request)
        live_manager = _state_live_manager(request)
        try:
            status = await helix_client.save_app_credentials(
                payload.client_id,
                payload.client_secret,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to validate Twitch app credentials: {exc.__class__.__name__}",
            ) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        await _safe_refresh_helix(live_manager)
        return status

    @fastapi_app.get("/api/twitch/oauth/start")
    async def start_twitch_oauth(
        request: Request,
        mode: Annotated[Literal["viewer", "broadcaster"], Query()] = "viewer",
    ) -> dict[str, str]:
        settings = _state_settings(request)
        helix_client = _state_helix_client(request)
        if not helix_client.has_app_credentials():
            raise HTTPException(
                status_code=400,
                detail="Configure Twitch Client ID and Client Secret first",
            )
        status_info = helix_client.get_status()
        redirect_uri = str(
            status_info.get("redirect_uri")
            or f"http://{settings.host}:{settings.port}/api/twitch/oauth/callback"
        )
        authorize_url, state = helix_client.build_oauth_authorize_url(mode, redirect_uri)
        return {
            "authorize_url": authorize_url,
            "state": state,
            "redirect_uri": redirect_uri,
        }

    @fastapi_app.get("/api/twitch/oauth/callback", response_class=HTMLResponse)
    async def twitch_oauth_callback(
        request: Request,
        code: Annotated[str | None, Query()] = None,
        state: Annotated[str | None, Query()] = None,
        error: Annotated[str | None, Query()] = None,
        error_description: Annotated[str | None, Query()] = None,
    ) -> HTMLResponse:
        settings = _state_settings(request)
        helix_client = _state_helix_client(request)
        live_manager = _state_live_manager(request)

        if error:
            escaped_err = html.escape(error_description or error)
            page_html = _build_oauth_callback_html(
                heading="Twitch Authorization Cancelled",
                body_html=f"<p>{escaped_err}</p>",
                payload={
                    "type": "streamclippers_twitch_oauth",
                    "status": "error",
                    "error": error_description or error,
                },
                allowed_origins=settings.cors_origins,
                auto_close=False,
            )
            return HTMLResponse(content=page_html, status_code=400)

        if not code or not state:
            raise HTTPException(status_code=400, detail="Missing OAuth code or state")

        status_info = helix_client.get_status()
        redirect_uri = str(
            status_info.get("redirect_uri")
            or f"http://{settings.host}:{settings.port}/api/twitch/oauth/callback"
        )
        try:
            updated_status = await helix_client.handle_oauth_callback(code, state, redirect_uri)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (httpx.HTTPError, RuntimeError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Twitch OAuth exchange failed: {exc}",
            ) from exc

        await _safe_refresh_helix(live_manager)

        user_login = str(updated_status.get("user_login") or "")
        escaped_login = html.escape(user_login or "connected account")
        page_html = _build_oauth_callback_html(
            heading="Twitch Account Connected",
            body_html=f"<p>Signed in as <strong>{escaped_login}</strong>. You can close this window.</p>",
            payload={
                "type": "streamclippers_twitch_oauth",
                "status": "connected",
                "user_login": user_login,
            },
            allowed_origins=settings.cors_origins,
            auto_close=True,
        )
        return HTMLResponse(content=page_html, status_code=200)

    @fastapi_app.post("/api/twitch/oauth/disconnect")
    async def disconnect_twitch_oauth(request: Request) -> dict[str, Any]:
        helix_client = _state_helix_client(request)
        live_manager = _state_live_manager(request)
        res = helix_client.disconnect_user()
        status = await res if inspect.isawaitable(res) else res
        await _safe_refresh_helix(live_manager)
        return status

    @fastapi_app.post("/api/twitch/clips/import", response_model=ClipRead)
    async def import_twitch_clip(
        payload: TwitchClipImportRequest,
        request: Request,
    ) -> ClipRead:
        settings = _state_settings(request)
        session_factory = _state_session_factory(request)
        helix_client = _state_helix_client(request)
        broadcast_event = request.app.state.broadcast_event

        try:
            safe_clip_url = validate_twitch_clip_url(payload.clip_url)
            clean_login = (
                validate_twitch_channel(payload.broadcaster_login)
                if payload.broadcaster_login and payload.broadcaster_login.strip()
                else None
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        try:
            clip = await import_twitch_clip_to_review(
                safe_clip_url,
                clean_login,
                payload.prefer_portrait,
                settings,
                session_factory,
                broadcast_event,
                helix_client=helix_client,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to download Twitch clip: {exc.__class__.__name__}",
            ) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        with session_factory() as session:
            db_clip = session.get(ClipItem, clip.id)
            if db_clip is not None:
                return ClipRead.from_orm_row(db_clip)
        return ClipRead.from_orm_row(clip)

    @fastapi_app.get("/api/twitch/clips/{channel_login}")
    async def list_twitch_top_clips(
        channel_login: str,
        request: Request,
        period: Annotated[Literal["day", "week", "month"], Query()] = "day",
        first: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> list[dict[str, Any]]:
        helix_client = _state_helix_client(request)
        try:
            login = validate_twitch_channel(channel_login)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if not helix_client.has_app_credentials():
            raise HTTPException(
                status_code=400,
                detail="Configure Twitch Client ID and Client Secret first",
            )

        days = {"day": 1, "week": 7, "month": 30}[period]
        started_at = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            users = await helix_client.get_users([login])
            if not users:
                raise HTTPException(status_code=404, detail=f"Twitch channel '{login}' not found")
            broadcaster_id = str(users[0].get("id") or "")
            if not broadcaster_id:
                raise HTTPException(status_code=404, detail="Broadcaster ID unavailable")
            return await helix_client.get_clips(broadcaster_id, started_at=started_at, first=first)
        except HTTPException:
            raise
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=exc.response.status_code if exc.response.status_code < 500 else 502,
                detail=f"Twitch API returned {exc.response.status_code}",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Twitch API error: {exc.__class__.__name__}",
            ) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @fastapi_app.post("/api/channels/{channel_id}/publish-clip")
    async def publish_channel_twitch_clip(
        channel_id: int,
        request: Request,
        payload: PublishTwitchClipRequest | None = None,
    ) -> dict[str, Any]:
        session_factory = _state_session_factory(request)
        helix_client = _state_helix_client(request)
        req = payload or PublishTwitchClipRequest()

        if not helix_client.has_user_token():
            raise HTTPException(
                status_code=401,
                detail="Connect Twitch OAuth before creating clips on Twitch",
            )

        with session_factory() as session:
            channel = session.get(Channel, channel_id)
            if channel is None:
                raise HTTPException(status_code=404, detail="Channel not found")
            login = str(channel.channel_login)
            display_name = str(channel.display_name)
            cached_broadcaster_id = channel.broadcaster_id

        try:
            broadcaster_id = await _resolve_channel_broadcaster_id(
                channel_id,
                login,
                cached_broadcaster_id,
                helix_client,
                session_factory,
            )

            if req.vod_id and req.vod_offset is not None:
                editor_id = str(helix_client.get_status().get("user_id") or "")
                if not editor_id:
                    raise HTTPException(status_code=401, detail="Authenticated Twitch user ID missing")
                return await helix_client.create_clip_from_vod(
                    broadcaster_id,
                    editor_id,
                    req.vod_id,
                    req.vod_offset,
                    float(req.duration or 30.0),
                    req.title or f"{display_name} highlight",
                )

            return await helix_client.create_clip(
                broadcaster_id,
                title=req.title,
                duration=req.duration,
            )
        except HTTPException:
            raise
        except httpx.HTTPStatusError as exc:
            _raise_twitch_http_error(exc, "Twitch clip creation failed")
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Twitch API error: {exc.__class__.__name__}",
            ) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @fastapi_app.post("/api/channels/{channel_id}/marker")
    async def create_channel_stream_marker(
        channel_id: int,
        request: Request,
        payload: StreamMarkerRequest | None = None,
    ) -> dict[str, Any]:
        session_factory = _state_session_factory(request)
        helix_client = _state_helix_client(request)
        req = payload or StreamMarkerRequest()

        if not helix_client.has_user_token():
            raise HTTPException(
                status_code=401,
                detail="Connect Twitch OAuth before placing stream markers",
            )

        with session_factory() as session:
            channel = session.get(Channel, channel_id)
            if channel is None:
                raise HTTPException(status_code=404, detail="Channel not found")
            login = str(channel.channel_login)
            cached_broadcaster_id = channel.broadcaster_id

        try:
            broadcaster_id = await _resolve_channel_broadcaster_id(
                channel_id,
                login,
                cached_broadcaster_id,
                helix_client,
                session_factory,
            )
            return await helix_client.create_stream_marker(
                broadcaster_id,
                description=req.description,
            )
        except HTTPException:
            raise
        except httpx.HTTPStatusError as exc:
            _raise_twitch_http_error(exc, "Twitch stream marker failed")
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Twitch API error: {exc.__class__.__name__}",
            ) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @fastapi_app.websocket("/ws/live")
    async def websocket_live(websocket: WebSocket) -> None:
        await websocket.accept()
        ws_clients: set[WebSocket] = websocket.app.state.ws_clients
        live_manager: LiveManager = websocket.app.state.live_manager
        ws_clients.add(websocket)
        try:
            initial_msg = json.dumps(
                {
                    "type": "monitor_status",
                    "payload": {"active_channel_ids": live_manager.active_channel_ids()},
                }
            )
            await websocket.send_text(initial_msg)
            while True:
                await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            ws_clients.discard(websocket)

    @fastapi_app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend_spa(full_path: str, request: Request) -> Response:
        if (
            full_path == "api"
            or full_path.startswith("api/")
            or full_path == "ws"
            or full_path.startswith("ws/")
        ):
            raise HTTPException(status_code=404, detail="Not found")

        settings = _state_settings(request)
        dist_dir = settings.project_root / "frontend" / "dist"
        if not dist_dir.is_dir():
            raise HTTPException(status_code=404, detail="Frontend bundle not built")

        if full_path:
            try:
                candidate = safe_resolve_under(dist_dir, full_path)
            except ValueError:
                raise HTTPException(status_code=404, detail="Not found") from None
            if candidate.is_file():
                return FileResponse(candidate)

        index_file = dist_dir / "index.html"
        if index_file.is_file():
            return FileResponse(index_file, media_type="text/html")
        raise HTTPException(status_code=404, detail="Frontend index.html not found")

    return fastapi_app


app = create_app()


if __name__ == "__main__":
    runtime_settings = get_settings()
    uvicorn.run(
        "backend.main:app",
        host=runtime_settings.host,
        port=runtime_settings.port,
        reload=False,
    )
