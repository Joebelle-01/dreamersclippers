"""Unit and integration tests for Twitch Helix OAuth/API, CDN SSRF checks, EventSub, and Signal #4."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import websockets.asyncio.server as ws_server

from backend.config import Settings
from backend.db import get_session_factory, init_db
from backend.models import Channel
from backend.services.hype_detector import HypeDetector
from backend.services.live_manager import LiveManager, _ChannelMonitor
from backend.services.ring_buffer import ChannelRingBuffer
from backend.services.twitch_eventsub import (
    TwitchEventSubClient,
    _validate_eventsub_reconnect_url,
)
from backend.services.twitch_helix import (
    TwitchHelixClient,
    import_twitch_clip_to_review,
    normalize_clip_url_and_slug,
    validate_twitch_cdn_url,
)
from backend.services.twitch_irc import TwitchIRCClient


class _MockTransport(httpx.AsyncBaseTransport):
    """Deterministic async HTTP transport mocking Twitch OAuth, Helix, and CDN responses."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._handler(request)


def _patch_httpx_async_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    transport = _MockTransport(handler)
    real_client = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("backend.services.twitch_helix.httpx.AsyncClient", _factory)


def _make_sample_mp4(path: Path, ffmpeg_path: str) -> bytes:
    proc = subprocess.run(
        [
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=640x360:r=24:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(path),
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return path.read_bytes()


def test_twitch_cdn_and_clip_url_ssrf_guards() -> None:
    assert (
        validate_twitch_cdn_url("https://clips-media-assets2.twitch.tv/AT-cm%7C123.mp4")
        == "https://clips-media-assets2.twitch.tv/AT-cm%7C123.mp4"
    )
    assert (
        validate_twitch_cdn_url("https://production.assets.clips.ttvnw.net/clip.mp4")
        == "https://production.assets.clips.ttvnw.net/clip.mp4"
    )
    assert (
        validate_twitch_cdn_url("https://d2e2de1etea730.cloudfront.net/clip.mp4")
        == "https://d2e2de1etea730.cloudfront.net/clip.mp4"
    )

    for bad_cdn in (
        "http://clips-media-assets2.twitch.tv/clip.mp4",
        "https://127.0.0.1/clip.mp4",
        "https://evil.com/clip.mp4",
        "https://twitch.tv.evil.com/clip.mp4",
        "https://user:pass@clips-media-assets2.twitch.tv/clip.mp4",  # pragma: allowlist secret
        "https://clips-media-assets2.twitch.tv:8443/clip.mp4",
        "file:///etc/passwd",
    ):
        with pytest.raises(ValueError):
            validate_twitch_cdn_url(bad_cdn)

    canonical, slug = normalize_clip_url_and_slug("https://clips.twitch.tv/AwesomeClipSlug123?tt_medium=x")
    assert canonical == "https://clips.twitch.tv/AwesomeClipSlug123"
    assert slug == "AwesomeClipSlug123"

    canonical2, slug2 = normalize_clip_url_and_slug("AwesomeClipSlug123")
    assert canonical2 == "https://clips.twitch.tv/AwesomeClipSlug123"
    assert slug2 == "AwesomeClipSlug123"

    with pytest.raises(ValueError):
        normalize_clip_url_and_slug("http://127.0.0.1:8000/clip/evil")


def test_app_credentials_and_oauth_csrf_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_data_dirs()
    calls: list[tuple[str, str]] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        url = str(request.url)
        if url == "https://id.twitch.tv/oauth2/token":
            body = request.content.decode("utf-8")
            form = parse_qs(body)
            grant = form.get("grant_type", [""])[0]
            if grant == "client_credentials":
                return httpx.Response(
                    200,
                    json={"access_token": "mock_app_tok_1", "expires_in": 3600, "token_type": "bearer"},
                )
            if grant == "authorization_code":
                return httpx.Response(
                    200,
                    json={
                        "access_token": "mock_user_tok_1",
                        "refresh_token": "mock_refresh_1",
                        "expires_in": 3600,
                        "scope": ["clips:edit", "channel:manage:clips", "channel:read:hype_train"],
                    },
                )
        if url == "https://id.twitch.tv/oauth2/validate":
            return httpx.Response(
                200,
                json={
                    "client_id": "abcd1234efgh5678",
                    "login": "Shroud",
                    "user_id": "37402112",
                    "scopes": ["clips:edit", "channel:manage:clips", "channel:read:hype_train"],
                },
            )
        return httpx.Response(404, json={"message": "Not found"})

    _patch_httpx_async_client(monkeypatch, _handler)
    client = TwitchHelixClient(settings)

    async def _run() -> None:
        status = await client.save_app_credentials("abcd1234efgh5678", "super_secret_val_99")
        assert status["configured"] is True
        assert status["app_token_ready"] is True
        assert status["client_id_masked"] == "abcd...5678"
        dumped = json.dumps(status)
        assert "super_secret_val_99" not in dumped
        assert "mock_app_tok_1" not in dumped

        auth_url, state = client.build_oauth_authorize_url("broadcaster", client.redirect_uri)
        parsed = urlsplit(auth_url)
        qs = parse_qs(parsed.query)
        assert qs["state"][0] == state
        assert "channel:manage:clips" in qs["scope"][0]
        assert "channel:read:hype_train" in qs["scope"][0]

        # CSRF invalid state must be rejected
        with pytest.raises(ValueError):
            await client.handle_oauth_callback("auth_code_x", "tampered_state", client.redirect_uri)

        # Valid single-use callback succeeds
        connected = await client.handle_oauth_callback("auth_code_1", state, client.redirect_uri)
        assert connected["user_connected"] is True
        assert connected["user_login"] == "shroud"
        assert connected["user_id"] == "37402112"
        assert connected["has_broadcaster_scopes"] is True
        assert "mock_user_tok_1" not in json.dumps(connected)

        # Reusing the same state is blocked (single-use)
        with pytest.raises(ValueError):
            await client.handle_oauth_callback("auth_code_2", state, client.redirect_uri)

        disconnected = client.disconnect_user()
        assert disconnected["user_connected"] is False
        assert disconnected["user_login"] is None

    asyncio.run(_run())


def test_helix_api_methods_token_refresh_and_clip_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_data_dirs()
    init_db(settings.db_path)
    session_factory = get_session_factory(settings.db_path)

    sample_mp4_bytes = _make_sample_mp4(tmp_path / "sample_src.mp4", settings.ffmpeg_path)
    user_token_version = {"n": 1}

    async def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://id.twitch.tv/oauth2/token":
            form = parse_qs(request.content.decode("utf-8"))
            grant = form.get("grant_type", [""])[0]
            if grant == "client_credentials":
                return httpx.Response(200, json={"access_token": "app_tok_x", "expires_in": 3600})
            if grant == "refresh_token":
                user_token_version["n"] += 1
                return httpx.Response(
                    200,
                    json={
                        "access_token": f"user_tok_v{user_token_version['n']}",
                        "refresh_token": "refresh_v2",
                        "expires_in": 3600,
                    },
                )
        if url.startswith("https://api.twitch.tv/helix/users"):
            return httpx.Response(200, json={"data": [{"id": "37402112", "login": "shroud"}]})
        if url.startswith("https://api.twitch.tv/helix/streams/markers"):
            return httpx.Response(
                200,
                json={"data": [{"id": "m1", "created_at": "2026-11-23T10:00:00Z", "position_seconds": 42}]},
            )
        if url.startswith("https://api.twitch.tv/helix/streams"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "user_id": "37402112",
                            "user_login": "shroud",
                            "game_name": "VALORANT",
                            "title": "Ranked grind",
                            "viewer_count": 18500,
                        }
                    ]
                },
            )
        if url.startswith("https://api.twitch.tv/helix/clips/download"):
            if request.headers.get("Authorization") == "Bearer expired_tok":
                return httpx.Response(401, json={"message": "Invalid OAuth token"})
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "clip_id": "ClutchAce99",
                            "landscape_download_url": "https://clips-media-assets2.twitch.tv/ClutchAce99.mp4",
                            "portrait_download_url": "https://clips-media-assets2.twitch.tv/ClutchAce99_p.mp4",
                        }
                    ]
                },
            )
        if url.startswith("https://api.twitch.tv/helix/clips/vod"):
            return httpx.Response(
                200,
                json={"data": [{"id": "VodClip1", "edit_url": "https://clips.twitch.tv/VodClip1/edit"}]},
            )
        if url.startswith("https://api.twitch.tv/helix/clips") and request.method == "POST":
            return httpx.Response(
                200,
                json={"data": [{"id": "NewClip1", "edit_url": "https://clips.twitch.tv/NewClip1/edit"}]},
            )
        if url.startswith("https://api.twitch.tv/helix/clips") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "ClutchAce99",
                            "broadcaster_id": "37402112",
                            "broadcaster_name": "shroud",
                            "title": "Insane 1v4 Clutch",
                            "view_count": 4200,
                            "duration": 1.0,
                        }
                    ]
                },
            )
        if url == "https://clips-media-assets2.twitch.tv/ClutchAce99_p.mp4":
            return httpx.Response(200, content=sample_mp4_bytes)
        return httpx.Response(404, json={"message": "Unexpected URL"})

    _patch_httpx_async_client(monkeypatch, _handler)
    client = TwitchHelixClient(settings)

    async def _run() -> None:
        await client.save_app_credentials("cid_12345678", "csec_12345678")
        # Inject an expired user token + valid refresh token to verify auto-refresh on 401
        client._user_access_token = "expired_tok"
        client._user_refresh_token = "refresh_v1"
        client._user_expires_at = 9999999999.0
        client._user_id = "37402112"
        client._user_login = "shroud"
        client._scopes = ["clips:edit", "channel:manage:clips"]

        users = await client.get_users(["Shroud"])
        assert users[0]["id"] == "37402112"

        streams = await client.get_streams(["Shroud"])
        assert "shroud" in streams and streams["shroud"]["viewer_count"] == 18500

        created = await client.create_clip("37402112", title="Clutch", duration=30.0)
        assert created["id"] == "NewClip1"

        vod_clip = await client.create_clip_from_vod("37402112", "37402112", "999", 120, 20.0, "Vod")
        assert vod_clip["id"] == "VodClip1"

        marker = await client.create_stream_marker("37402112", "Ace round")
        assert marker["id"] == "m1"

        events: list[tuple[str, dict[str, Any]]] = []

        async def _broadcast(ev_type: str, payload: dict[str, Any]) -> None:
            events.append((ev_type, payload))

        imported = await import_twitch_clip_to_review(
            "https://clips.twitch.tv/ClutchAce99",
            "shroud",
            True,
            settings,
            session_factory,
            _broadcast,
            helix_client=client,
        )
        assert user_token_version["n"] == 2
        assert imported.source_type == "twitch_clip"
        assert imported.status == "pending_review"
        assert imported.caption_position == "seam"
        assert Path(imported.raw_video_path).is_file()
        assert imported.thumbnail_path and Path(imported.thumbnail_path).is_file()
        assert any(t == "clip_created" and p["id"] == imported.id for t, p in events)

    asyncio.run(_run())


