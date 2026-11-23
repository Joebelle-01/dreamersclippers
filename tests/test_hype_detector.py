"""Unit and integration tests for Twitch IRC, HypeDetector, RingBuffer, VOD peaks, and LiveManager."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

from backend.config import Settings
from backend.db import get_session_factory, init_db
from backend.models import Channel
from backend.services.hype_detector import HypeDetector
from backend.services.live_manager import LiveManager
from backend.services.ring_buffer import (
    ChannelRingBuffer,
    compute_pcm_dbfs,
    ordered_segments,
    write_concat_list,
)
from backend.services.twitch_irc import (
    TwitchIRCClient,
    build_pong_response,
    generate_anonymous_nick,
    parse_irc_line,
)
from backend.services.vod_scanner import extract_non_overlapping_peaks, scan_twitch_vod


def _seed_normal_chat_baseline(detector: HypeDetector, *, end_ts: float = 100.0) -> None:
    """Populate 60s of steady normal-rate chat (>= 3 msg/min) and calm audio/motion."""
    for step in range(28):
        t = end_ts - 58.0 + step * 2.0
        detector.ingest_chat(f"base_user_{step % 5}", "nice run", ts=t)
        detector.ingest_audio_rms(-32.0, ts=t)
        detector.ingest_motion(4.0, ts=t)


def test_irc_message_parsing_tags_sanitization_and_ping() -> None:
    nick = generate_anonymous_nick()
    assert re.fullmatch(r"justinfan\d{5}", nick)

    assert build_pong_response("PING :tmi.twitch.tv") == "PONG :tmi.twitch.tv"
    assert build_pong_response("PING") == "PONG :tmi.twitch.tv"
    assert build_pong_response("PRIVMSG #shroud :hello") is None

    tagged = (
        "@badge-info=;badges=subscriber/12;display-name=ClipMaster;"
        "emotes=25:0-4/1902:12-16 "
        ":clipmaster!clipmaster@clipmaster.tmi.twitch.tv "
        "PRIVMSG #Shroud :Kappa \x00\x07WOW Keepo CLIP IT!!!"
    )
    msg = parse_irc_line(tagged, expected_channel="shroud", now=1234.5)
    assert msg is not None
    assert msg.author == "ClipMaster"
    assert msg.channel == "shroud"
    assert msg.ts == 1234.5
    assert "\x00" not in msg.text and "\x07" not in msg.text
    assert msg.text == "Kappa WOW Keepo CLIP IT!!!"
    assert "Kappa" in msg.emotes
    assert "Keepo" in msg.emotes

    untagged = ":viewer99!viewer99@viewer99.tmi.twitch.tv PRIVMSG #shroud :POGGERS!!!"
    msg2 = parse_irc_line(untagged, expected_channel="shroud", now=1235.0)
    assert msg2 is not None
    assert msg2.author == "viewer99"
    assert msg2.text == "POGGERS!!!"
    assert msg2.emotes == []

    # Channel mismatch filtered out cleanly
    assert parse_irc_line(untagged, expected_channel="summit1g") is None

    # Action CTCP unwrapped cleanly
    action_line = ":alice!alice@alice.tmi.twitch.tv PRIVMSG #shroud :\x01ACTION clutches 1v4\x01"
    action_msg = parse_irc_line(action_line, expected_channel="shroud", now=1236.0)
    assert action_msg is not None
    assert action_msg.text == "clutches 1v4"


def test_chat_burst_lexicon_and_unique_chatter_anti_spam_gate() -> None:
    spam_detector = HypeDetector("shroud", threshold=68.0, min_unique_chatters=3)
    _seed_normal_chat_baseline(spam_detector, end_ts=100.0)

    # Single spammer sends 25 high-hype messages inside 2 seconds + moderate audio/motion
    for i in range(25):
        spam_detector.ingest_chat("lone_spammer", "KEKW POGGERS CLIP IT!!!", ts=98.2 + i * 0.06)
    spam_detector.ingest_audio_rms(-24.0, ts=99.5)
    spam_detector.ingest_motion(12.0, ts=99.5)

    spam_eval = spam_detector.evaluate(now=100.0)
    assert spam_eval.unique_chatters == 1
    assert spam_eval.chat_score <= 15.0
    assert spam_eval.should_trigger is False
    assert "gated" in spam_eval.reason.lower()

    # Even if 2 normal chatters say hello inside the burst window, 1 dominant spammer stays gated
    spam_detector.ingest_chat("normal_a", "hello", ts=99.0)
    spam_detector.ingest_chat("normal_b", "nice", ts=99.2)
    dom_eval = spam_detector.evaluate(now=100.0)
    assert dom_eval.unique_chatters == 3
    assert dom_eval.chat_score <= 15.0
    assert dom_eval.should_trigger is False
    assert "gated" in dom_eval.reason.lower()

    # Now genuine multi-chatter burst with Twitch lexicon, caps, elongation, and punctuation
    burst_detector = HypeDetector("shroud", threshold=68.0, min_unique_chatters=3)
    _seed_normal_chat_baseline(burst_detector, end_ts=100.0)

    burst_msgs = [
        ("u1", "KEKW NO WAY!!!"),
        ("u2", "OMEGALUL CLIP IT"),
        ("u3", "POGGERS CLUTCHHHH"),
        ("u4", "INSANE PLAY???"),
        ("u5", "LULW WTF"),
        ("u6", "WWWWWWWW"),
        ("u7", "OMG POG"),
        ("u8", "LUL CLIP THAT"),
    ]
    for idx, (user, text) in enumerate(burst_msgs):
        burst_detector.ingest_chat(user, text, ts=98.4 + idx * 0.15)
    burst_detector.ingest_audio_rms(-10.0, ts=99.5)
    burst_detector.ingest_motion(28.0, ts=99.5)

    burst_eval = burst_detector.evaluate(now=100.0)
    assert burst_eval.unique_chatters >= 8
    assert burst_eval.chat_score >= 80.0
    assert burst_eval.weights_mode == "normal"
    assert burst_eval.fused_score >= 68.0
    assert burst_eval.should_trigger is True
    assert len(burst_eval.top_tokens) >= 2
    assert len(burst_eval.recent_samples) >= 8


def test_audio_and_motion_spikes() -> None:
    detector = HypeDetector("shroud", threshold=68.0)
    for i in range(30):
        t = 40.0 + i * 1.9
        detector.ingest_audio_rms(-34.0 + (i % 3) * 0.5, ts=t)
        detector.ingest_motion(0.03 + (i % 2) * 0.005, ts=t)

    # Tiny jitter shouldn't spike
    detector.ingest_audio_rms(-32.8, ts=99.0)
    detector.ingest_motion(0.036, ts=99.0)
    calm = detector.evaluate(now=100.0)
    assert calm.audio_score < 20.0
    assert calm.motion_score < 20.0
    assert calm.should_trigger is False

    # Sharp scream (-7 dBFS) + explosive visual transition (0.45)
    detector.ingest_audio_rms(-7.0, ts=103.0)
    detector.ingest_motion(0.45, ts=103.0)
    spike = detector.evaluate(now=104.0)
    assert spike.audio_score >= 85.0
    assert spike.motion_score >= 85.0


def test_small_chat_fallback_reweighting() -> None:
    # Quiet chat (< 3 msg/min baseline) automatically shifts weights to (0.20, 0.55, 0.25)
    quiet = HypeDetector("smallstreamer", threshold=68.0)
    for i in range(30):
        t = 40.0 + i * 1.9
        quiet.ingest_audio_rms(-32.0, ts=t)
        quiet.ingest_motion(5.0, ts=t)
    # Only 1 chat message in baseline (< 3 msg/min)
    quiet.ingest_chat("mod_1", "hi", ts=70.0)

    quiet.ingest_audio_rms(-6.0, ts=99.2)
    quiet.ingest_motion(38.0, ts=99.2)
    ev = quiet.evaluate(now=100.0)

    assert ev.weights_mode == "quiet_chat"
    assert ev.audio_score >= 90.0
    assert ev.motion_score >= 90.0
    # 0.55*95 + 0.25*95 = 76.0 >= 68.0 (whereas normal 0.30 + 0.20 would only reach ~47.5)
    assert ev.fused_score >= 68.0
    assert ev.should_trigger is True


def test_cooldown_lock_and_hysteresis() -> None:
    detector = HypeDetector("shroud", threshold=68.0, cooldown_sec=35.0)
    _seed_normal_chat_baseline(detector, end_ts=100.0)

    def _inject_burst(t_base: float) -> None:
        for i in range(8):
            detector.ingest_chat(f"chatter_{i}_{int(t_base)}", "POGGERS KEKW CLIP IT!!!", ts=t_base + i * 0.1)
        detector.ingest_audio_rms(-6.0, ts=t_base + 0.5)
        detector.ingest_motion(32.0, ts=t_base + 0.5)

    # First trigger at t=100
    _inject_burst(98.5)
    first = detector.evaluate(now=100.0)
    assert first.should_trigger is True

    # Burst 12 seconds later is blocked by 35s cooldown
    _inject_burst(110.5)
    during_cd = detector.evaluate(now=112.0)
    assert during_cd.fused_score >= 68.0
    assert during_cd.should_trigger is False
    assert "cooldown" in during_cd.reason.lower()

    # Let score cool down below hysteresis floor (0.75 * 68 = 51.0)
    for t_step in range(118, 134, 2):
        detector.ingest_chat(f"calm_{t_step}", "lol", ts=float(t_step))
        detector.ingest_audio_rms(-32.0, ts=float(t_step))
        detector.ingest_motion(4.0, ts=float(t_step))
    cooled = detector.evaluate(now=133.0)
    assert cooled.fused_score < 51.0

    # After cooldown expires (t=137 >= 100 + 35), fresh burst triggers again
    _inject_burst(135.5)
    second = detector.evaluate(now=137.0)
    assert second.should_trigger is True


def test_ring_buffer_wraparound_ordering_and_snapshot(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_data_dirs()

    buf_dir = settings.buffers_dir / "shroud"
    buf_dir.mkdir(parents=True, exist_ok=True)

    # Synthesize 3 real 1s MPEG-TS segments simulating wraparound: seg_034 (oldest), seg_000, seg_001
    for idx, seg_name in enumerate(("seg_034.ts", "seg_000.ts", "seg_001.ts")):
        seg_path = buf_dir / seg_name
        proc = subprocess.run(
            [
                settings.ffmpeg_path,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=640x360:r=30:d=1",
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
                "-f",
                "mpegts",
                str(seg_path),
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
        os.utime(seg_path, (1000 + idx * 4, 1000 + idx * 4))

    ordered = ordered_segments(buf_dir)
    assert [p.name for p in ordered] == ["seg_034.ts", "seg_000.ts", "seg_001.ts"]

    concat_file = tmp_path / "concat.txt"
    write_concat_list(ordered, concat_file)
    concat_text = concat_file.read_text(encoding="utf-8")
    assert concat_text.index("seg_034.ts") < concat_text.index("seg_000.ts") < concat_text.index("seg_001.ts")

    # Verify PCM RMS helper on silent vs loud buffer
    assert compute_pcm_dbfs(b"\x00\x00" * 1000) == -90.0
    loud_sample = (16000).to_bytes(2, "little", signed=True) * 1000
    assert -8.0 < compute_pcm_dbfs(loud_sample) < -4.0

    ring = ChannelRingBuffer("shroud", settings)
    out_mp4 = settings.clips_dir / "snap_test.mp4"
    result_path = asyncio.run(ring.snapshot_clip(pre_roll_sec=12.0, post_roll_sec=0.0, out_path=out_mp4))
    assert result_path.is_file()
    assert result_path.stat().st_size > 0


def test_vod_peak_extraction_nms_and_live_manager_preset(tmp_path: Path) -> None:
    timeline = [
        {"t": float(t), "chat": 10.0, "audio": 20.0, "motion": 10.0, "fused": 20.0}
        for t in range(1, 121)
    ]
    timeline[19]["fused"] = 85.0  # t=20
    timeline[24]["fused"] = 92.0  # t=25 (within 20s separation of t=20; t=25 should win!)
    timeline[79]["fused"] = 78.0  # t=80

    peaks = extract_non_overlapping_peaks(
        timeline,
        threshold=65.0,
        max_clips=5,
        min_separation_sec=20.0,
    )
    assert [p["t"] for p in peaks] == [25.0, 80.0]

    # LiveManager honors saved channel preset and emits clip_created to subscribers
    settings = Settings(data_dir=tmp_path / "live_data")
    settings.ensure_data_dirs()
    init_db(settings.db_path)
    session_factory = get_session_factory(settings.db_path)

    preset_cam = {"x": 0.70, "y": 0.05, "w": 0.25, "h": 0.25}
    preset_game = {"x": 0.20, "y": 0.0, "w": 0.60, "h": 1.0}
    with session_factory() as session:
        ch = Channel(
            channel_login="shroud",
            display_name="Shroud",
            preset_layout_mode="split_top_cam",
            preset_split_ratio=0.40,
            preset_webcam_json=json.dumps(preset_cam),
            preset_gameplay_json=json.dumps(preset_game),
        )
        session.add(ch)
        session.commit()
        session.refresh(ch)
        ch_id = int(ch.id or 0)

    # Create a real synthetic segment in buffers/shroud so clip_now can snapshot immediately
    buf_dir = settings.buffers_dir / "shroud"
    buf_dir.mkdir(parents=True, exist_ok=True)
    seg_path = buf_dir / "seg_000.ts"
    proc = subprocess.run(
        [
            settings.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=640x360:r=30:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=520:sample_rate=48000:duration=1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-f",
            "mpegts",
            str(seg_path),
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

    async def _run_live_flow() -> None:
        manager = LiveManager(settings, session_factory)
        sub_q = manager.subscribe()
        try:
            # Register monitor state directly without opening external Twitch sockets
            ring = ChannelRingBuffer("shroud", settings)
            detector = HypeDetector("shroud")
            from backend.services.live_manager import _ChannelMonitor
            from backend.services.twitch_irc import TwitchIRCClient

            monitor = _ChannelMonitor(
                channel_id=ch_id,
                channel_login="shroud",
                detector=detector,
                irc=TwitchIRCClient("shroud", lambda _m: None),
                ring=ring,
            )
            manager._monitors[ch_id] = monitor
            assert manager.active_channel_ids() == [ch_id]

            clip = await manager.clip_now(ch_id)
            assert clip.status == "pending_review"
            assert clip.layout_mode == "split_top_cam"
            assert clip.split_ratio == 0.40
            assert json.loads(clip.webcam_box_json or "{}") == preset_cam
            assert json.loads(clip.gameplay_box_json or "{}") == preset_game
            assert clip.thumbnail_path and Path(clip.thumbnail_path).is_file()
            assert Path(clip.raw_video_path).is_file()

            evt = await asyncio.wait_for(sub_q.get(), timeout=2.0)
            assert evt["type"] == "clip_created"
            assert evt["payload"]["id"] == clip.id
        finally:
            manager._monitors.clear()
            manager.unsubscribe(sub_q)

    asyncio.run(_run_live_flow())


def test_twitch_irc_client_ws_handshake_ping_and_privmsg() -> None:
    import websockets.asyncio.server as ws_server

    async def _run_irc_test() -> None:
        received_cmds: list[str] = []
        parsed_msgs: list[str] = []
        got_msg = asyncio.Event()

        async def _handler(conn: ws_server.ServerConnection) -> None:
            for _ in range(4):
                frame = await conn.recv()
                for ln in str(frame).splitlines():
                    if ln.strip():
                        received_cmds.append(ln.strip())
            await conn.send("PING :tmi.twitch.tv\r\n")
            pong = await conn.recv()
            received_cmds.append(str(pong).strip())
            await conn.send(
                ":tester!tester@tester.tmi.twitch.tv PRIVMSG #shroud :POGGERS \x01clip\r\n"
            )
            await asyncio.sleep(0.2)

        async with ws_server.serve(_handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]

            def _on_msg(msg: object) -> None:
                parsed_msgs.append(getattr(msg, "text", ""))
                got_msg.set()

            client = TwitchIRCClient("shroud", _on_msg, ws_url=f"ws://127.0.0.1:{port}")
            task = asyncio.create_task(client.run())
            try:
                await asyncio.wait_for(got_msg.wait(), timeout=5.0)
            finally:
                await client.stop()
                await asyncio.wait_for(task, timeout=5.0)

        assert any(re.fullmatch(r"NICK justinfan\d{5}", cmd) for cmd in received_cmds)
        assert "JOIN #shroud" in received_cmds
        assert "PONG :tmi.twitch.tv" in received_cmds
        assert parsed_msgs == ["POGGERS clip"]

    asyncio.run(_run_irc_test())


def test_vod_scanner_ssrf_and_audio_motion_fallback(
    tmp_path: Path, monkeypatch: object
) -> None:
    import pytest

    settings = Settings(data_dir=tmp_path / "vod_data")
    settings.ensure_data_dirs()
    init_db(settings.db_path)
    session_factory = get_session_factory(settings.db_path)

    with pytest.raises(ValueError):
        asyncio.run(
            scan_twitch_vod(
                "http://127.0.0.1:8000/videos/123",
                None,
                3,
                settings,
                session_factory,
                None,
            )
        )

    # Synthesize a 12s MP4: calm dark/quiet 0..8s, loud sine + high-motion flash at 8..11s
    synth_vod = tmp_path / "synthetic_vod.mp4"
    proc = subprocess.run(
        [
            settings.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=640x360:r=15:d=8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=48000:duration=8,volume=0.01",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=640x360:r=15:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=4,volume=0.8",
            "-filter_complex",
            "[0:v][1:a][2:v][3:a]concat=n=2:v=1:a=1[v][a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(synth_vod),
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    def _fake_download(vod_url: str, work_dir: Path, ffmpeg_path: str) -> tuple[Path, dict]:
        copied = work_dir / "999999.mp4"
        copied.write_bytes(synth_vod.read_bytes())
        return copied, {"title": "Test Clutch VOD", "uploader_id": "shroud", "duration": 12.0}

    monkeypatch.setattr("backend.services.vod_scanner._download_vod_with_ytdlp_sync", _fake_download)

    events: list[tuple[str, dict]] = []

    async def _broadcast(ev_type: str, payload: dict) -> None:
        events.append((ev_type, payload))

    result = asyncio.run(
        scan_twitch_vod(
            "https://www.twitch.tv/videos/999999",
            None,
            2,
            settings,
            session_factory,
            _broadcast,
            hype_threshold=55.0,
        )
    )
    assert result["used_chat_replay"] is False
    assert len(result["clips_created"]) >= 1
    assert any(t == "vod_progress" and p.get("status") == "completed" for t, p in events)
    assert any(t == "clip_created" for t, _ in events)


def test_external_boost_decay_tokens_and_cap() -> None:
    detector = HypeDetector("shroud", threshold=68.0, cooldown_sec=35.0)
    _seed_normal_chat_baseline(detector, end_ts=100.0)

    # Moderate audio/motion below threshold (~35 fused)
    detector.ingest_audio_rms(-18.0, ts=99.5)
    detector.ingest_motion(16.0, ts=99.5)
    before = detector.evaluate(now=100.0)
    assert before.should_trigger is False
    base_score = before.fused_score

    # Inject +40 Hype Train boost at t=100.0 -> pushes past 68 and appends label
    detector.ingest_external_boost(40.0, "Hype Train", ts=100.0)
    at_peak = detector.evaluate(now=100.0)
    assert at_peak.fused_score == min(100.0, round(base_score + 40.0, 2))
    assert at_peak.should_trigger is True
    assert "Hype Train" in at_peak.top_tokens
    assert "Hype Train" in at_peak.reason

    # Verify linear decay after 7.5s (~50% remaining -> +20 pts)
    half_decayed = detector.evaluate(now=107.5)
    assert "Hype Train" in half_decayed.top_tokens
    assert half_decayed.fused_score < at_peak.fused_score

    # Verify expiration after 15.0s
    expired = detector.evaluate(now=115.5)
    assert "Hype Train" not in expired.top_tokens

    # Verify capping at 100.0 when stacking multiple boosts
    detector.ingest_external_boost(80.0, "Channel raid", ts=140.0)
    detector.ingest_external_boost(50.0, "Cheer burst", ts=140.0)
    capped = detector.evaluate(now=140.0)
    assert capped.fused_score == 100.0
    assert "Channel raid" in capped.top_tokens
    assert "Cheer burst" in capped.top_tokens

