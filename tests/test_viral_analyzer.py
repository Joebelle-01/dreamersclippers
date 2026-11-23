"""Unit and integration tests for full-video non-live viral analysis."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import backend.main as backend_main
import backend.services.viral_analyzer as viral_analyzer
from backend.config import Settings, resolve_ffmpeg_path
from backend.main import create_app
from backend.models import CaptionWord, ChannelCreate, ClipItem, VideoAnalyzeUrlRequest
from backend.security import validate_video_upload_filename, validate_video_url
from backend.services.caption_service import slice_captions_for_window
from backend.services.viral_analyzer import build_viral_title, score_viral_candidates


def _synth_mp4(ffmpeg_path: str, out_path: Path, duration_sec: float = 14.0) -> None:
    """Create a deterministic synthetic 1280x720 MP4 with audio for upload tests."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_path,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=1280x720:rate=15:duration={duration_sec}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=520:sample_rate=48000:duration={duration_sec}",
        "-vf",
        "drawbox=x=24:y=460:w=260:h=210:color=0x225588@0.95:t=fill",
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
        timeout=45,
        check=False,
    )
    assert proc.returncode == 0, f"FFmpeg synthesis failed: {proc.stderr}"


class TestVideoValidators:
    """Validate YouTube/Twitch VOD SSRF allowlisting and upload extension rules."""

    @pytest.mark.parametrize(
        ("raw", "expected_url", "expected_platform"),
        [
            (
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "youtube",
            ),
            (
                "https://m.youtube.com/watch?v=abcDEF_12-3&t=42s",
                "https://www.youtube.com/watch?v=abcDEF_12-3",
                "youtube",
            ),
            (
                "https://youtu.be/abcDEF_12-3/",
                "https://www.youtube.com/watch?v=abcDEF_12-3",
                "youtube",
            ),
            (
                "https://www.youtube.com/shorts/abcDEF_12-3",
                "https://www.youtube.com/watch?v=abcDEF_12-3",
                "youtube",
            ),
            (
                "https://www.youtube.com/live/abcDEF_12-3",
                "https://www.youtube.com/watch?v=abcDEF_12-3",
                "youtube",
            ),
            (
                "https://www.twitch.tv/videos/1234567890",
                "https://www.twitch.tv/videos/1234567890",
                "twitch_vod",
            ),
        ],
    )
    def test_accepts_allowed_video_urls(
        self,
        raw: str,
        expected_url: str,
        expected_platform: str,
    ) -> None:
        canonical, platform = validate_video_url(raw)
        assert canonical == expected_url
        assert platform == expected_platform

    @pytest.mark.parametrize(
        "malicious",
        [
            "",
            "   ",
            "http://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "http://127.0.0.1:8430/api/system/status",
            "https://evil.com/watch?v=dQw4w9WgXcQ",
            "https://" + "u" + ":" + "p" + "@www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com:8443/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=short",
            "https://www.youtube.com/playlist?list=PL123",
            "https://www.twitch.tv/caseoh_",
            "file:///etc/passwd",
            "https://youtu.be/bad\nline",
        ],
    )
    def test_rejects_invalid_and_ssrf_video_urls(self, malicious: str) -> None:
        with pytest.raises(ValueError):
            validate_video_url(malicious)

    @pytest.mark.parametrize(
        ("filename", "expected_ext"),
        [
            ("gameplay_highlight.mp4", ".mp4"),
            ("STREAM_PART1.MKV", ".mkv"),
            ("podcast.mov", ".mov"),
            ("clip.webm", ".webm"),
            ("edit.m4v", ".m4v"),
        ],
    )
    def test_accepts_allowed_upload_filenames(self, filename: str, expected_ext: str) -> None:
        assert validate_video_upload_filename(filename) == expected_ext

    @pytest.mark.parametrize(
        "bad_name",
        [
            "",
            "   ",
            "payload.exe",
            "script.sh",
            "noextension",
            "../escape.mp4",
            "sub/clip.mp4",
            "sub\\clip.mp4",
            "bad\x00name.mp4",
        ],
    )
    def test_rejects_bad_upload_filenames(self, bad_name: str) -> None:
        with pytest.raises(ValueError):
            validate_video_upload_filename(bad_name)