def test_eventsub_websocket_and_live_manager_signal4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_data_dirs()
    init_db(settings.db_path)
    session_factory = get_session_factory(settings.db_path)

    with session_factory() as session:
        ch = Channel(
            channel_login="shroud",
            display_name="Shroud",
            broadcaster_id="37402112",
            auto_monitor_online=True,
        )
        session.add(ch)
        session.commit()
        session.refresh(ch)
        ch_id = int(ch.id or 0)

    subs_created: list[str] = []
    poll_step = {"n": 0}

    async def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://id.twitch.tv/oauth2/token":
            return httpx.Response(200, json={"access_token": "app_tok", "expires_in": 3600})
        if url == "https://api.twitch.tv/helix/eventsub/subscriptions":
            body = json.loads(request.content.decode("utf-8"))
            subs_created.append(str(body.get("type")))
            return httpx.Response(202, json={"data": [{"id": f"sub_{len(subs_created)}"}]})
        if url.startswith("https://api.twitch.tv/helix/streams"):
            poll_step["n"] += 1
            viewers = 1000 if poll_step["n"] == 1 else 1350  # +35% spike on poll 2
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "user_id": "37402112",
                            "user_login": "shroud",
                            "game_name": "CS2",
                            "title": "FPL",
                            "viewer_count": viewers,
                        }
                    ]
                },
            )
        if url.startswith("https://api.twitch.tv/helix/clips"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "clip_a"},
                        {"id": "clip_b"},
                    ]
                },
            )
        return httpx.Response(404, json={"message": "Not found"})

    _patch_httpx_async_client(monkeypatch, _handler)
    helix = TwitchHelixClient(settings)

    async def _run() -> None:
        await helix.save_app_credentials("cid_12345678", "csec_12345678")
        helix._user_access_token = "user_tok_live"
        helix._user_expires_at = 9999999999.0
        helix._user_id = "37402112"
        helix._user_login = "shroud"
        helix._scopes = ["clips:edit", "channel:manage:clips", "channel:read:hype_train"]

        received_events: list[tuple[str, dict[str, Any]]] = []
        notified = asyncio.Event()

        async def _ws_handler(conn: ws_server.ServerConnection) -> None:
            await conn.send(
                json.dumps(
                    {
                        "metadata": {"message_id": "m_welcome", "message_type": "session_welcome"},
                        "payload": {"session": {"id": "sess_123", "keepalive_timeout_seconds": 10}},
                    }
                )
            )
            await asyncio.sleep(0.15)
            await conn.send(
                json.dumps(
                    {
                        "metadata": {"message_id": "m_hype", "message_type": "notification"},
                        "payload": {
                            "subscription": {"type": "channel.hype_train.begin"},
                            "event": {"broadcaster_user_id": "37402112", "level": 1},
                        },
                    }
                )
            )
            await asyncio.sleep(0.25)

        async with ws_server.serve(_ws_handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]

            async def _on_notif(sub_type: str, ev: dict[str, Any]) -> None:
                received_events.append((sub_type, ev))
                notified.set()

            es_client = TwitchEventSubClient(
                helix,
                _on_notif,
                ws_url=f"ws://127.0.0.1:{port}",
            )
            await es_client.sync_subscriptions(["37402112"])
            await es_client.start()
            try:
                await asyncio.wait_for(notified.wait(), timeout=5.0)
                assert es_client.is_connected is True
                assert helix.eventsub_connected is True
            finally:
                await es_client.stop()

        assert "stream.online" in subs_created
        assert "channel.raid" in subs_created
        assert "channel.hype_train.begin" in subs_created
        assert "channel.cheer" in subs_created
        assert received_events and received_events[0][0] == "channel.hype_train.begin"

        # Now test LiveManager Signal #4 poll + EventSub dispatch
        broadcasts: list[tuple[str, dict[str, Any]]] = []

        async def _collect_broadcast(ev_type: str, payload: dict[str, Any]) -> None:
            broadcasts.append((ev_type, payload))

        manager = LiveManager(settings, session_factory, _collect_broadcast, helix_client=helix)

        # Register monitor without launching external Streamlink process
        detector = HypeDetector("shroud", threshold=68.0)
        ring = ChannelRingBuffer("shroud", settings)
        monitor = _ChannelMonitor(
            channel_id=ch_id,
            channel_login="shroud",
            detector=detector,
            irc=TwitchIRCClient("shroud", lambda _m: None),
            ring=ring,
        )
        manager._monitors[ch_id] = monitor

        # Poll 1: establishes baseline viewers (1000) and detects 2 new clips (+35 boost)
        await manager._poll_helix_once()
        # Poll 2: viewer spike 1000 -> 1350 (+35% >= 20% -> +20 boost)
        await manager._poll_helix_once()

        # EventSub notifications add +30 (raid), +40 (hype train), +25 (cheer)
        await manager._handle_eventsub_notification("channel.raid", {"to_broadcaster_user_id": "37402112"})
        await manager._handle_eventsub_notification(
            "channel.hype_train.progress", {"broadcaster_user_id": "37402112"}
        )

        ev = detector.evaluate()
        assert "Viewer clip spike" in ev.top_tokens
        assert "Viewer spike" in ev.top_tokens
        assert "Channel raid" in ev.top_tokens
        assert "Hype Train" in ev.top_tokens
        assert ev.fused_score == 100.0
        assert any(t == "channel_metadata" and p["game_name"] == "CS2" for t, p in broadcasts)

        manager._monitors.clear()

    asyncio.run(_run())


