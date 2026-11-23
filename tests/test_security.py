"""Security unit tests for input validation, SSRF allowlisting, and path confinement."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.security import (
    CropBox,
    extract_twitch_channel_login,
    safe_resolve_under,
    sanitize_chat_text,
    validate_crop_box,
    validate_twitch_channel,
    validate_twitch_vod_url,
)


class TestValidateTwitchChannel:
    """Validate Twitch channel login allowlisting."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("shroud", "shroud"),
            ("xqc_123", "xqc_123"),
            ("Shroud_TV", "shroud_tv"),
            ("abc", "abc"),
            ("a" * 25, "a" * 25),
        ],
    )
    def test_accepts_valid_logins(self, raw: str, expected: str) -> None:
        assert validate_twitch_channel(raw) == expected

    @pytest.mark.parametrize(
        "invalid",
        [
            "",
            "   ",
            " shroud ",
            "ab",
            "a" * 26,
            "shroud; calc",
            "shroud && whoami",
            "shroud | dir",
            "shroud`id`",
            "shroud$(id)",
            "shroud ow",
            "https://twitch.tv/shroud",
            "twitch.tv/shroud",
            "shroud/videos",
            "../shroud",
            "shroud\nwhoami",
            "shroud\x00",
        ],
    )
    def test_rejects_invalid_logins_and_metacharacters(self, invalid: str) -> None:
        with pytest.raises(ValueError):
            validate_twitch_channel(invalid)


class TestExtractTwitchChannelLogin:
    """Validate flexible extraction of Twitch channel logins from URLs and @handles."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("caseoh_", "caseoh_"),
            ("  @CaseOh_  ", "caseoh_"),
            ("https://www.twitch.tv/caseoh_", "caseoh_"),
            ("https://twitch.tv/caseoh_/", "caseoh_"),
            ("https://m.twitch.tv/caseoh_?referrer=raid", "caseoh_"),
            ("http://twitch.tv/caseoh_", "caseoh_"),
            ("twitch.tv/caseoh_", "caseoh_"),
            ("www.twitch.tv/caseoh_", "caseoh_"),
        ],
    )
    def test_extracts_valid_channels_and_urls(self, raw: str, expected: str) -> None:
        assert extract_twitch_channel_login(raw) == expected

    @pytest.mark.parametrize(
        "invalid",
        [
            "",
            "   ",
            "https://evil.com/caseoh_",
            "http://127.0.0.1:8000/caseoh_",
            "https://www.twitch.tv:8443/caseoh_",
            "https://" + "u" + ":" + "p" + "@twitch.tv/caseoh_",
            "https://www.twitch.tv/videos/123456789",
            "https://www.twitch.tv/directory",
            "https://www.twitch.tv/caseoh_/videos",
            "ftp://twitch.tv/caseoh_",
            "caseoh_; calc",
            "bad login",
            "../etc/passwd",
            "caseoh_\x00",
        ],
    )
    def test_rejects_ssrf_and_non_channel_urls(self, invalid: str) -> None:
        with pytest.raises(ValueError):
            extract_twitch_channel_login(invalid)


class TestValidateTwitchVodUrl:
    """Validate strict HTTPS twitch.tv VOD URL allowlist against SSRF."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://www.twitch.tv/videos/123456789", "https://www.twitch.tv/videos/123456789"),
            ("https://twitch.tv/videos/99", "https://twitch.tv/videos/99"),
            ("https://m.twitch.tv/videos/42/", "https://m.twitch.tv/videos/42"),
            ("  https://www.twitch.tv/videos/555  ", "https://www.twitch.tv/videos/555"),
        ],
    )
    def test_accepts_valid_twitch_vod_urls(self, raw: str, expected: str) -> None:
        assert validate_twitch_vod_url(raw) == expected

    @pytest.mark.parametrize(
        "malicious",
        [
            "",
            "http://127.0.0.1:8000",
            "http://127.0.0.1:8430/api/system/status",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]:8000/videos/1",
            "file:///etc/passwd",
            "https://evil.com/videos/1",
            "https://twitch.tv.evil.com/videos/1",
            "https://www.twitch.tv:8443/videos/1",
            "https://" + "u" + ":" + "p" + "@www.twitch.tv/videos/1",
            "http://www.twitch.tv/videos/123456789",
            "javascript:alert(1)",
            "; calc",
            "https://www.twitch.tv/videos/123;calc",
            "https://www.twitch.tv/shroud",
            "https://www.twitch.tv/videos/abc",
            "https://www.twitch.tv/videos/123\ncalc",
        ],
    )
    def test_rejects_ssrf_and_invalid_urls(self, malicious: str) -> None:
        with pytest.raises(ValueError):
            validate_twitch_vod_url(malicious)