class TestViralScoringEngine:
    """Verify multi-factor viral scoring, sentence boundaries, titles, and captions slicing."""

    def test_hook_plus_energy_outranks_dull_filler_and_respects_sentences(self) -> None:
        duration_sec = 90.0
        audio_samples = []
        motion_samples = []
        for s in range(90):
            # 0..35s quiet flat filler; 40..66s loud climax; 70..90s moderate chatter
            if 40 <= s <= 66:
                db = -11.0 if s in (42, 55, 62) else -17.0
                mot = 38.0 if s in (42, 56) else 22.0
            elif s < 35:
                db = -36.0
                mot = 4.0
            else:
                db = -32.0
                mot = 6.0
            audio_samples.append((s + 0.5, db))
            motion_samples.append((s + 0.5, mot))

        speech_segments = [
            {
                "start": 2.0,
                "end": 12.0,
                "text": "Okay so today we are just walking slowly around the map.",
                "words": [],
            },
            {
                "start": 12.5,
                "end": 26.0,
                "text": "Nothing really interesting happens during this menu screen.",
                "words": [],
            },
            {
                "start": 40.0,
                "end": 47.0,
                "text": "Wait why did that secret trick just work on the first try?!",
                "words": [],
            },
            {
                "start": 47.4,
                "end": 57.0,
                "text": "Hold on everyone look at that angle because nobody tells you this!",
                "words": [],
            },
            {
                "start": 57.5,
                "end": 65.5,
                "text": "No way that is actually insane clutch of the whole game!",
                "words": [],
            },
            {
                "start": 71.0,
                "end": 86.0,
                "text": "Anyway let us check the settings page for a minute.",
                "words": [],
            },
        ]

        picks = score_viral_candidates(
            duration_sec,
            audio_samples,
            motion_samples,
            speech_segments,
            min_clip_sec=18.0,
            max_clip_sec=35.0,
            max_clips=3,
            viral_threshold=45.0,
            fallback_title="Match VOD",
        )

        assert len(picks) >= 1
        top = picks[0]
        assert top["viral_score"] >= 60.0
        # Best window starts at segment 2 (~39.65s) and ends at sentence end (~65.85s)
        assert 39.0 <= top["start"] <= 41.0
        assert 65.0 <= top["end"] <= 67.0
        assert "Wait" in top["title"]
        assert top["reason"].startswith("Viral ")

        # Verify non-overlapping constraint among returned peaks
        for i in range(len(picks)):
            for j in range(i + 1, len(picks)):
                a0, a1 = picks[i]["start"], picks[i]["end"]
                b0, b1 = picks[j]["start"], picks[j]["end"]
                overlap = max(0.0, min(a1, b1) - max(a0, b0))
                min_len = max(0.1, min(a1 - a0, b1 - b0))
                assert (overlap / min_len) < 0.15

    def test_no_speech_audio_motion_fallback(self) -> None:
        audio_samples = [(s + 0.5, -10.0 if 25 <= s <= 38 else -42.0) for s in range(60)]
        motion_samples = [(s + 0.5, 42.0 if 25 <= s <= 38 else 3.0) for s in range(60)]
        picks = score_viral_candidates(
            60.0,
            audio_samples,
            motion_samples,
            [],
            min_clip_sec=15.0,
            max_clip_sec=30.0,
            max_clips=2,
            viral_threshold=45.0,
            fallback_title="Highlight Reel",
        )
        assert len(picks) >= 1
        assert picks[0]["title"].startswith("Highlight Reel")
        assert 10.0 <= picks[0]["start"] <= 30.0
        assert picks[0]["end"] > picks[0]["start"]

    def test_build_viral_title_formats_clean_hook(self) -> None:
        title = build_viral_title("wait why did you actually jump right there?!", "Stream", 1)
        assert title.startswith("Wait why did you")
        assert len(title) <= 68

        fallback = build_viral_title("ok", "Podcast Ep 4", 2)
        assert fallback == "Podcast Ep 4 — Viral #2"

    def test_slice_captions_for_window_shifts_and_groups_words(self) -> None:
        words = [
            CaptionWord(start=5.0, end=5.4, word="Too"),
            CaptionWord(start=10.2, end=10.5, word="Wait"),
            CaptionWord(start=10.6, end=10.9, word="what?!"),
            CaptionWord(start=11.8, end=12.1, word="No"),
            CaptionWord(start=12.2, end=12.6, word="way!"),
            CaptionWord(start=35.0, end=35.5, word="Late"),
        ]
        segs = slice_captions_for_window(words, 10.0, 20.0)
        assert len(segs) == 2
        assert segs[0].text == "Wait what?!"
        assert segs[0].start == pytest.approx(0.2, abs=0.05)
        assert segs[1].text == "No way!"
        assert segs[1].start == pytest.approx(1.8, abs=0.05)