def test_eventsub_reconnect_dedup_and_ytdlp_clip_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        _validate_eventsub_reconnect_url("wss://eventsub.wss.twitch.tv/ws?keep=1")
        == "wss://eventsub.wss.twitch.tv/ws?keep=1"
    )
    for bad_ws in (
        "ws://eventsub.wss.twitch.tv/ws",
        "wss://evil.com/ws",
        "wss://eventsub.wss.twitch.tv:9443/ws",
    ):
        with pytest.raises(ValueError):
            _validate_eventsub_reconnect_url(bad_ws)

    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_data_dirs()
    init_db(settings.db_path)
    session_factory = get_session_factory(settings.db_path)
    helix = TwitchHelixClient(settings)

    seen_notifs: list[str] = []
    es = TwitchEventSubClient(helix, lambda sub_type, _ev: seen_notifs.append(sub_type))

    async def _run_frames() -> None:
        # Duplicate message_id ignored
        notif_frame = json.dumps(
            {
                "metadata": {"message_id": "dup_1", "message_type": "notification"},
                "payload": {"subscription": {"type": "channel.cheer"}, "event": {"bits": 500}},
            }
        )
        await es.handle_frame(notif_frame)
        await es.handle_frame(notif_frame)
        assert seen_notifs == ["channel.cheer"]

        # Valid vs malicious reconnect URL
        assert (
            await es.handle_frame(
                json.dumps(
                    {
                        "metadata": {"message_id": "rec_1", "message_type": "session_reconnect"},
                        "payload": {"session": {"reconnect_url": "wss://eventsub.wss.twitch.tv/ws?r=1"}},
                    }
                )
            )
            == "wss://eventsub.wss.twitch.tv/ws?r=1"
        )
        assert (
            await es.handle_frame(
                json.dumps(
                    {
                        "metadata": {"message_id": "rec_2", "message_type": "session_reconnect"},
                        "payload": {"session": {"reconnect_url": "wss://evil.com/ws"}},
                    }
                )
            )
            is None
        )

    asyncio.run(_run_frames())

    sample_bytes = _make_sample_mp4(tmp_path / "fallback.mp4", settings.ffmpeg_path)

    def _fake_ytdlp(clip_url: str, dest_path: Path, ffmpeg_path: str) -> Path:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(sample_bytes)
        return dest_path

    monkeypatch.setattr("backend.services.twitch_helix._download_clip_with_ytdlp_sync", _fake_ytdlp)

    clip = asyncio.run(
        import_twitch_clip_to_review(
            "https://www.twitch.tv/shroud/clip/FallbackClipSlug",
            None,
            False,
            settings,
            session_factory,
            None,
            helix_client=None,
        )
    )
    assert clip.channel_login == "shroud"
    assert clip.source_type == "twitch_clip"
    assert Path(clip.raw_video_path).is_file()
