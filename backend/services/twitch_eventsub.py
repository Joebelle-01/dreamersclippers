"""Twitch EventSub WebSocket client for stream lifecycle and Signal #4 hype events."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Any
from urllib.parse import urlsplit

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import WebSocketException

from backend.services.twitch_helix import TwitchHelixClient

logger = logging.getLogger(__name__)

TWITCH_EVENTSUB_WS_URL = "wss://eventsub.wss.twitch.tv/ws"


def _validate_eventsub_reconnect_url(url: str) -> str:
    """Verify EventSub reconnect_url targets an authorized Twitch WSS endpoint."""
    cleaned = (url or "").strip()
    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() != "wss":
        raise ValueError("EventSub reconnect_url must use wss://")
    if parsed.username is not None or parsed.password is not None or parsed.port is not None:
        raise ValueError("EventSub reconnect_url must not contain credentials or custom ports")
    host = parsed.netloc.lower()
    if host != "eventsub.wss.twitch.tv" and not host.endswith(".twitch.tv"):
        raise ValueError(f"Unauthorized EventSub reconnect host '{host}'")
    return cleaned


class TwitchEventSubClient:
    """Manages an EventSub WebSocket session and dynamic Helix subscriptions."""

    def __init__(
        self,
        helix_client: TwitchHelixClient,
        on_notification: Callable[[str, dict[str, Any]], Awaitable[None] | None],
        *,
        on_status_change: Callable[[bool], Awaitable[None] | None] | None = None,
        ws_url: str = TWITCH_EVENTSUB_WS_URL,
        initial_backoff_sec: float = 2.0,
        max_backoff_sec: float = 45.0,
    ) -> None:
        self._helix = helix_client
        self._on_notification = on_notification
        self._on_status_change = on_status_change
        self._ws_url = ws_url
        self._initial_backoff_sec = max(0.2, float(initial_backoff_sec))
        self._max_backoff_sec = max(self._initial_backoff_sec, float(max_backoff_sec))

        self._stop_event = asyncio.Event()
        self._runner_task: asyncio.Task[None] | None = None
        self._ws: ClientConnection | None = None
        self._session_id: str | None = None
        self._connected = False
        self._desired_broadcaster_ids: list[str] = []
        self._registered_keys: set[tuple[str, str]] = set()
        self._seen_message_ids: set[str] = set()
        self._sync_lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        """Return True while an active EventSub session_welcome handshake is alive."""
        return self._connected

    async def _notify_status(self, connected: bool) -> None:
        self._connected = connected
        self._helix.set_eventsub_connected(connected)
        if self._on_status_change is not None:
            res = self._on_status_change(connected)
            if isawaitable(res):
                await res

    async def _emit_notification(self, sub_type: str, event: dict[str, Any]) -> None:
        res = self._on_notification(sub_type, event)
        if isawaitable(res):
            await res

    async def start(self) -> None:
        """Start the EventSub WebSocket background task if not already running."""
        if self._runner_task is not None and not self._runner_task.done():
            return
        self._stop_event.clear()
        self._runner_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the EventSub WebSocket client cleanly."""
        self._stop_event.set()
        ws = self._ws
        self._session_id = None
        self._registered_keys.clear()
        await self._notify_status(False)
        if ws is not None:
            try:
                await ws.close()
            except (OSError, WebSocketException):
                pass
        if self._runner_task is not None:
            self._runner_task.cancel()
            await asyncio.gather(self._runner_task, return_exceptions=True)
            self._runner_task = None

    async def sync_subscriptions(self, broadcaster_ids: list[str]) -> None:
        """Register missing EventSub WebSocket subscriptions for target broadcaster IDs."""
        clean_ids: list[str] = []
        seen: set[str] = set()
        for raw in broadcaster_ids:
            bid = str(raw).strip()
            if bid and bid not in seen:
                seen.add(bid)
                clean_ids.append(bid)
        self._desired_broadcaster_ids = clean_ids

        session_id = self._session_id
        if not session_id or not self._connected or not self._helix.has_user_token():
            return

        async with self._sync_lock:
            session_id = self._session_id
            if not session_id:
                return
            user_id = self._helix.user_id
            has_broadcaster = self._helix.has_broadcaster_scopes

            for bid in clean_ids:
                specs: list[tuple[str, str, dict[str, Any]]] = [
                    ("stream.online", "1", {"broadcaster_user_id": bid}),
                    ("stream.offline", "1", {"broadcaster_user_id": bid}),
                    ("channel.raid", "1", {"to_broadcaster_user_id": bid}),
                ]
                if has_broadcaster and user_id and bid == user_id:
                    specs.extend(
                        [
                            ("channel.hype_train.begin", "1", {"broadcaster_user_id": bid}),
                            ("channel.hype_train.progress", "1", {"broadcaster_user_id": bid}),
                            ("channel.cheer", "1", {"broadcaster_user_id": bid}),
                        ]
                    )

                for sub_type, version, condition in specs:
                    key = (sub_type, bid)
                    if key in self._registered_keys:
                        continue
                    try:
                        await self._helix.create_eventsub_subscription(
                            sub_type=sub_type,
                            version=version,
                            condition=condition,
                            session_id=session_id,
                        )
                        self._registered_keys.add(key)
                    except RuntimeError as exc:
                        err_lower = str(exc).lower()
                        if "already exists" in err_lower or "409" in err_lower:
                            self._registered_keys.add(key)
                        else:
                            logger.debug(
                                "EventSub subscription %s skipped for %s: %s",
                                sub_type,
                                bid,
                                exc,
                            )

    async def handle_frame(self, frame: str | bytes) -> str | None:
        """Process one EventSub JSON message; return reconnect_url if session_reconnect received."""
        text = frame.decode("utf-8", errors="replace") if isinstance(frame, bytes) else str(frame)
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None

        metadata = data.get("metadata") or {}
        payload = data.get("payload") or {}
        msg_id = str(metadata.get("message_id") or "")
        msg_type = str(metadata.get("message_type") or "")

        if msg_id:
            if msg_id in self._seen_message_ids:
                return None
            self._seen_message_ids.add(msg_id)
            if len(self._seen_message_ids) > 512:
                self._seen_message_ids.clear()

        if msg_type == "session_welcome":
            session_info = payload.get("session") or {}
            session_id = str(session_info.get("id") or "")
            if session_id:
                self._session_id = session_id
                self._registered_keys.clear()
                await self._notify_status(True)
                if self._desired_broadcaster_ids:
                    await self.sync_subscriptions(self._desired_broadcaster_ids)
            return None

        if msg_type == "session_keepalive":
            return None

        if msg_type == "session_reconnect":
            session_info = payload.get("session") or {}
            reconnect_url = str(session_info.get("reconnect_url") or "")
            if reconnect_url:
                try:
                    return _validate_eventsub_reconnect_url(reconnect_url)
                except ValueError as exc:
                    logger.warning("Ignoring invalid EventSub reconnect_url: %s", exc)
            return None

        if msg_type == "notification":
            sub = payload.get("subscription") or {}
            event = payload.get("event") or {}
            sub_type = str(sub.get("type") or "")
            if sub_type and isinstance(event, dict):
                await self._emit_notification(sub_type, event)
            return None

        if msg_type == "revocation":
            sub = payload.get("subscription") or {}
            logger.warning(
                "Twitch EventSub subscription revoked: %s (%s)",
                sub.get("type"),
                sub.get("status"),
            )
            return None

        return None

    async def _run_loop(self) -> None:
        backoff = self._initial_backoff_sec
        active_url = self._ws_url

        while not self._stop_event.is_set():
            if not self._helix.has_user_token():
                await self._notify_status(False)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
                except TimeoutError:
                    pass
                continue

            next_reconnect_url: str | None = None
            try:
                async with websockets.asyncio.client.connect(
                    active_url,
                    ping_interval=30.0,
                    ping_timeout=15.0,
                    close_timeout=5.0,
                    max_size=1024 * 1024,
                ) as ws:
                    self._ws = ws
                    backoff = self._initial_backoff_sec
                    active_url = self._ws_url
                    async for raw_frame in ws:
                        if self._stop_event.is_set():
                            break
                        reconnect_target = await self.handle_frame(raw_frame)
                        if reconnect_target:
                            next_reconnect_url = reconnect_target
                            break
            except asyncio.CancelledError:
                raise
            except (OSError, WebSocketException, TimeoutError) as exc:
                if self._stop_event.is_set():
                    break
                logger.warning("Twitch EventSub WebSocket dropped: %s", exc)
            finally:
                self._ws = None
                if next_reconnect_url is None:
                    self._session_id = None
                    self._registered_keys.clear()
                    await self._notify_status(False)

            if self._stop_event.is_set():
                break

            if next_reconnect_url is not None:
                active_url = next_reconnect_url
                continue

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(self._max_backoff_sec, backoff * 2.0)