class TestVideoAnalysisApi:
    """Integration tests for /api/videos/analyze-url, /analyze-upload, and /jobs/{id}."""

    def test_analyze_url_validation_and_dispatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = Settings(data_dir=tmp_path / "data")
        captured: dict[str, Any] = {}

        async def _fake_analyze_url(video_url: str, **kwargs: Any) -> dict[str, Any]:
            captured["video_url"] = video_url
            captured["kwargs"] = kwargs
            return {"job_id": kwargs["job_id"], "clips_created": []}

        monkeypatch.setattr(backend_main, "analyze_video_url", _fake_analyze_url)

        app = create_app(custom_settings=settings)
        with TestClient(app) as client:
            bad = client.post(
                "/api/videos/analyze-url",
                json={"video_url": "http://127.0.0.1:8080/video.mp4"},
            )
            assert bad.status_code == 422

            bad_range = client.post(
                "/api/videos/analyze-url",
                json={
                    "video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "min_clip_sec": 40,
                    "max_clip_sec": 42,
                },
            )
            assert bad_range.status_code == 422

            res = client.post(
                "/api/videos/analyze-url",
                json={
                    "video_url": "https://youtu.be/dQw4w9WgXcQ",
                    "max_clips": 3,
                    "min_clip_sec": 15,
                    "max_clip_sec": 35,
                    "viral_threshold": 55.0,
                    "use_speech_analysis": True,
                },
            )
            assert res.status_code == 200
            body = res.json()
            assert body["platform"] == "youtube"
            job_id = body["job_id"]

            for _ in range(30):
                job_resp = client.get(f"/api/videos/jobs/{job_id}")
                assert job_resp.status_code == 200
                if job_resp.json()["status"] == "completed":
                    break
                time.sleep(0.05)
            else:
                pytest.fail("Background URL job did not complete")

        assert captured["video_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert captured["kwargs"]["max_clips"] == 3

    def test_analyze_upload_full_pipeline_creates_captioned_clip_and_cleans_up(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ffmpeg_path = resolve_ffmpeg_path()
        if not ffmpeg_path:
            pytest.skip("FFmpeg unavailable in test environment")

        src_mp4 = tmp_path / "sample_recorded.mp4"
        _synth_mp4(ffmpeg_path, src_mp4, duration_sec=14.0)

        mock_words = [
            CaptionWord(start=1.0, end=1.4, word="Wait"),
            CaptionWord(start=1.5, end=1.9, word="why"),
            CaptionWord(start=2.0, end=2.4, word="did"),
            CaptionWord(start=2.5, end=3.1, word="that"),
            CaptionWord(start=3.2, end=3.8, word="insane"),
            CaptionWord(start=3.9, end=4.6, word="moment"),
            CaptionWord(start=4.7, end=5.4, word="happen?!"),
            CaptionWord(start=6.0, end=6.5, word="No"),
            CaptionWord(start=6.6, end=7.1, word="way!"),
        ]
        mock_segments = [
            {
                "start": 1.0,
                "end": 5.4,
                "text": "Wait why did that insane moment happen?!",
                "words": mock_words[:7],
            },
            {
                "start": 6.0,
                "end": 12.2,
                "text": "No way that actually clutched the entire round!",
                "words": mock_words[7:],
            },
        ]

        def _fake_transcribe_window(
            *args: Any, **kwargs: Any
        ) -> tuple[list[CaptionWord], list[dict[str, Any]]]:
            return mock_words, mock_segments

        monkeypatch.setattr(viral_analyzer, "transcribe_window_words", _fake_transcribe_window)

        settings = Settings(data_dir=tmp_path / "data")
        app = create_app(custom_settings=settings)
        with TestClient(app) as client:
            bad_ext = client.post(
                "/api/videos/analyze-upload",
                files={"file": ("malware.exe", b"MZ1234", "application/octet-stream")},
            )
            assert bad_ext.status_code == 422

            bad_missing_job = client.get("/api/videos/jobs/nonexistent-job")
            assert bad_missing_job.status_code == 404

            with src_mp4.open("rb") as fh:
                resp = client.post(
                    "/api/videos/analyze-upload",
                    files={"file": ("recorded_match.mp4", fh, "video/mp4")},
                    data={
                        "max_clips": "2",
                        "min_clip_sec": "8",
                        "max_clip_sec": "20",
                        "viral_threshold": "10",
                        "use_speech_analysis": "true",
                    },
                )
            assert resp.status_code == 200
            job_id = resp.json()["job_id"]
            assert resp.json()["source_type"] == "video_upload"

            job_payload: dict[str, Any] = {}
            for _ in range(120):
                jr = client.get(f"/api/videos/jobs/{job_id}")
                assert jr.status_code == 200
                job_payload = jr.json()
                if job_payload["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.1)
            assert job_payload.get("status") == "completed", job_payload

            clips = client.get("/api/clips").json()
            assert len(clips) == 1
            clip = clips[0]
            assert clip["source_type"] == "video_upload"
            assert clip["captions_enabled"] is True
            parsed_caps = json.loads(clip["captions_json"])
            assert len(parsed_caps) >= 1
            assert Path(clip["raw_video_path"]).is_file()
            assert Path(clip["thumbnail_path"]).is_file()

        # Temporary upload buffer folder must be cleaned up
        leftover_uploads = list((settings.buffers_dir).glob("upload_*"))
        assert leftover_uploads == []

    def test_analyze_upload_enforces_max_size_413_and_cleans_up(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path / "data", max_upload_bytes=1024)
        app = create_app(custom_settings=settings)
        with TestClient(app) as client:
            oversize = b"0" * 4096
            resp = client.post(
                "/api/videos/analyze-upload",
                files={"file": ("huge.mp4", oversize, "video/mp4")},
            )
            assert resp.status_code == 413

        assert list(settings.buffers_dir.glob("upload_*")) == []


class TestExtendedClipDurations:
    """Verify 60s+ default pre-roll, 180s limits, and Clip Now duration overrides."""

    def test_defaults_and_extended_bounds_support_60_to_180_sec(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path / "data")
        assert settings.pre_roll_sec == 60
        assert settings.ring_segment_wrap == 60

        ch_default = ChannelCreate(channel_login="xqc")
        assert ch_default.pre_roll_sec == 60

        ch_max = ChannelCreate(channel_login="caseoh_", pre_roll_sec=180, post_roll_sec=120)
        assert ch_max.pre_roll_sec == 180
        assert ch_max.post_roll_sec == 120

        req_long = VideoAnalyzeUrlRequest(
            video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            min_clip_sec=60,
            max_clip_sec=180,
        )
        assert req_long.min_clip_sec == 60
        assert req_long.max_clip_sec == 180

    def test_clip_now_forwards_duration_sec_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = Settings(data_dir=tmp_path / "data")
        app = create_app(custom_settings=settings)
        calls: list[dict[str, Any]] = []

        async def _fake_clip_now(channel_id: int, duration_sec: float | None = None) -> ClipItem:
            calls.append({"channel_id": channel_id, "duration_sec": duration_sec})
            row = ClipItem(
                channel_id=channel_id,
                channel_login="xqc",
                source_type="live",
                source_ref="https://www.twitch.tv/xqc",
                title="xqc Manual Clip",
                raw_video_path=str(settings.clips_dir / "dummy.mp4"),
                duration_sec=float(duration_sec or 60.0),
                trim_start_sec=0.0,
                trim_end_sec=float(duration_sec or 60.0),
            )
            with app.state.session_factory() as session:
                session.add(row)
                session.commit()
                session.refresh(row)
            return row

        with TestClient(app) as client:
            ch = client.post("/api/channels", json={"channel_login": "xqc"}).json()
            ch_id = ch["id"]
            monkeypatch.setattr(app.state.live_manager, "clip_now", _fake_clip_now)

            res_90 = client.post(
                f"/api/channels/{ch_id}/clip-now",
                json={"duration_sec": 90},
            )
            assert res_90.status_code == 200
            assert res_90.json()["duration_sec"] == pytest.approx(90.0)

            res_default = client.post(f"/api/channels/{ch_id}/clip-now")
            assert res_default.status_code == 200

        assert calls == [
            {"channel_id": ch_id, "duration_sec": 90},
            {"channel_id": ch_id, "duration_sec": None},
        ]
