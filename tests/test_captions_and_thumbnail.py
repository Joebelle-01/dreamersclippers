"""Tests for local Closed Captions (ASS/SRT burn-in), 9:16 cover thumbnails, and DB migration."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.db import dispose_engines, init_db
from backend.main import create_app
from backend.models import CaptionSegment, CaptionWord, ClipItem
from backend.services.caption_service import (
    build_ass_subtitles,
    build_srt_subtitles,
    escape_ffmpeg_filter_path,
    sanitize_ass_text,
)
from backend.services.video_renderer import render_vertical_clip, render_vertical_thumbnail


def _synthesize_test_mp4(ffmpeg_path: str, out_path: Path, duration_sec: float = 2.0) -> None:
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


class TestCaptionFormatting:
    """Verify ASS/SRT generation, sanitization, and FFmpeg Windows path escaping."""

    @pytest.mark.parametrize("style", ["bold_pop", "clean_box", "classic_cc"])
    def test_build_ass_subtitles_all_styles(self, style: str) -> None:
        segments = [
            CaptionSegment(
                start=0.0,
                end=1.2,
                text="NO WAY {override}\\N bro",
                words=[
                    CaptionWord(start=0.0, end=0.4, word="NO"),
                    CaptionWord(start=0.4, end=0.8, word="WAY"),
                    CaptionWord(start=0.8, end=1.2, word="bro"),
                ],
            )
        ]
        ass = build_ass_subtitles(
            segments,
            layout_mode="split_top_cam",
            split_ratio=0.35,
            caption_style=style,
        )
        assert "PlayResX: 1080" in ass
        assert "PlayResY: 1920" in ass
        assert "Dialogue: 0," in ass
        # User braces/backslashes must not inject raw ASS override tags
        assert "{override}" not in ass
        assert "\\N" not in ass

    def test_build_srt_subtitles(self) -> None:
        segments = [
            CaptionSegment(start=0.5, end=1.75, text="CLUTCH WIN"),
            CaptionSegment(start=1.8, end=3.0, text="WHAT A PLAY"),
        ]
        srt = build_srt_subtitles(segments)
        assert "1\n00:00:00,500 --> 00:00:01,750\nCLUTCH WIN" in srt
        assert "2\n00:00:01,800 --> 00:00:03,000\nWHAT A PLAY" in srt

    def test_sanitize_and_escape_path(self, tmp_path: Path) -> None:
        assert sanitize_ass_text("Hi\x00 {bad}\\n line\n") == "Hi (bad) n line"
        escaped = escape_ffmpeg_filter_path(tmp_path / "test_subs.ass")
        assert "\\" in escaped or "/" in escaped
        if ":" in str(tmp_path.resolve()):
            assert "\\:" in escaped

    @pytest.mark.parametrize(
        ("split_ratio", "expected_seam_y"),
        [(0.35, 672), (0.40, 768), (0.50, 960)],
    )
    def test_build_ass_seam_placement(self, split_ratio: float, expected_seam_y: int) -> None:
        segments = [
            CaptionSegment(
                start=0.0,
                end=1.2,
                text="CLUTCH SEAM",
                words=[
                    CaptionWord(start=0.0, end=0.6, word="CLUTCH"),
                    CaptionWord(start=0.6, end=1.2, word="SEAM"),
                ],
            )
        ]
        ass = build_ass_subtitles(
            segments,
            layout_mode="split_top_cam",
            split_ratio=split_ratio,
            caption_style="bold_pop",
            caption_position="seam",
        )
        style_line = next(line for line in ass.splitlines() if line.startswith("Style: Default,"))
        style_fields = style_line.split(",")
        assert style_fields[18] == "5"
        assert style_fields[21] in {"0", "20"}

        dialogue_lines = [line for line in ass.splitlines() if line.startswith("Dialogue: ")]
        assert len(dialogue_lines) == 2
        for line in dialogue_lines:
            assert f"\\pos(540,{expected_seam_y})" in line

    def test_build_ass_lower_safe_and_blur_fallback(self) -> None:
        segments = [CaptionSegment(start=0.2, end=1.5, text="LOWER SAFE CAPTION")]

        lower_ass = build_ass_subtitles(
            segments,
            layout_mode="split_top_cam",
            split_ratio=0.35,
            caption_style="clean_box",
            caption_position="lower_safe",
        )
        lower_style = next(line for line in lower_ass.splitlines() if line.startswith("Style: Default,"))
        lower_fields = lower_style.split(",")
        assert lower_fields[18] == "2"
        assert lower_fields[21] == "420"
        assert "\\pos(540," not in lower_ass

        high_split_lower = build_ass_subtitles(
            segments,
            layout_mode="split_top_cam",
            split_ratio=0.50,
            caption_style="classic_cc",
            caption_position="lower_safe",
        )
        high_fields = next(
            line for line in high_split_lower.splitlines() if line.startswith("Style: Default,")
        ).split(",")
        assert high_fields[18] == "2"
        assert high_fields[21] == "340"

        blur_ass = build_ass_subtitles(
            segments,
            layout_mode="blur_no_cam",
            split_ratio=0.35,
            caption_style="bold_pop",
            caption_position="seam",
        )
        blur_fields = next(
            line for line in blur_ass.splitlines() if line.startswith("Style: Default,")
        ).split(",")
        assert blur_fields[18] == "2"
        assert blur_fields[21] == "360"
        assert "\\pos(540," not in blur_ass


class TestVerticalRenderWithCcAndThumb:
    """Verify real FFmpeg burn-in ASS subtitles and 1080x1920 JPEG cover thumbnails."""

    def test_ffmpeg_renders_1080x1920_with_burned_ass_and_thumbnails(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path / "data")
        src = tmp_path / "source.mp4"
        _synthesize_test_mp4(settings.ffmpeg_path, src, duration_sec=2.0)

        ass_path = tmp_path / "subs.ass"
        ass_text = build_ass_subtitles(
            [
                CaptionSegment(
                    start=0.1,
                    end=1.6,
                    text="INSANE CLUTCH",
                    words=[
                        CaptionWord(start=0.1, end=0.8, word="INSANE"),
                        CaptionWord(start=0.8, end=1.6, word="CLUTCH"),
                    ],
                )
            ],
            layout_mode="split_top_cam",
            split_ratio=0.35,
            caption_style="bold_pop",
            caption_position="seam",
        )
        assert "\\pos(540,672)" in ass_text
        ass_path.write_text(ass_text, encoding="utf-8")

        out_mp4 = tmp_path / "vertical_cc.mp4"
        result = render_vertical_clip(
            raw_video_path=src,
            output_path=out_mp4,
            trim_start_sec=0.0,
            trim_end_sec=1.8,
            layout_mode="split_top_cam",
            split_ratio=0.35,
            webcam_box=None,
            gameplay_box=None,
            normalize_audio=False,
            ffmpeg_path=settings.ffmpeg_path,
            use_nvenc=settings.nvenc_available,
            ass_subtitle_path=ass_path,
        )
        assert Path(result.output_path).is_file()
        probe = _probe_media(settings.ffprobe_path, out_mp4)
        v_stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
        assert v_stream["width"] == 1080
        assert v_stream["height"] == 1920
        assert v_stream["sample_aspect_ratio"] == "1:1"

        plain_thumb = tmp_path / "cover_plain.jpg"
        render_vertical_thumbnail(
            raw_video_path=src,
            output_path=plain_thumb,
            at_sec=0.8,
            layout_mode="split_top_cam",
            split_ratio=0.35,
            webcam_box=None,
            gameplay_box=None,
            ffmpeg_path=settings.ffmpeg_path,
            title_overlay_text=None,
        )
        assert plain_thumb.is_file() and plain_thumb.stat().st_size > 1000
        tp = _probe_media(settings.ffprobe_path, plain_thumb)
        t_stream = next(s for s in tp["streams"] if s["codec_type"] == "video")
        assert t_stream["width"] == 1080
        assert t_stream["height"] == 1920

        titled_thumb = tmp_path / "cover_titled.jpg"
        render_vertical_thumbnail(
            raw_video_path=src,
            output_path=titled_thumb,
            at_sec=0.8,
            layout_mode="blur_no_cam",
            split_ratio=0.35,
            webcam_box=None,
            gameplay_box=None,
            ffmpeg_path=settings.ffmpeg_path,
            title_overlay_text="CASEOH REACTS LIVE",
        )
        assert titled_thumb.is_file() and titled_thumb.stat().st_size > 1000
        tp2 = _probe_media(settings.ffprobe_path, titled_thumb)
        t2_stream = next(s for s in tp2["streams"] if s["codec_type"] == "video")
        assert t2_stream["width"] == 1080
        assert t2_stream["height"] == 1920


class TestDbMigrationAndEndpoints:
    """Verify legacy DB migration and caption/thumbnail/render/download endpoints."""

    def test_init_db_migrates_legacy_clipitem_columns(self, tmp_path: Path) -> None:
        db_file = tmp_path / "legacy.db"
        conn = sqlite3.connect(db_file)
        conn.execute(
            """
            CREATE TABLE clipitem (
                id TEXT PRIMARY KEY,
                channel_id INTEGER,
                channel_login TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                hype_score REAL NOT NULL,
                trigger_reason TEXT NOT NULL,
                trigger_chat_samples_json TEXT NOT NULL,
                hype_timeline_json TEXT NOT NULL,
                raw_video_path TEXT NOT NULL,
                thumbnail_path TEXT,
                export_video_path TEXT,
                duration_sec REAL NOT NULL,
                trim_start_sec REAL NOT NULL,
                trim_end_sec REAL NOT NULL,
                layout_mode TEXT NOT NULL,
                split_ratio REAL NOT NULL,
                webcam_box_json TEXT,
                gameplay_box_json TEXT,
                webcam_confidence REAL NOT NULL,
                normalize_audio INTEGER NOT NULL,
                encoder_used TEXT,
                error_message TEXT,
                captured_at TEXT NOT NULL,
                exported_at TEXT
            )
            """
        )
        conn.commit()
        conn.close()

        init_db(db_file)
        conn2 = sqlite3.connect(db_file)
        cols = {row[1] for row in conn2.execute("PRAGMA table_info(clipitem)").fetchall()}
        conn2.close()
        dispose_engines()

        assert {
            "captions_enabled",
            "caption_style",
            "captions_json",
            "export_srt_path",
            "export_thumb_path",
            "thumb_time_sec",
            "thumb_title_overlay",
        }.issubset(cols)

    def test_caption_thumbnail_and_render_endpoints(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path / "data")
        app = create_app(custom_settings=settings)
        with TestClient(app) as client:
            ch_resp = client.post("/api/channels", json={"channel_login": "caseoh_"})
            assert ch_resp.status_code == 201
            ch_id = ch_resp.json()["id"]

            raw_clip_path = settings.clips_dir / "cc_test_clip.mp4"
            _synthesize_test_mp4(settings.ffmpeg_path, raw_clip_path, duration_sec=2.0)

            clip_id = "clip-cc-thumb-001"
            with app.state.session_factory() as session:
                session.add(
                    ClipItem(
                        id=clip_id,
                        channel_id=ch_id,
                        channel_login="caseoh_",
                        source_type="live",
                        source_ref="live:caseoh_",
                        title="No Way Chat",
                        status="pending_review",
                        hype_score=91.0,
                        trigger_reason="chat+audio spike",
                        raw_video_path=str(raw_clip_path),
                        duration_sec=2.0,
                        trim_start_sec=0.0,
                        trim_end_sec=2.0,
                    )
                )
                session.commit()

            mocked_segments = [
                CaptionSegment(
                    start=0.0,
                    end=1.4,
                    text="NO WAY CHAT",
                    words=[
                        CaptionWord(start=0.0, end=0.4, word="NO"),
                        CaptionWord(start=0.4, end=0.9, word="WAY"),
                        CaptionWord(start=0.9, end=1.4, word="CHAT"),
                    ],
                )
            ]
            with patch("backend.main.transcribe_clip", return_value=mocked_segments):
                gen_resp = client.post(f"/api/clips/{clip_id}/captions/generate")
            assert gen_resp.status_code == 200
            gen_data = gen_resp.json()
            assert gen_data["captions_enabled"] is True
            assert len(gen_data["captions"]) == 1
            assert gen_data["captions"][0]["text"] == "NO WAY CHAT"

            # Edit caption inline and choose clean_box style
            patch_resp = client.patch(
                f"/api/clips/{clip_id}",
                json={
                    "caption_style": "clean_box",
                    "captions": [{"start": 0.0, "end": 1.4, "text": "EDITED CAPTION", "words": []}],
                },
            )
            assert patch_resp.status_code == 200
            assert patch_resp.json()["caption_style"] == "clean_box"
            assert patch_resp.json()["captions"][0]["text"] == "EDITED CAPTION"

            # Capture 9:16 cover thumbnail before render
            thumb_resp = client.post(
                f"/api/clips/{clip_id}/thumbnail",
                json={"at_sec": 0.6, "title_overlay": True},
            )
            assert thumb_resp.status_code == 200
            assert thumb_resp.json()["export_thumb_path"] is not None
            assert thumb_resp.json()["thumb_title_overlay"] is True

            thumb_dl = client.get(f"/api/media/export-thumb/{clip_id}")
            assert thumb_dl.status_code == 200
            assert thumb_dl.headers["content-type"].startswith("image/jpeg")

            # Render MP4 with CC burned in + SRT + 9:16 cover thumbnail
            render_resp = client.post(f"/api/clips/{clip_id}/render")
            assert render_resp.status_code == 200

            final_clip = None
            for _ in range(60):
                poll = client.get(f"/api/clips/{clip_id}")
                assert poll.status_code == 200
                final_clip = poll.json()
                if final_clip["status"] in ("exported", "failed"):
                    break
                time.sleep(0.1)

            assert final_clip is not None
            assert final_clip["status"] == "exported", final_clip.get("error_message")
            assert Path(final_clip["export_video_path"]).is_file()
            assert Path(final_clip["export_srt_path"]).is_file()
            assert Path(final_clip["export_thumb_path"]).is_file()

            srt_dl = client.get(f"/api/media/srt/{clip_id}")
            assert srt_dl.status_code == 200
            assert "EDITED CAPTION" in srt_dl.text