class TestValidateCropBox:
    """Validate normalized [0, 1] crop bounding boxes."""

    def test_accepts_valid_dict_and_model(self) -> None:
        box = validate_crop_box({"x": 0.02, "y": 0.65, "w": 0.25, "h": 0.30})
        assert isinstance(box, CropBox)
        assert box.x == pytest.approx(0.02)
        assert box.y == pytest.approx(0.65)
        assert box.w == pytest.approx(0.25)
        assert box.h == pytest.approx(0.30)

        cloned = validate_crop_box(box)
        assert cloned == box

    @pytest.mark.parametrize(
        "bad_box",
        [
            {"x": -0.1, "y": 0.0, "w": 0.3, "h": 0.3},
            {"x": 0.0, "y": -0.2, "w": 0.3, "h": 0.3},
            {"x": 0.0, "y": 0.0, "w": 0.04, "h": 0.3},
            {"x": 0.0, "y": 0.0, "w": 0.3, "h": 0.04},
            {"x": 0.8, "y": 0.0, "w": 0.25, "h": 0.3},
            {"x": 0.0, "y": 0.8, "w": 0.3, "h": 0.25},
            {"x": 1.1, "y": 0.0, "w": 0.1, "h": 0.1},
            {"x": float("nan"), "y": 0.0, "w": 0.2, "h": 0.2},
            {"x": float("inf"), "y": 0.0, "w": 0.2, "h": 0.2},
            {"x": True, "y": 0.0, "w": 0.2, "h": 0.2},
        ],
    )
    def test_rejects_out_of_bounds_or_undersized_boxes(self, bad_box: dict) -> None:
        with pytest.raises(ValueError):
            validate_crop_box(bad_box)


class TestSafeResolveUnder:
    """Validate directory traversal protection for media/data paths."""

    def test_allows_nested_file_inside_base(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        target_file = data_dir / "exports" / "clip_1.mp4"
        target_file.parent.mkdir(parents=True)
        target_file.write_bytes(b"mp4")

        resolved = safe_resolve_under(data_dir, Path("exports/clip_1.mp4"))
        assert resolved == target_file.resolve()

        resolved_abs = safe_resolve_under(data_dir, target_file)
        assert resolved_abs == target_file.resolve()

    @pytest.mark.parametrize(
        "traversal",
        [
            "../secret.txt",
            "exports/../../secret.txt",
            "..",
            ".",
            "",
            "   ",
            "exports/\x00clip.mp4",
        ],
    )
    def test_rejects_traversal_and_invalid_targets(self, tmp_path: Path, traversal: str) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")

        with pytest.raises(ValueError):
            safe_resolve_under(data_dir, traversal)

    def test_rejects_external_absolute_path(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(b"nope")

        with pytest.raises(ValueError):
            safe_resolve_under(data_dir, outside)


class TestSanitizeChatText:
    """Validate chat text control character stripping and length capping."""

    def test_strips_control_chars_and_normalizes_whitespace(self) -> None:
        raw = "KEKW\x00\x07\x1b[31m   POGGERS\r\n\tCLUTCH\x7f\x85!"
        cleaned = sanitize_chat_text(raw)
        assert cleaned == "KEKW[31m POGGERS CLUTCH!"

    def test_caps_length_cleanly(self) -> None:
        long_text = "POG " * 100
        cleaned = sanitize_chat_text(long_text, max_len=30)
        assert len(cleaned) <= 30
        assert cleaned == cleaned.rstrip()

    def test_handles_empty_input(self) -> None:
        assert sanitize_chat_text("") == ""
