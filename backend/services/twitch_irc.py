"""Async Twitch IRC WebSocket client for anonymous live chat ingestion."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from inspect import isawaitable

import websockets
from pydantic import BaseModel, Field
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import WebSocketException

from backend.security import sanitize_chat_text, validate_twitch_channel

logger = logging.getLogger(__name__)

TWITCH_IRC_WS_URL = "wss://irc-ws.chat.twitch.tv:443"

_PRIVMSG_RE = re.compile(
    r"^(?:@(?P<tags>\S+)\s+)?"
    r":(?P<prefix>[^\s!]+)(?:![^\s@]+@[^\s]+)?\s+"
    r"PRIVMSG\s+#(?P<channel>[a-zA-Z0-9_]+)\s+:(?P<text>.*)$"
)
_PING_RE = re.compile(r"^PING(?:\s+(?P<payload>.+))?$")

_TAG_UNESCAPE = {
    "s": " ",
    ":": ";",
    "\\": "\\",
    "r": "\r",
    "n": "\n",
}


class ChatMessage(BaseModel):
    """Parsed and sanitized Twitch IRC PRIVMSG payload."""

    author: str
    text: str
    emotes: list[str] = Field(default_factory=list)
    ts: float
    channel: str = ""


def generate_anonymous_nick() -> str:
    """Return an anonymous Twitch read-only IRC nick (`justinfan<5-digits>`)."""
    return f"justinfan{10000 + secrets.randbelow(90000)}"


def _unescape_tag_value(raw: str) -> str:
    if "\\" not in raw:
        return raw
    out: list[str] = []
    i = 0
    length = len(raw)
    while i < length:
        ch = raw[i]
        if ch == "\\" and i + 1 < length:
            nxt = raw[i + 1]
            out.append(_TAG_UNESCAPE.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _parse_irc_tags(tags_raw: str | None) -> dict[str, str]:
    if not tags_raw:
        return {}
    parsed: dict[str, str] = {}
    for pair in tags_raw.split(";"):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        parsed[key] = _unescape_tag_value(value)
    return parsed


def _extract_emotes(emotes_tag: str | None, raw_text: str) -> list[str]:
    if not emotes_tag:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for entry in emotes_tag.split("/"):
        if ":" not in entry:
            continue
        emote_id, ranges_str = entry.split(":", 1)
        emote_id = emote_id.strip()
        if not emote_id:
            continue
        added = False
        for span in ranges_str.split(","):
            if "-" not in span:
                continue
            start_s, end_s = span.split("-", 1)
            try:
                start_idx = int(start_s)
                end_idx = int(end_s)
            except ValueError:
                continue
            if 0 <= start_idx <= end_idx < len(raw_text):
                token = sanitize_chat_text(raw_text[start_idx : end_idx + 1], max_len=64)
                if token and token not in seen:
                    seen.add(token)
                    result.append(token)
                added = True
                break
        if not added and emote_id not in seen:
            seen.add(emote_id)
            result.append(emote_id)
    return result


def build_pong_response(line: str) -> str | None:
    """Return a PONG line if `line` is an IRC PING command, otherwise None."""
    stripped = line.strip("\r\n")
    match = _PING_RE.match(stripped)
    if not match:
        return None
    payload = match.group("payload")
    if payload:
        return f"PONG {payload}"
    return "PONG :tmi.twitch.tv"


def parse_irc_line(
    raw_line: str,
    expected_channel: str | None = None,
    now: float | None = None,
) -> ChatMessage | None:
    """Parse one IRC line into a sanitized ChatMessage if it is a matching PRIVMSG."""
    line = raw_line.rstrip("\r\n")
    if not line:
        return None
    match = _PRIVMSG_RE.match(line)
    if not match:
        return None

    channel_login = match.group("channel").lower()
    if expected_channel is not None and channel_login != expected_channel.lower():
        return None

    tags = _parse_irc_tags(match.group("tags"))
    raw_text = match.group("text")
    if raw_text.startswith("\x01ACTION ") and raw_text.endswith("\x01"):
        raw_text = raw_text[8:-1]

    emotes = _extract_emotes(tags.get("emotes"), raw_text)
    clean_text = sanitize_chat_text(raw_text, max_len=240)
    if not clean_text:
        return None

    raw_author = tags.get("display-name") or match.group("prefix")
    clean_author = sanitize_chat_text(raw_author, max_len=64) or sanitize_chat_text(
        match.group("prefix"), max_len=64
    )
    if not clean_author:
        return None

    ts = float(now if now is not None else time.time())
    return ChatMessage(
        author=clean_author,
        text=clean_text,
        emotes=emotes,
        ts=ts,
        channel=channel_login,
    )


class TwitchIRCClient:
    """Resilient anonymous Twitch IRC client over TLS WebSocket."""

    def __init__(
        self,
        channel_login: str,
        on_message: Callable[[ChatMessage], Awaitable[None] | None],
        *,
        ws_url: str = TWITCH_IRC_WS_URL,
        initial_backoff_sec: float = 1.0,
        max_backoff_sec: float = 30.0,
    ) -> None:
        self.channel_login = validate_twitch_channel(channel_login)
        self._on_message = on_message
        self._ws_url = ws_url
        self._initial_backoff_sec = max(0.1, float(initial_backoff_sec))
        self._max_backoff_sec = max(self._initial_backoff_sec, float(max_backoff_sec))
        self._stop_event = asyncio.Event()
        self._ws: ClientConnection | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        """Return True while an active authenticated JOIN session is open."""
        return self._connected

    async def _emit(self, message: ChatMessage) -> None:
        result = self._on_message(message)
        if isawaitable(result):
            await result

    async def _handshake(self, ws: ClientConnection) -> None:
        nick = generate_anonymous_nick()
        await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")
        await ws.send("PASS SCHMOOPIIE\r\n")
        await ws.send(f"NICK {nick}\r\n")
        await ws.send(f"JOIN #{self.channel_login}\r\n")

    async def _consume_connection(self, ws: ClientConnection) -> None:
        self._ws = ws
        await self._handshake(ws)
        self._connected = True
        try:
            async for raw_frame in ws:
                if self._stop_event.is_set():
                    break
                frame_text = (
                    raw_frame.decode("utf-8", errors="replace")
                    if isinstance(raw_frame, bytes)
                    else str(raw_frame)
                )
                for line in frame_text.splitlines():
                    pong = build_pong_response(line)
                    if pong is not None:
                        await ws.send(f"{pong}\r\n")
                        continue
                    msg = parse_irc_line(line, expected_channel=self.channel_login)
                    if msg is not None:
                        await self._emit(msg)
        finally:
            self._connected = False
            self._ws = None

    async def run(self) -> None:
        """Connect to Twitch IRC and stream chat messages until stopped."""
        backoff = self._initial_backoff_sec
        while not self._stop_event.is_set():
            try:
                async with websockets.asyncio.client.connect(
                    self._ws_url,
                    ping_interval=60.0,
                    ping_timeout=20.0,
                    close_timeout=5.0,
                    max_size=1024 * 1024,
                ) as ws:
                    backoff = self._initial_backoff_sec
                    await self._consume_connection(ws)
            except asyncio.CancelledError:
                raise
            except (OSError, WebSocketException, TimeoutError) as exc:
                if self._stop_event.is_set():
                    break
                logger.warning(
                    "Twitch IRC connection dropped for #%s: %s; reconnecting in %.1fs",
                    self.channel_login,
                    exc,
                    backoff,
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(self._max_backoff_sec, backoff * 2.0)

    async def stop(self) -> None:
        """Stop the IRC client and close any active WebSocket cleanly."""
        self._stop_event.set()
        ws = self._ws
        self._connected = False
        if ws is not None:
            try:
                await ws.close()
            except (OSError, WebSocketException):
                pass
