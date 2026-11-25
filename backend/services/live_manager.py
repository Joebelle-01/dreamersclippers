"""Live channel monitoring orchestrator coordinating IRC, ring buffers, and auto-clipping."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from inspect import isawaitable
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from backend.config import Settings, resolve_ffmpeg_path, resolve_ffprobe_path
from backend.models import Channel, ClipItem, ClipRead, HypeEventLog, utc_now
from backend.security import CropBox, safe_resolve_under, sanitize_chat_text, validate_crop_box
from backend.services.caption_service import generate_hook_headline
from backend.services.hype_detector import HypeDetector, HypeEvaluation
from backend.services.ring_buffer import ChannelRingBuffer
from backend.services.twitch_eventsub import TwitchEventSubClient
from backend.services.twitch_helix import TwitchHelixClient
from backend.services.twitch_irc import ChatMessage, TwitchIRCClient
from backend.services.video_renderer import generate_thumbnail, probe_video_duration
from backend.services.webcam_detector import detect_webcam

logger = logging.getLogger(__name__)

_DEFAULT_GAMEPLAY_BOX = CropBox(x=0.22, y=0.0, w=0.56, h=1.0)


@dataclass
class _ChannelConfigSnapshot:
    channel_id: int
    channel_login: str
    display_name: str
    hype_threshold: float
    pre_roll_sec: float
    post_roll_sec: float
    cooldown_sec: float
    weight_chat: float
    weight_audio: float
    weight_motion: float
    preset_layout_mode: str
    preset_split_ratio: float
    preset_webcam_json: str | None
    preset_gameplay_json: str | None
    auto_monitor_online: bool = False
    broadcaster_id: str | None = None


@dataclass
class _ChannelRowSummary:
    channel_id: int
    channel_login: str
    broadcaster_id: str | None
    auto_monitor_online: bool
    is_monitoring: bool


@dataclass
class _ChannelMonitor:
    channel_id: int
    channel_login: str
    detector: HypeDetector
    irc: TwitchIRCClient
    ring: ChannelRingBuffer
    timeline_history: deque[dict[str, float]] = field(default_factory=lambda: deque(maxlen=120))
    irc_task: asyncio.Task[None] | None = None
    eval_task: asyncio.Task[None] | None = None
    capture_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    capture_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_chat_emit_ts: float = 0.0
    last_chat_emit_count: int = 0


def _load_channel_snapshot(
    session_factory: Callable[[], Session],
    channel_id: int,
) -> _ChannelConfigSnapshot | None:
    with session_factory() as session:
        ch = session.get(Channel, channel_id)
        if ch is None:
            return None
        return _ChannelConfigSnapshot(
            channel_id=int(ch.id or channel_id),
            channel_login=str(ch.channel_login),
            display_name=str(ch.display_name),
            hype_threshold=float(ch.hype_threshold),
            pre_roll_sec=float(ch.pre_roll_sec),
            post_roll_sec=float(ch.post_roll_sec),
            cooldown_sec=float(ch.cooldown_sec),
            weight_chat=float(ch.weight_chat),
            weight_audio=float(ch.weight_audio),
            weight_motion=float(ch.weight_motion),
            preset_layout_mode=str(ch.preset_layout_mode),
            preset_split_ratio=float(ch.preset_split_ratio),
            preset_webcam_json=ch.preset_webcam_json,
            preset_gameplay_json=ch.preset_gameplay_json,
            auto_monitor_online=bool(getattr(ch, "auto_monitor_online", False)),
            broadcaster_id=getattr(ch, "broadcaster_id", None),
        )


def _parse_valid_crop_json(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        validated = validate_crop_box(parsed)
        return json.dumps(validated.model_dump())
    except (ValueError, TypeError):
        return None


class LiveManager:
    """Coordinates active live Twitch channel monitors, WS subscribers, and auto-clipping."""

    def __init__(
        self,
        settings: Settings,
        session_factory: Callable[[], Session],
        on_event: Callable[..., Any] | None = None,
        helix_client: TwitchHelixClient | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self._on_event = on_event
        self.helix_client = helix_client
        self._monitors: dict[int, _ChannelMonitor] = {}
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._helix_poll_task: asyncio.Task[None] | None = None
        self._helix_stop_event = asyncio.Event()
        self._eventsub: TwitchEventSubClient | None = None
        self._last_viewer_counts: dict[int, int] = {}
        self._seen_clip_ids: dict[int, set[str]] = {}

    def active_channel_ids(self) -> list[int]:
        """Return sorted list of currently monitored channel IDs."""
        return sorted(self._monitors.keys())

    def subscribe(self, maxsize: int = 256) -> asyncio.Queue[dict[str, Any]]:
        """Register an in-memory subscriber queue for live JSON events."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove a previously registered subscriber queue."""
        self._subscribers.discard(queue)

    async def _broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        envelope = {"type": event_type, "payload": payload}
        for queue in list(self._subscribers):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(envelope)

        if self._on_event is None:
            return
        try:
            res = self._on_event(event_type, payload)
            if isawaitable(res):
                await res
        except TypeError:
            res = self._on_event(envelope)
            if isawaitable(res):
                await res
        except Exception:
            logger.exception("LiveManager event handler failed for '%s'", event_type)

    async def _handle_chat_message(self, monitor: _ChannelMonitor, msg: ChatMessage) -> None:
        monitor.detector.ingest_chat(msg.author, msg.text, ts=msg.ts)
        now = time.monotonic()
        if now - monitor.last_chat_emit_ts >= 1.0:
            monitor.last_chat_emit_ts = now
            monitor.last_chat_emit_count = 0
        if monitor.last_chat_emit_count < 15:
            monitor.last_chat_emit_count += 1
            await self._broadcast(
                "chat_burst",
                {
                    "channel_id": monitor.channel_id,
                    "channel_login": monitor.channel_login,
                    "author": msg.author,
                    "text": msg.text,
                    "emotes": msg.emotes,
                    "ts": msg.ts,
                },
            )

    def _sync_detector_settings(self, monitor: _ChannelMonitor) -> _ChannelConfigSnapshot | None:
        snap = _load_channel_snapshot(self.session_factory, monitor.channel_id)
        if snap is None:
            return None
        monitor.detector.threshold = snap.hype_threshold
        monitor.detector.cooldown_sec = snap.cooldown_sec
        monitor.detector.weight_chat = snap.weight_chat
        monitor.detector.weight_audio = snap.weight_audio
        monitor.detector.weight_motion = snap.weight_motion
        return snap

    async def _capture_and_create_clip(
        self,
        monitor: _ChannelMonitor,
        evaluation: HypeEvaluation,
        *,
        source_reason: str,
        pre_roll_override: float | None = None,
        post_roll_override: float | None = None,
        clip_title_prefix: str = "Live Hype",
    ) -> ClipItem:
        ffmpeg_path = self.settings.ffmpeg_path or resolve_ffmpeg_path()
        ffprobe_path = self.settings.ffprobe_path or resolve_ffprobe_path(ffmpeg_path)
        if not ffmpeg_path or not ffprobe_path:
            raise RuntimeError("FFmpeg and ffprobe executables are required to capture clips")

        self.settings.ensure_data_dirs()
        snap = _load_channel_snapshot(self.session_factory, monitor.channel_id)
        if snap is None:
            raise ValueError(f"Channel {monitor.channel_id} no longer exists")

        pre_roll = (
            snap.pre_roll_sec
            if pre_roll_override is None
            else max(5.0, float(pre_roll_override))
        )
        post_roll = snap.post_roll_sec if post_roll_override is None else max(0.0, post_roll_override)
        clip_id = str(uuid4())
        raw_clip_path = safe_resolve_under(self.settings.clips_dir, f"{clip_id}.mp4")
        thumb_path = safe_resolve_under(self.settings.thumbs_dir, f"{clip_id}.jpg")

        async with monitor.capture_lock:
            await monitor.ring.snapshot_clip(pre_roll, post_roll, raw_clip_path)

        duration_sec = await asyncio.to_thread(probe_video_duration, raw_clip_path, ffprobe_path)
        if duration_sec <= 0.0:
            duration_sec = max(1.0, pre_roll + post_roll)

        await asyncio.to_thread(
            generate_thumbnail,
            raw_clip_path,
            thumb_path,
            ffmpeg_path,
            min(1.0, duration_sec * 0.25),
        )

        # Honor saved channel webcam preset if valid; otherwise run Haar+edge webcam detection.
        preset_webcam_json = _parse_valid_crop_json(snap.preset_webcam_json)
        preset_gameplay_json = _parse_valid_crop_json(snap.preset_gameplay_json)
        if preset_webcam_json is not None:
            layout_mode = snap.preset_layout_mode
            webcam_box_json: str | None = preset_webcam_json
            gameplay_box_json = preset_gameplay_json or json.dumps(_DEFAULT_GAMEPLAY_BOX.model_dump())
            webcam_confidence = 1.0
        else:
            detection = await asyncio.to_thread(detect_webcam, raw_clip_path, ffmpeg_path)
            layout_mode = str(detection.layout_mode)
            webcam_confidence = float(detection.confidence)
            webcam_box_json = (
                json.dumps(detection.webcam_box.model_dump())
                if detection.webcam_box is not None
                else None
            )
            gameplay_box_json = json.dumps(detection.gameplay_box.model_dump())

        # Shift timeline samples relative to clip start [0..duration_sec].
        now_ts = time.time()
        window_start_ts = now_ts - duration_sec
        local_timeline = [
            {
                "t": round(max(0.0, pt["t"] - window_start_ts), 2),
                "chat": pt["chat"],
                "audio": pt["audio"],
                "motion": pt["motion"],
                "fused": pt["fused"],
            }
            for pt in list(monitor.timeline_history)
            if pt["t"] >= window_start_ts - 1.0
        ]

        token_hint = f" ({', '.join(evaluation.top_tokens[:3])})" if evaluation.top_tokens else ""
        clip_title = f"{snap.display_name} {clip_title_prefix}{token_hint}"
        default_hook = generate_hook_headline(
            monitor.channel_login,
            clip_title,
            mode="context",
        )
        clip_row = ClipItem(
            id=clip_id,
            channel_id=monitor.channel_id,
            channel_login=monitor.channel_login,
            source_type="live",
            source_ref=f"https://www.twitch.tv/{monitor.channel_login}",
            title=clip_title,
            status="pending_review",
            hype_score=float(evaluation.fused_score),
            trigger_reason=source_reason,
            trigger_chat_samples_json=json.dumps(evaluation.recent_samples),
            hype_timeline_json=json.dumps(local_timeline),
            raw_video_path=str(raw_clip_path),
            thumbnail_path=str(thumb_path),
            duration_sec=float(duration_sec),
            trim_start_sec=0.0,
            trim_end_sec=float(duration_sec),
            layout_mode=layout_mode,
            split_ratio=snap.preset_split_ratio,
            webcam_box_json=webcam_box_json,
            gameplay_box_json=gameplay_box_json,
            webcam_confidence=webcam_confidence,
            normalize_audio=True,
            caption_position="lower_safe",
            hook_enabled=True,
            hook_text=default_hook,
            hook_style="tiktok_white",
            hook_duration_mode="full",
        )

        with self.session_factory() as session:
            session.add(clip_row)
            session.commit()
            session.refresh(clip_row)
            clip_payload = ClipRead.from_orm_row(clip_row).model_dump(mode="json")

        await self._broadcast("clip_created", clip_payload)
        return clip_row

    async def _eval_loop(self, monitor: _ChannelMonitor) -> None:
        tick_counter = 0
        try:
            while monitor.channel_id in self._monitors:
                await asyncio.sleep(1.0)
                if monitor.channel_id not in self._monitors:
                    break

                tick_counter += 1
                if tick_counter % 3 == 0:
                    self._sync_detector_settings(monitor)

                now = time.time()
                ev = monitor.detector.evaluate(now=now)
                monitor.timeline_history.append(
                    {
                        "t": now,
                        "chat": ev.chat_score,
                        "audio": ev.audio_score,
                        "motion": ev.motion_score,
                        "fused": ev.fused_score,
                    }
                )

                await self._broadcast(
                    "hype_tick",
                    {
                        "channel_id": monitor.channel_id,
                        "channel_login": monitor.channel_login,
                        "t": round(now, 3),
                        "chat_score": ev.chat_score,
                        "audio_score": ev.audio_score,
                        "motion_score": ev.motion_score,
                        "fused_score": ev.fused_score,
                        "threshold": monitor.detector.threshold,
                        "unique_chatters": ev.unique_chatters,
                        "top_tokens": ev.top_tokens,
                        "weights_mode": ev.weights_mode,
                        "should_trigger": ev.should_trigger,
                    },
                )

                if ev.should_trigger:
                    with self.session_factory() as session:
                        session.add(
                            HypeEventLog(
                                channel_id=monitor.channel_id,
                                timestamp=utc_now(),
                                chat_score=ev.chat_score,
                                audio_score=ev.audio_score,
                                motion_score=ev.motion_score,
                                fused_score=ev.fused_score,
                                note=ev.reason,
                            )
                        )
                        session.commit()

                    if monitor.ring.has_segments():
                        task = asyncio.create_task(
                            self._run_auto_capture(monitor, ev),
                        )
                        monitor.capture_tasks.add(task)
                        task.add_done_callback(monitor.capture_tasks.discard)
        except asyncio.CancelledError:
            raise

    async def _run_auto_capture(self, monitor: _ChannelMonitor, ev: HypeEvaluation) -> None:
        try:
            await self._capture_and_create_clip(
                monitor,
                ev,
                source_reason=ev.reason,
                post_roll_override=None,
                clip_title_prefix="Auto Hype",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Auto-capture failed for #%s: %s", monitor.channel_login, exc)

    async def start_monitor(self, channel_id: int) -> None:
        """Start Twitch IRC + ring buffer monitoring for a channel."""
        async with self._lifecycle_lock:
            if channel_id in self._monitors:
                return

            snap = _load_channel_snapshot(self.session_factory, channel_id)
            if snap is None:
                raise ValueError(f"Channel {channel_id} not found")

            self.settings.ensure_data_dirs()
            detector = HypeDetector(
                channel_login=snap.channel_login,
                threshold=snap.hype_threshold,
                weight_chat=snap.weight_chat,
                weight_audio=snap.weight_audio,
                weight_motion=snap.weight_motion,
                cooldown_sec=snap.cooldown_sec,
            )

            monitor_holder: list[_ChannelMonitor] = []

            def _on_ring_error(err_msg: str) -> None:
                err_task = asyncio.create_task(
                    self._broadcast(
                        "monitor_status",
                        {
                            "channel_id": snap.channel_id,
                            "channel_login": snap.channel_login,
                            "is_monitoring": snap.channel_id in self._monitors,
                            "active_channel_ids": self.active_channel_ids(),
                            "status": "reconnecting",
                            "error": err_msg,
                        },
                    )
                )
                self._bg_tasks.add(err_task)
                err_task.add_done_callback(self._bg_tasks.discard)

            ring = ChannelRingBuffer(
                snap.channel_login,
                self.settings,
                on_audio_rms=detector.ingest_audio_rms,
                on_motion=detector.ingest_motion,
                on_error=_on_ring_error,
            )

            async def _on_irc_msg(msg: ChatMessage) -> None:
                if monitor_holder:
                    await self._handle_chat_message(monitor_holder[0], msg)

            irc = TwitchIRCClient(snap.channel_login, _on_irc_msg)
            monitor = _ChannelMonitor(
                channel_id=snap.channel_id,
                channel_login=snap.channel_login,
                detector=detector,
                irc=irc,
                ring=ring,
            )
            monitor_holder.append(monitor)

            await ring.start()
            self._monitors[snap.channel_id] = monitor
            monitor.irc_task = asyncio.create_task(irc.run())
            monitor.eval_task = asyncio.create_task(self._eval_loop(monitor))

            with self.session_factory() as session:
                db_ch = session.get(Channel, snap.channel_id)
                if db_ch is not None:
                    db_ch.is_monitoring = True
                    session.add(db_ch)
                    session.commit()

            await self._broadcast(
                "monitor_status",
                {
                    "channel_id": snap.channel_id,
                    "channel_login": snap.channel_login,
                    "is_monitoring": True,
                    "active_channel_ids": self.active_channel_ids(),
                    "status": "monitoring",
                },
            )

    async def stop_monitor(self, channel_id: int) -> None:
        """Stop monitoring a channel and clean up its IRC and FFmpeg/Streamlink processes."""
        async with self._lifecycle_lock:
            monitor = self._monitors.pop(channel_id, None)

        if monitor is not None:
            for task in list(monitor.capture_tasks):
                task.cancel()
            for task in (monitor.eval_task, monitor.irc_task):
                if task is not None:
                    task.cancel()

            await monitor.irc.stop()
            await monitor.ring.stop()

            all_tasks = [
                t
                for t in [*monitor.capture_tasks, monitor.eval_task, monitor.irc_task]
                if t is not None
            ]
            if all_tasks:
                await asyncio.gather(*all_tasks, return_exceptions=True)

        login = monitor.channel_login if monitor is not None else ""
        with self.session_factory() as session:
            db_ch = session.get(Channel, channel_id)
            if db_ch is not None:
                login = str(db_ch.channel_login)
                if db_ch.is_monitoring:
                    db_ch.is_monitoring = False
                    session.add(db_ch)
                    session.commit()

        self._seen_clip_ids.pop(channel_id, None)

        await self._broadcast(
            "monitor_status",
            {
                "channel_id": channel_id,
                "channel_login": login,
                "is_monitoring": False,
                "active_channel_ids": self.active_channel_ids(),
                "status": "stopped",
            },
        )

    def _load_all_channel_summaries(self) -> list[_ChannelRowSummary]:
        with self.session_factory() as session:
            rows = session.exec(select(Channel)).all()
            return [
                _ChannelRowSummary(
                    channel_id=int(ch.id or 0),
                    channel_login=str(ch.channel_login).lower(),
                    broadcaster_id=getattr(ch, "broadcaster_id", None),
                    auto_monitor_online=bool(getattr(ch, "auto_monitor_online", False)),
                    is_monitoring=bool(ch.is_monitoring),
                )
                for ch in rows
                if ch.id is not None
            ]

    async def _ensure_broadcaster_ids(
        self, summaries: list[_ChannelRowSummary]
    ) -> list[_ChannelRowSummary]:
        if self.helix_client is None or not self.helix_client.has_app_credentials():
            return summaries
        missing = [s.channel_login for s in summaries if not s.broadcaster_id]
        if not missing:
            return summaries

        users = await self.helix_client.get_users(missing)
        id_by_login = {
            str(u.get("login") or "").lower(): str(u.get("id") or "")
            for u in users
            if u.get("login") and u.get("id")
        }
        if not id_by_login:
            return summaries

        with self.session_factory() as session:
            for item in summaries:
                resolved = id_by_login.get(item.channel_login)
                if resolved and not item.broadcaster_id:
                    db_ch = session.get(Channel, item.channel_id)
                    if db_ch is not None:
                        db_ch.broadcaster_id = resolved
                        session.add(db_ch)
                    item.broadcaster_id = resolved
            session.commit()
        return summaries

    async def _poll_helix_once(self) -> None:
        if self.helix_client is None or not self.helix_client.has_app_credentials():
            return

        summaries = self._load_all_channel_summaries()
        if not summaries:
            return
        summaries = await self._ensure_broadcaster_ids(summaries)

        streams = await self.helix_client.get_streams([s.channel_login for s in summaries])
        for item in summaries:
            stream = streams.get(item.channel_login)
            is_live = stream is not None
            game_name = str(stream.get("game_name") or "") if stream else ""
            stream_title = (
                sanitize_chat_text(str(stream.get("title") or ""), max_len=140)
                if stream
                else ""
            )
            viewer_count = int(stream.get("viewer_count") or 0) if stream else 0

            await self._broadcast(
                "channel_metadata",
                {
                    "channel_id": item.channel_id,
                    "channel_login": item.channel_login,
                    "is_live": is_live,
                    "game_name": game_name,
                    "stream_title": stream_title,
                    "viewer_count": viewer_count,
                    "broadcaster_id": item.broadcaster_id,
                },
            )

            prev_viewers = self._last_viewer_counts.get(item.channel_id)
            if is_live:
                monitor = self._monitors.get(item.channel_id)
                if (
                    monitor is not None
                    and prev_viewers is not None
                    and prev_viewers >= 10
                    and viewer_count >= prev_viewers * 1.20
                ):
                    monitor.detector.ingest_external_boost(20.0, "Viewer spike")
                self._last_viewer_counts[item.channel_id] = viewer_count
            else:
                self._last_viewer_counts[item.channel_id] = 0

            if item.auto_monitor_online:
                if is_live and item.channel_id not in self._monitors:
                    with suppress(Exception):
                        await self.start_monitor(item.channel_id)
                elif not is_live and item.channel_id in self._monitors:
                    with suppress(Exception):
                        await self.stop_monitor(item.channel_id)

        # Signal #4: Viewer clip velocity for actively monitored channels
        summary_by_id = {item.channel_id: item for item in summaries}
        started_at = (datetime.now(UTC) - timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for ch_id, monitor in list(self._monitors.items()):
            summary = summary_by_id.get(ch_id)
            bid = summary.broadcaster_id if summary is not None else None
            if not bid:
                continue
            try:
                clips = await self.helix_client.get_clips(bid, started_at=started_at, first=20)
            except RuntimeError:
                continue
            clip_ids = {str(c.get("id") or "") for c in clips if c.get("id")}
            seen = self._seen_clip_ids.get(ch_id)
            new_ids = clip_ids if seen is None else (clip_ids - seen)
            merged = (seen or set()) | clip_ids
            if len(merged) > 200:
                merged = set(list(merged)[-200:])
            self._seen_clip_ids[ch_id] = merged
            if len(new_ids) >= 2:
                monitor.detector.ingest_external_boost(35.0, "Viewer clip spike")

    async def _helix_poll_loop(self) -> None:
        try:
            while not self._helix_stop_event.is_set():
                try:
                    await self._poll_helix_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("Helix metadata poll skipped/failed: %s", exc)
                try:
                    await asyncio.wait_for(self._helix_stop_event.wait(), timeout=25.0)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    async def _handle_eventsub_notification(
        self, sub_type: str, event: dict[str, Any]
    ) -> None:
        target_bid = str(
            event.get("broadcaster_user_id")
            or event.get("to_broadcaster_user_id")
            or ""
        ).strip()
        target_login = str(
            event.get("broadcaster_user_login")
            or event.get("to_broadcaster_user_login")
            or ""
        ).strip().lower()
        if not target_bid and not target_login:
            return

        summaries = self._load_all_channel_summaries()
        matched = [
            s
            for s in summaries
            if (target_bid and s.broadcaster_id == target_bid)
            or (target_login and s.channel_login == target_login)
        ]
        if not matched:
            return

        for item in matched:
            monitor = self._monitors.get(item.channel_id)
            if sub_type == "stream.online":
                await self._broadcast(
                    "channel_metadata",
                    {
                        "channel_id": item.channel_id,
                        "channel_login": item.channel_login,
                        "is_live": True,
                        "game_name": str(event.get("category_name") or ""),
                        "stream_title": "",
                        "viewer_count": self._last_viewer_counts.get(item.channel_id, 0),
                        "broadcaster_id": item.broadcaster_id,
                    },
                )
                if item.auto_monitor_online and item.channel_id not in self._monitors:
                    with suppress(Exception):
                        await self.start_monitor(item.channel_id)
            elif sub_type == "stream.offline":
                await self._broadcast(
                    "channel_metadata",
                    {
                        "channel_id": item.channel_id,
                        "channel_login": item.channel_login,
                        "is_live": False,
                        "game_name": "",
                        "stream_title": "",
                        "viewer_count": 0,
                        "broadcaster_id": item.broadcaster_id,
                    },
                )
                if item.auto_monitor_online and item.channel_id in self._monitors:
                    with suppress(Exception):
                        await self.stop_monitor(item.channel_id)
            elif sub_type == "channel.raid" and monitor is not None:
                monitor.detector.ingest_external_boost(30.0, "Channel raid")
            elif (
                sub_type in ("channel.hype_train.begin", "channel.hype_train.progress")
                and monitor is not None
            ):
                monitor.detector.ingest_external_boost(40.0, "Hype Train")
            elif sub_type == "channel.cheer" and monitor is not None:
                monitor.detector.ingest_external_boost(25.0, "Cheer burst")

    async def _handle_eventsub_status(self, connected: bool) -> None:
        await self._broadcast(
            "monitor_status",
            {
                "active_channel_ids": self.active_channel_ids(),
                "eventsub_connected": connected,
                "status": "eventsub_connected" if connected else "eventsub_disconnected",
            },
        )

    async def start_background_services(self) -> None:
        """Start periodic Helix metadata polling and EventSub listener when configured."""
        if self.helix_client is None:
            return
        self._helix_stop_event.clear()
        if self._helix_poll_task is None or self._helix_poll_task.done():
            self._helix_poll_task = asyncio.create_task(self._helix_poll_loop())
        await self.refresh_helix_subscriptions()

    async def refresh_helix_subscriptions(self) -> None:
        """Sync Twitch broadcaster IDs and EventSub subscriptions with current credentials/channels."""
        if self.helix_client is None:
            return

        summaries = self._load_all_channel_summaries()
        if self.helix_client.has_app_credentials() and summaries:
            with suppress(Exception):
                summaries = await self._ensure_broadcaster_ids(summaries)

        broadcaster_ids = [s.broadcaster_id for s in summaries if s.broadcaster_id]

        if self.helix_client.has_user_token():
            if self._eventsub is None:
                self._eventsub = TwitchEventSubClient(
                    self.helix_client,
                    self._handle_eventsub_notification,
                    on_status_change=self._handle_eventsub_status,
                )
            await self._eventsub.start()
            await self._eventsub.sync_subscriptions(broadcaster_ids)
        else:
            if self._eventsub is not None:
                await self._eventsub.stop()
                self._eventsub = None
            self.helix_client.set_eventsub_connected(False)

        if self.helix_client.has_app_credentials() and summaries:
            with suppress(Exception):
                await self._poll_helix_once()

    async def clip_now(self, channel_id: int, duration_sec: float | None = None) -> ClipItem:
        """Immediately snapshot a clip from the active channel ring buffer."""
        monitor = self._monitors.get(channel_id)
        if monitor is None:
            raise RuntimeError(f"Channel {channel_id} is not actively monitored")
        if not monitor.ring.has_segments():
            raise RuntimeError(
                f"No buffered video segments available yet for #{monitor.channel_login}"
            )

        self._sync_detector_settings(monitor)
        ev = monitor.detector.evaluate(now=time.time())
        return await self._capture_and_create_clip(
            monitor,
            ev,
            source_reason="Manual Clip Now trigger",
            pre_roll_override=duration_sec,
            post_roll_override=0.0,
            clip_title_prefix="Manual Clip",
        )

    async def shutdown(self) -> None:
        """Stop all active monitors, Helix polling, EventSub, and clear subscriber queues."""
        self._helix_stop_event.set()
        if self._helix_poll_task is not None:
            self._helix_poll_task.cancel()
            await asyncio.gather(self._helix_poll_task, return_exceptions=True)
            self._helix_poll_task = None
        if self._eventsub is not None:
            await self._eventsub.stop()
            self._eventsub = None

        ids = list(self._monitors.keys())
        if ids:
            await asyncio.gather(
                *(self.stop_monitor(cid) for cid in ids),
                return_exceptions=True,
            )
        for task in list(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()
        self._subscribers.clear()
