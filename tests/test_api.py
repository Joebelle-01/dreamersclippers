"""End-to-end FastAPI API and Range media tests with isolated temp storage."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.main as backend_main
from backend.config import Settings
from backend.main import create_app
from backend.models import ClipItem


def _synthesize_test_mp4(ffmpeg_path: str, out_path: Path, duration_sec: float = 2.0) -> None:
    """Generate a deterministic 16:9 1280x720 MP4 with a simulated webcam box and sine audio."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_path,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=1280x720:rate=30:duration={duration_sec}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=48000:duration={duration_sec}",
        "-vf",
        "drawbox=x=24:y=460:w=280:h=220:color=0x224466@0.95:t=fill",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-shortest",
        str(out_path),
    ]
    proc = subprocess.run(
        cmd,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"FFmpeg synthesis failed: {proc.stderr}"


def _probe_media(ffprobe_path: str, media_path: Path) -> dict:
    """Return parsed ffprobe stream metadata for a rendered MP4."""
    proc = subprocess.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,sample_aspect_ratio,sample_rate",
            "-of",
            "json",
            str(media_path),
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, f"ffprobe failed: {proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture()
def api_client(tmp_path: Path):
    """Create a FastAPI TestClient backed by an isolated temp data directory and SQLite DB."""
    settings = Settings(data_dir=tmp_path / "data")
    app = create_app(custom_settings=settings)
    with TestClient(app) as client:
        yield client, app, settings


class TestSystemAndChannelsApi:
    """Verify system status, channel CRUD, and input validation rejections."""

    def test_system_status_returns_capabilities_and_isolated_data_dir(
        self, api_client: tuple[TestClient, object, Settings]
    ) -> None:
        client, _, settings = api_client
        resp = client.get("/api/system/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_dir"] == str(settings.data_dir)
        assert body["active_monitors"] == 0
        assert body["pending_clips"] == 0
        assert body["exported_clips"] == 0
        assert isinstance(body["nvenc_available"], bool)
        assert Path(body["ffmpeg_path"]).is_file()
        assert Path(body["ffprobe_path"]).is_file()

    def test_channel_crud_lifecycle(
        self, api_client: tuple[TestClient, object, Settings]
    ) -> None:
        client, _, _ = api_client

        assert client.get("/api/channels").json() == []

        create_resp = client.post(
            "/api/channels",
            json={
                "channel_login": "Shroud",
                "display_name": "Shroud Official",
                "hype_threshold": 72.5,
                "pre_roll_sec": 20,
                "post_roll_sec": 12,
                "cooldown_sec": 40,
            },
        )
        assert create_resp.status_code == 201
        created = create_resp.json()
        channel_id = created["id"]
        assert created["channel_login"] == "shroud"
        assert created["display_name"] == "Shroud Official"
        assert created["hype_threshold"] == pytest.approx(72.5)
        assert created["is_monitoring"] is False

        dup_resp = client.post("/api/channels", json={"channel_login": "shroud"})
        assert dup_resp.status_code == 409

        list_resp = client.get("/api/channels")
        assert list_resp.status_code == 200
        assert len(list_resp.json()) == 1

        patch_resp = client.patch(
            f"/api/channels/{channel_id}",
            json={
                "hype_threshold": 64.0,
                "preset_split_ratio": 0.40,
                "preset_layout_mode": "blur_no_cam",
            },
        )
        assert patch_resp.status_code == 200
        patched = patch_resp.json()
        assert patched["hype_threshold"] == pytest.approx(64.0)
        assert patched["preset_split_ratio"] == pytest.approx(0.40)
        assert patched["preset_layout_mode"] == "blur_no_cam"

        del_resp = client.delete(f"/api/channels/{channel_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted"] is True
        assert client.get("/api/channels").json() == []

        assert client.delete(f"/api/channels/{channel_id}").status_code == 404

    @pytest.mark.parametrize(
        "bad_login",
        [
            "shroud; calc",
            "bad login",
            "https://evil.com/shroud",
            "https://www.twitch.tv/videos/123456",
            "https://www.twitch.tv/directory",
            "ab",
            "../etc/passwd",
        ],
    )
    def test_rejects_invalid_channel_logins(
        self, api_client: tuple[TestClient, object, Settings], bad_login: str
    ) -> None:
        client, _, _ = api_client
        resp = client.post("/api/channels", json={"channel_login": bad_login})
        assert resp.status_code in (400, 422)

    def test_creates_channel_from_live_twitch_url(
        self, api_client: tuple[TestClient, object, Settings]
    ) -> None:
        client, _, _ = api_client
        resp = client.post(
            "/api/channels",
            json={
                "channel_login": "https://www.twitch.tv/caseoh_",
                "display_name": "https://www.twitch.tv/caseoh_",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["channel_login"] == "caseoh_"
        assert data["display_name"] == "caseoh_"

    def test_auto_monitor_online_persistence(
        self, api_client: tuple[TestClient, object, Settings]
    ) -> None:
        client, _, _ = api_client
        create_resp = client.post(
            "/api/channels",
            json={
                "channel_login": "tarik",
                "auto_monitor_online": True,
            },
        )
        assert create_resp.status_code == 201
        created = create_resp.json()
        channel_id = created["id"]
        assert created["auto_monitor_online"] is True

        listed = client.get("/api/channels").json()
        assert listed[0]["auto_monitor_online"] is True

        patch_resp = client.patch(
            f"/api/channels/{channel_id}",
            json={"auto_monitor_online": False},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["auto_monitor_online"] is False

        listed_after = client.get("/api/channels").json()
        assert listed_after[0]["auto_monitor_online"] is False

    @pytest.mark.parametrize(
        "ssrf_url",
        [
            "http://127.0.0.1:8000",
            "file:///etc/passwd",
            "https://evil.com/videos/1",
            "javascript:alert(1)",
            "; calc",
            "http://169.254.169.254/latest/meta-data/",
        ],
    )
    def test_rejects_ssrf_vod_urls(
        self, api_client: tuple[TestClient, object, Settings], ssrf_url: str
    ) -> None:
        client, _, _ = api_client
        resp = client.post("/api/vods/scan", json={"vod_url": ssrf_url})
        assert resp.status_code in (400, 422)


class TestClipWorkflowApi:
    """Verify clip detection, patching, preset persistence, 9:16 render, Range 206, and delete."""

    def test_clip_detect_patch_preset_render_range_and_delete(
        self, api_client: tuple[TestClient, object, Settings]
    ) -> None:
        client, app, settings = api_client

        channel_resp = client.post("/api/channels", json={"channel_login": "xqc_123"})
        assert channel_resp.status_code == 201
        channel_id = channel_resp.json()["id"]

        raw_clip_path = settings.clips_dir / "synthetic_test_clip.mp4"
        _synthesize_test_mp4(settings.ffmpeg_path, raw_clip_path, duration_sec=2.0)

        clip_id = "clip-e2e-test-001"
        with app.state.session_factory() as session:
            clip = ClipItem(
                id=clip_id,
                channel_id=channel_id,
                channel_login="xqc_123",
                source_type="live",
                source_ref="live:xqc_123",
                title="Synthetic Clutch Moment",
                status="pending_review",
                hype_score=84.5,
                trigger_reason="chat+audio burst",
                raw_video_path=str(raw_clip_path),
                duration_sec=2.0,
                trim_start_sec=0.0,
                trim_end_sec=2.0,
            )
            session.add(clip)
            session.commit()

        list_resp = client.get("/api/clips", params={"status": "pending_review"})
        assert list_resp.status_code == 200
        assert any(item["id"] == clip_id for item in list_resp.json())

        get_resp = client.get(f"/api/clips/{clip_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["title"] == "Synthetic Clutch Moment"

        detect_resp = client.post(f"/api/clips/{clip_id}/detect-webcam")
        assert detect_resp.status_code == 200
        detected = detect_resp.json()
        assert detected["layout_mode"] in ("split_top_cam", "blur_no_cam")
        assert detected["gameplay_box"] is not None
        assert 0.0 <= detected["webcam_confidence"] <= 1.0

        patch_resp = client.patch(
            f"/api/clips/{clip_id}",
            json={
                "title": "Edited Vertical Highlight",
                "trim_start_sec": 0.2,
                "trim_end_sec": 1.8,
                "layout_mode": "split_top_cam",
                "split_ratio": 0.40,
                "normalize_audio": True,
                "captions_enabled": False,
                "webcam_box": {"x": 0.02, "y": 0.60, "w": 0.24, "h": 0.32},
                "gameplay_box": {"x": 0.22, "y": 0.0, "w": 0.56, "h": 1.0},
            },
        )
        assert patch_resp.status_code == 200
        patched = patch_resp.json()
        assert patched["title"] == "Edited Vertical Highlight"
        assert patched["layout_mode"] == "split_top_cam"
        assert patched["split_ratio"] == pytest.approx(0.40)
        assert patched["webcam_box"]["x"] == pytest.approx(0.02)

        bad_patch = client.patch(
            f"/api/clips/{clip_id}",
            json={"trim_start_sec": 1.5, "trim_end_sec": 1.0},
        )
        assert bad_patch.status_code == 422

        preset_resp = client.post(f"/api/clips/{clip_id}/save-preset")
        assert preset_resp.status_code == 200
        preset_channel = preset_resp.json()
        assert preset_channel["preset_layout_mode"] == "split_top_cam"
        assert preset_channel["preset_split_ratio"] == pytest.approx(0.40)
        assert preset_channel["preset_webcam"]["w"] == pytest.approx(0.24)
        assert preset_channel["preset_gameplay"]["w"] == pytest.approx(0.56)

        render_resp = client.post(f"/api/clips/{clip_id}/render")
        assert render_resp.status_code == 200
        assert render_resp.json()["status"] == "rendering"

        final_clip = None
        for _ in range(60):
            poll = client.get(f"/api/clips/{clip_id}")
            assert poll.status_code == 200
            final_clip = poll.json()
            if final_clip["status"] in ("exported", "failed"):
                break
            time.sleep(0.1)

        assert final_clip is not None
        assert final_clip["status"] == "exported", f"Render failed: {final_clip.get('error_message')}"
        export_path = Path(final_clip["export_video_path"])
        assert export_path.is_file()
        assert final_clip["export_thumb_path"] is not None
        export_thumb_path = Path(final_clip["export_thumb_path"])
        assert export_thumb_path.is_file()

        probe_data = _probe_media(settings.ffprobe_path, export_path)
        streams = probe_data["streams"]
        video_stream = next(s for s in streams if s["codec_type"] == "video")
        audio_stream = next(s for s in streams if s["codec_type"] == "audio")
        assert video_stream["width"] == 1080
        assert video_stream["height"] == 1920
        assert video_stream["codec_name"] == "h264"
        assert video_stream["sample_aspect_ratio"] == "1:1"
        assert audio_stream["codec_name"] == "aac"
        assert int(audio_stream["sample_rate"]) == 48000

        file_size = export_path.stat().st_size
        assert file_size > 1024

        range_resp = client.get(
            f"/api/media/export/{clip_id}",
            headers={"Range": "bytes=0-1023"},
        )
        assert range_resp.status_code == 206
        assert range_resp.headers["accept-ranges"] == "bytes"
        assert range_resp.headers["content-range"] == f"bytes 0-1023/{file_size}"
        assert len(range_resp.content) == 1024

        raw_range_resp = client.get(
            f"/api/media/raw/{clip_id}",
            headers={"Range": "bytes=0-511"},
        )
        assert raw_range_resp.status_code == 206
        assert len(raw_range_resp.content) == 512

        status_after = client.get("/api/system/status").json()
        assert status_after["exported_clips"] == 1

        del_resp = client.delete(f"/api/clips/{clip_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted"] is True
        assert client.get(f"/api/clips/{clip_id}").status_code == 404
        assert not raw_clip_path.exists()
        assert not export_path.exists()
        assert not export_thumb_path.exists()

    def test_caption_position_seam_patch_and_render(
        self,
        api_client: tuple[TestClient, object, Settings],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, app, settings = api_client

        channel_resp = client.post("/api/channels", json={"channel_login": "tarik"})
        assert channel_resp.status_code == 201
        channel_id = channel_resp.json()["id"]

        raw_clip_path = settings.clips_dir / "seam_caption_clip.mp4"
        _synthesize_test_mp4(settings.ffmpeg_path, raw_clip_path, duration_sec=1.5)

        clip_id = "clip-seam-caption-001"
        with app.state.session_factory() as session:
            clip = ClipItem(
                id=clip_id,
                channel_id=channel_id,
                channel_login="tarik",
                source_type="live",
                source_ref="live:tarik",
                title="Seam Caption Test",
                status="pending_review",
                hype_score=91.0,
                trigger_reason="chat+audio burst",
                raw_video_path=str(raw_clip_path),
                duration_sec=1.5,
                trim_start_sec=0.0,
                trim_end_sec=1.5,
                captions_enabled=True,
                captions_json=json.dumps(
                    [{"start": 0.0, "end": 1.2, "text": "INSANE CLUTCH"}]
                ),
            )
            session.add(clip)
            session.commit()

        lower_patch = client.patch(
            f"/api/clips/{clip_id}",
            json={"caption_position": "lower_safe"},
        )
        assert lower_patch.status_code == 200
        assert lower_patch.json()["caption_position"] == "lower_safe"

        seam_patch = client.patch(
            f"/api/clips/{clip_id}",
            json={
                "caption_position": "seam",
                "layout_mode": "split_top_cam",
                "split_ratio": 0.40,
                "captions_enabled": True,
                "webcam_box": {"x": 0.02, "y": 0.60, "w": 0.24, "h": 0.32},
                "gameplay_box": {"x": 0.22, "y": 0.0, "w": 0.56, "h": 1.0},
            },
        )
        assert seam_patch.status_code == 200
        assert seam_patch.json()["caption_position"] == "seam"

        captured: list[dict[str, Any]] = []
        real_build_ass = backend_main.build_ass_subtitles

        def _spy_build_ass(*args: Any, **kwargs: Any) -> str:
            ass_output = real_build_ass(*args, **kwargs)
            captured.append({"kwargs": kwargs, "ass": ass_output})
            return ass_output

        monkeypatch.setattr(backend_main, "build_ass_subtitles", _spy_build_ass)

        render_resp = client.post(f"/api/clips/{clip_id}/render")
        assert render_resp.status_code == 200
        assert render_resp.json()["status"] == "rendering"

        final_clip = None
        for _ in range(60):
            poll = client.get(f"/api/clips/{clip_id}")
            assert poll.status_code == 200
            final_clip = poll.json()
            if final_clip["status"] in ("exported", "failed"):
                break
            time.sleep(0.1)

        assert final_clip is not None
        assert final_clip["status"] == "exported", f"Render failed: {final_clip.get('error_message')}"
        assert final_clip["caption_position"] == "seam"
        assert len(captured) == 1
        assert captured[0]["kwargs"]["caption_position"] == "seam"
        assert r"\pos(540,768)" in captured[0]["ass"]

        export_path = Path(final_clip["export_video_path"])
        assert export_path.is_file()
        probe_data = _probe_media(settings.ffprobe_path, export_path)
        video_stream = next(s for s in probe_data["streams"] if s["codec_type"] == "video")
        assert video_stream["width"] == 1080
        assert video_stream["height"] == 1920


class _MockTwitchTransport(httpx.AsyncBaseTransport):
    """Async HTTP transport stub for Twitch OAuth and Helix API endpoints."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._handler(request)


class TestTwitchHelixAndOAuthApi:
    """Verify Twitch status/config, OAuth CSRF guard, clip import, publish-clip, and marker routes."""

    def test_twitch_helix_oauth_clips_publish_and_marker_flow(
        self,
        api_client: tuple[TestClient, object, Settings],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _, settings = api_client

        async def _mock_twitch_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if request.method == "POST" and url.startswith("https://id.twitch.tv/oauth2/token"):
                form = parse_qs(request.content.decode("utf-8"))
                grant_type = form.get("grant_type", [""])[0]
                if grant_type == "client_credentials":
                    return httpx.Response(
                        200,
                        json={
                            "access_token": "mock_app_tok_value",  # pragma: allowlist secret
                            "expires_in": 3600,
                            "token_type": "bearer",
                        },
                    )
                if grant_type == "authorization_code":
                    return httpx.Response(
                        200,
                        json={
                            "access_token": "mock_user_tok_value",  # pragma: allowlist secret
                            "refresh_token": "mock_refresh_tok_value",  # pragma: allowlist secret
                            "expires_in": 3600,
                            "scope": ["clips:edit", "channel:manage:clips", "channel:manage:broadcast"],
                            "token_type": "bearer",
                        },
                    )
                return httpx.Response(400, json={"message": "unsupported grant"})

            if request.method == "GET" and url.startswith("https://id.twitch.tv/oauth2/validate"):
                return httpx.Response(
                    200,
                    json={
                        "login": "shroud",
                        "user_id": "37402112",
                        "scopes": ["clips:edit", "channel:manage:clips", "channel:manage:broadcast"],
                    },
                )

            if request.method == "GET" and url.startswith("https://api.twitch.tv/helix/users"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "37402112",
                                "login": "shroud",
                                "display_name": "shroud",
                            }
                        ]
                    },
                )

            if request.method == "GET" and url.startswith("https://api.twitch.tv/helix/clips"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "ClipSlug001",
                                "url": "https://clips.twitch.tv/ClipSlug001",
                                "broadcaster_id": "37402112",
                                "broadcaster_name": "shroud",
                                "title": "Insane Flick Shot",
                                "view_count": 4200,
                                "duration": 18.0,
                                "thumbnail_url": "https://clips-media-assets2.twitch.tv/thumb.jpg",
                                "created_at": "2026-11-23T09:00:00Z",
                            }
                        ]
                    },
                )

            if request.method == "POST" and url.startswith("https://api.twitch.tv/helix/clips"):
                return httpx.Response(
                    202,
                    json={
                        "data": [
                            {
                                "id": "CreatedClip999",
                                "edit_url": "https://clips.twitch.tv/CreatedClip999/edit",
                            }
                        ]
                    },
                )

            if request.method == "POST" and url.startswith("https://api.twitch.tv/helix/streams/markers"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "marker-101",
                                "created_at": "2026-11-23T10:00:00Z",
                                "description": "Boss defeated",
                                "position_seconds": 412,
                            }
                        ]
                    },
                )

            return httpx.Response(404, json={"message": f"Unhandled mock URL: {url}"})

        transport = _MockTwitchTransport(_mock_twitch_handler)
        real_async_client = httpx.AsyncClient

        def _client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return real_async_client(*args, **kwargs)

        async def _noop_async(*_args: Any, **_kwargs: Any) -> None:
            return None

        def _fake_ytdlp_download(clip_url: str, dest_path: Path, ffmpeg_path: str) -> Path:
            assert clip_url == "https://clips.twitch.tv/ClipSlug001"
            _synthesize_test_mp4(ffmpeg_path, dest_path, duration_sec=1.5)
            return dest_path

        monkeypatch.setattr("backend.services.twitch_helix.httpx.AsyncClient", _client_factory)
        monkeypatch.setattr(
            "backend.services.twitch_helix._download_clip_with_ytdlp_sync",
            _fake_ytdlp_download,
        )
        monkeypatch.setattr("backend.services.twitch_eventsub.TwitchEventSubClient.start", _noop_async)
        monkeypatch.setattr(
            "backend.services.twitch_eventsub.TwitchEventSubClient.sync_subscriptions",
            _noop_async,
        )

        # 1. Unconfigured status must expose zero secrets
        status_init = client.get("/api/twitch/status")
        assert status_init.status_code == 200
        init_body = status_init.json()
        assert init_body["configured"] is False
        assert init_body["user_connected"] is False
        assert init_body["client_id_masked"] is None
        for secret_field in ("client_secret", "access_token", "refresh_token", "app_access_token"):
            assert secret_field not in init_body

        # 2. Save Twitch app credentials via mocked OAuth client_credentials exchange
        cfg_resp = client.put(
            "/api/twitch/config",
            json={
                "client_id": "demo_twitch_client_1234",
                "client_secret": "demo_twitch_secret_5678",  # pragma: allowlist secret
            },
        )
        assert cfg_resp.status_code == 200
        cfg_body = cfg_resp.json()
        assert cfg_body["configured"] is True
        assert cfg_body["app_token_ready"] is True
        assert cfg_body["client_id_masked"] == "demo...1234"
        assert "demo_twitch_secret_5678" not in json.dumps(cfg_body)
        assert "mock_app_tok_value" not in json.dumps(cfg_body)

        # 3. OAuth start + CSRF invalid-state rejection (400) + valid OAuth callback
        oauth_start = client.get("/api/twitch/oauth/start", params={"mode": "broadcaster"})
        assert oauth_start.status_code == 200
        oauth_data = oauth_start.json()
        assert "https://id.twitch.tv/oauth2/authorize" in oauth_data["authorize_url"]
        valid_state = oauth_data["state"]
        assert valid_state

        bad_cb = client.get(
            "/api/twitch/oauth/callback",
            params={"code": "fake_code", "state": "forged_csrf_state"},
        )
        assert bad_cb.status_code == 400

        good_cb = client.get(
            "/api/twitch/oauth/callback",
            params={"code": "valid_auth_code", "state": valid_state},
        )
        assert good_cb.status_code == 200
        assert "shroud" in good_cb.text

        status_after_oauth = client.get("/api/twitch/status").json()
        assert status_after_oauth["user_connected"] is True
        assert status_after_oauth["user_login"] == "shroud"
        assert status_after_oauth["has_broadcaster_scopes"] is True
        assert "mock_user_tok_value" not in json.dumps(status_after_oauth)

        # 4. Fetch mocked top clips for channel login
        clips_resp = client.get("/api/twitch/clips/shroud", params={"period": "week"})
        assert clips_resp.status_code == 200
        clips_list = clips_resp.json()
        assert len(clips_list) == 1
        assert clips_list[0]["id"] == "ClipSlug001"

        # 5. Reject SSRF clip import URL and import valid Twitch clip URL into Review Queue
        bad_import = client.post(
            "/api/twitch/clips/import",
            json={"clip_url": "https://evil.com/ClipSlug001"},
        )
        assert bad_import.status_code == 422

        import_resp = client.post(
            "/api/twitch/clips/import",
            json={
                "clip_url": "https://clips.twitch.tv/ClipSlug001",
                "broadcaster_login": "shroud",
                "prefer_portrait": False,
            },
        )
        assert import_resp.status_code == 200
        imported = import_resp.json()
        assert imported["source_type"] == "twitch_clip"
        assert imported["status"] == "pending_review"
        assert imported["caption_position"] == "seam"
        assert imported["channel_login"] == "shroud"
        assert Path(imported["raw_video_path"]).is_file()
        assert Path(imported["thumbnail_path"]).is_file()

        # 6. Publish clip & create stream marker for a Channel
        ch_resp = client.post("/api/channels", json={"channel_login": "shroud"})
        assert ch_resp.status_code == 201
        channel_id = ch_resp.json()["id"]

        publish_resp = client.post(
            f"/api/channels/{channel_id}/publish-clip",
            json={"title": "Highlight Clip", "duration": 25.0},
        )
        assert publish_resp.status_code == 200
        assert publish_resp.json()["id"] == "CreatedClip999"
        assert publish_resp.json()["edit_url"] == "https://clips.twitch.tv/CreatedClip999/edit"

        marker_resp = client.post(
            f"/api/channels/{channel_id}/marker",
            json={"description": "Boss defeated"},
        )
        assert marker_resp.status_code == 200
        assert marker_resp.json()["id"] == "marker-101"
        assert marker_resp.json()["description"] == "Boss defeated"

        # 7. Disconnect clears user tokens and blocks publish-clip with 401
        disc_resp = client.post("/api/twitch/oauth/disconnect")
        assert disc_resp.status_code == 200
        assert disc_resp.json()["user_connected"] is False

        unauth_publish = client.post(f"/api/channels/{channel_id}/publish-clip")
        assert unauth_publish.status_code == 401
        assert settings.twitch_credentials_path.is_file()
