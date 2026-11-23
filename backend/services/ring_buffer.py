"""Per-channel Streamlink -> FFmpeg rolling MPEG-TS ring buffer and signal pipes."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np

from backend.config import Settings, resolve_ffmpeg_path
from backend.security import safe_resolve_under, validate_twitch_channel

logger = logging.getLogger(__name__)

_AUDIO_SAMPLE_RATE = 16000
_AUDIO_BYTES_PER_SAMPLE = 2
_AUDIO_CHUNK_SEC = 0.5
_AUDIO_CHUNK_BYTES = int(_AUDIO_SAMPLE_RATE * _AUDIO_BYTES_PER_SAMPLE * _AUDIO_CHUNK_SEC)


def resolve_streamlink_command() -> list[str]:
    """Resolve the Streamlink executable in the active venv or PATH."""
    exe_name = "streamlink.exe" if os.name == "nt" else "streamlink"
    venv_candidate = Path(sys.executable).with_name(exe_name)
    if venv_candidate.is_file():
        return [str(venv_candidate)]
    which_hit = shutil.which("streamlink")
    if which_hit:
        return [which_hit]
    return [sys.executable, "-m", "streamlink"]


def ordered_segments(buffer_dir: Path) -> list[Path]:
    """Return existing non-empty `seg_*.ts` files ordered oldest -> newest by mtime."""
    if not buffer_dir.is_dir():
        return []
    items: list[tuple[int, str, Path]] = []
    for entry in buffer_dir.glob("seg_*.ts"):
        try:
            st = entry.stat()
        except OSError:
            continue
        if st.st_size >= 188:
            items.append((st.st_mtime_ns, entry.name, entry))
    items.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in items]


def write_concat_list(segments: list[Path], concat_path: Path) -> Path:
    """Write a safe UTF-8 FFmpeg concat demuxer file using POSIX-escaped paths."""
    lines: list[str] = []
    for seg in segments:
        escaped = seg.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat_path.parent.mkdir(parents=True, exist_ok=True)
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return concat_path


def compute_pcm_dbfs(pcm_bytes: bytes) -> float:
    """Compute RMS dBFS from 16-bit signed little-endian mono PCM samples."""
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable <= 0:
        return -90.0
    samples = np.frombuffer(pcm_bytes[:usable], dtype="<i2").astype(np.float64) / 32768.0
    if samples.size == 0:
        return -90.0
    rms = float(np.sqrt(np.mean(samples * samples)))
    db_level = 20.0 * math.log10(max(rms, 1e-5))
    return max(-90.0, min(0.0, round(db_level, 2)))


def _terminate_process_tree(proc: subprocess.Popen[bytes] | None) -> None:
    """Terminate a subprocess cleanly, escalating to kill if it ignores SIGTERM."""
    if proc is None:
        return
    if proc.poll() is None:
        with suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            with suppress(OSError):
                proc.kill()
            with suppress(OSError, subprocess.TimeoutExpired):
                proc.wait(timeout=2.0)
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            with suppress(OSError, ValueError):
                pipe.close()


class ChannelRingBuffer:
    """Manages Streamlink -> FFmpeg ring buffer, audio RMS pipe, and motion grabs."""

    def __init__(
        self,
        channel_login: str,
        settings: Settings,
        *,
        on_audio_rms: Callable[[float, float], None] | None = None,
        on_motion: Callable[[float, float], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.channel_login = validate_twitch_channel(channel_login)
        self.settings = settings
        self.buffer_dir = safe_resolve_under(settings.buffers_dir, self.channel_login)
        self.ffmpeg_path = settings.ffmpeg_path or resolve_ffmpeg_path()
        self.segment_time = max(2, int(settings.ring_segment_seconds))
        self.segment_wrap = max(10, int(settings.ring_segment_wrap))

        self._on_audio_rms = on_audio_rms
        self._on_motion = on_motion
        self._on_error = on_error

        self._stop_event = asyncio.Event()
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._motion_task: asyncio.Task[None] | None = None
        self._streamlink_proc: subprocess.Popen[bytes] | None = None
        self._ffmpeg_proc: subprocess.Popen[bytes] | None = None
        self._audio_thread: asyncio.Future[None] | None = None
        self._proc_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        """Return True while the ring buffer supervisor is active."""
        return self._running

    def has_segments(self) -> bool:
        """Return True when at least one non-empty MPEG-TS ring segment exists."""
        return bool(ordered_segments(self.buffer_dir))

    def _clear_stale_files(self) -> None:
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        for pattern in ("seg_*.ts", "mframe_*.jpg", "concat_*.txt"):
            for stale in self.buffer_dir.glob(pattern):
                with suppress(OSError):
                    stale.unlink(missing_ok=True)

    def _emit_audio(self, db_level: float, ts: float) -> None:
        if self._on_audio_rms is None:
            return
        try:
            self._on_audio_rms(db_level, ts)
        except Exception:
            logger.exception("Audio RMS callback failed for #%s", self.channel_login)

    def _emit_motion(self, score: float, ts: float) -> None:
        if self._on_motion is None:
            return
        try:
            self._on_motion(score, ts)
        except Exception:
            logger.exception("Motion callback failed for #%s", self.channel_login)

    def _emit_error(self, message: str) -> None:
        if self._on_error is None:
            return
        try:
            self._on_error(message)
        except Exception:
            logger.exception("Ring buffer error callback failed for #%s", self.channel_login)

    def _build_streamlink_cmd(self) -> list[str]:
        return [
            *resolve_streamlink_command(),
            "--stdout",
            "--twitch-low-latency",
            "--retry-streams",
            "2",
            "--retry-max",
            "2",
            f"https://www.twitch.tv/{self.channel_login}",
            "best,720p60,720p,480p,worst",
        ]

    def _build_ffmpeg_cmd(self) -> list[str]:
        seg_pattern = str(self.buffer_dir / "seg_%03d.ts")
        motion_pattern = str(self.buffer_dir / "mframe_%06d.jpg")
        return [
            self.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            # Output 1: rolling MPEG-TS ring buffer
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(self.segment_time),
            "-segment_wrap",
            str(self.segment_wrap),
            "-segment_format",
            "mpegts",
            "-reset_timestamps",
            "1",
            seg_pattern,
            # Output 2: 16kHz mono s16le audio RMS pipe
            "-map",
            "0:a?",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(_AUDIO_SAMPLE_RATE),
            "-f",
            "s16le",
            "pipe:1",
            # Output 3: 1.5 fps low-res frames for motion scoring
            "-map",
            "0:v?",
            "-an",
            "-vf",
            "fps=1.5,scale=160:90",
            "-q:v",
            "6",
            motion_pattern,
        ]

    def _read_audio_pipe_sync(self, proc: subprocess.Popen[bytes], loop: asyncio.AbstractEventLoop) -> None:
        stdout = proc.stdout
        if stdout is None:
            return
        buf = bytearray()
        while not self._stop_event.is_set():
            try:
                chunk = stdout.read(_AUDIO_CHUNK_BYTES - len(buf))
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) >= _AUDIO_CHUNK_BYTES:
                db_level = compute_pcm_dbfs(bytes(buf[:_AUDIO_CHUNK_BYTES]))
                del buf[:_AUDIO_CHUNK_BYTES]
                ts = time.time()
                if not self._stop_event.is_set() and not loop.is_closed():
                    loop.call_soon_threadsafe(self._emit_audio, db_level, ts)

    def _drain_stderr_sync(self, proc: subprocess.Popen[bytes], source_name: str) -> str:
        stderr = proc.stderr
        if stderr is None:
            return ""
        tail_lines: list[str] = []
        while True:
            try:
                raw_line = stderr.readline()
            except (OSError, ValueError):
                break
            if not raw_line:
                break
            decoded = raw_line.decode("utf-8", errors="replace").strip()
            if decoded:
                tail_lines.append(decoded)
                if len(tail_lines) > 12:
                    tail_lines.pop(0)
                logger.debug("[%s #%s] %s", source_name, self.channel_login, decoded)
        return " | ".join(tail_lines)

    async def _motion_poll_loop(self) -> None:
        prev_gray: np.ndarray | None = None
        try:
            while not self._stop_event.is_set():
                frames = sorted(self.buffer_dir.glob("mframe_*.jpg"))
                # Leave the newest frame untouched so FFmpeg finishes flushing it before read.
                ready = frames[:-1] if len(frames) >= 2 else []
                for frame_path in ready:
                    if self._stop_event.is_set():
                        break
                    img = await asyncio.to_thread(cv2.imread, str(frame_path), cv2.IMREAD_GRAYSCALE)
                    with suppress(OSError):
                        frame_path.unlink(missing_ok=True)
                    if img is None or img.size == 0:
                        continue
                    blurred = cv2.GaussianBlur(img, (5, 5), 0)
                    if prev_gray is not None and prev_gray.shape == blurred.shape:
                        diff = cv2.absdiff(blurred, prev_gray)
                        score = round(float(np.mean(diff)) * (100.0 / 255.0), 3)
                        self._emit_motion(score, time.time())
                    prev_gray = blurred
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=0.45)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    async def _run_pipeline_once(self) -> tuple[int, str]:
        if not self.ffmpeg_path:
            raise RuntimeError("FFmpeg path is not configured")

        loop = asyncio.get_running_loop()
        streamlink_cmd = self._build_streamlink_cmd()
        ffmpeg_cmd = self._build_ffmpeg_cmd()

        async with self._proc_lock:
            if self._stop_event.is_set():
                return 0, ""
            streamlink_proc = subprocess.Popen(
                streamlink_cmd,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if streamlink_proc.stdout is None:
                streamlink_proc.kill()
                streamlink_proc.wait(timeout=5)
                raise RuntimeError("Streamlink stdout pipe was not created")
            ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd,
                shell=False,
                stdin=streamlink_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            # Close parent's read handle so FFmpeg sees EOF if Streamlink exits.
            with suppress(OSError):
                streamlink_proc.stdout.close()
            self._streamlink_proc = streamlink_proc
            self._ffmpeg_proc = ffmpeg_proc

        audio_task = asyncio.create_task(
            asyncio.to_thread(self._read_audio_pipe_sync, ffmpeg_proc, loop)
        )
        sl_err_task = asyncio.create_task(
            asyncio.to_thread(self._drain_stderr_sync, streamlink_proc, "streamlink")
        )
        ff_err_task = asyncio.create_task(
            asyncio.to_thread(self._drain_stderr_sync, ffmpeg_proc, "ffmpeg")
        )

        try:
            ff_rc = await asyncio.to_thread(ffmpeg_proc.wait)
            sl_rc = await asyncio.to_thread(streamlink_proc.wait)
            _, sl_err, ff_err = await asyncio.gather(audio_task, sl_err_task, ff_err_task)
            combined_err = ff_err or sl_err
            rc = ff_rc if ff_rc != 0 else sl_rc
            return rc, combined_err
        finally:
            for task in (audio_task, sl_err_task, ff_err_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(audio_task, sl_err_task, ff_err_task, return_exceptions=True)
            async with self._proc_lock:
                _terminate_process_tree(self._ffmpeg_proc)
                _terminate_process_tree(self._streamlink_proc)
                self._ffmpeg_proc = None
                self._streamlink_proc = None

    async def _supervisor_loop(self) -> None:
        backoff = 2.0
        try:
            while not self._stop_event.is_set():
                try:
                    rc, err_msg = await self._run_pipeline_once()
                    if self._stop_event.is_set():
                        break
                    if rc != 0 or err_msg:
                        summary = err_msg or f"Stream ingest exited with code {rc}"
                        logger.warning("Ring buffer exited for #%s: %s", self.channel_login, summary)
                        self._emit_error(summary)
                except asyncio.CancelledError:
                    raise
                except (OSError, RuntimeError) as exc:
                    if self._stop_event.is_set():
                        break
                    logger.warning("Ring buffer error for #%s: %s", self.channel_login, exc)
                    self._emit_error(str(exc))

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(30.0, backoff * 1.5)
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False

    async def start(self) -> None:
        """Start the Streamlink -> FFmpeg ring buffer and motion/audio workers."""
        if self._running:
            return
        if not self.ffmpeg_path:
            raise RuntimeError("FFmpeg executable could not be resolved")

        self._stop_event.clear()
        self._clear_stale_files()
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._motion_task = asyncio.create_task(self._motion_poll_loop())
        self._supervisor_task = asyncio.create_task(self._supervisor_loop())

    async def stop(self) -> None:
        """Terminate running subprocesses and stop background tasks cleanly."""
        self._stop_event.set()
        self._running = False

        async with self._proc_lock:
            _terminate_process_tree(self._streamlink_proc)
            _terminate_process_tree(self._ffmpeg_proc)
            self._streamlink_proc = None
            self._ffmpeg_proc = None

        tasks = [t for t in (self._motion_task, self._supervisor_task) if t is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._motion_task = None
        self._supervisor_task = None

    async def snapshot_clip(
        self,
        pre_roll_sec: float,
        post_roll_sec: float,
        out_path: Path,
    ) -> Path:
        """Wait `post_roll_sec`, select ordered TS segments, and remux `-c copy` MP4."""
        if not self.ffmpeg_path:
            raise RuntimeError("FFmpeg executable could not be resolved")

        trigger_ts = time.time()
        wait_sec = max(0.0, float(post_roll_sec))
        if wait_sec > 0.0:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_sec)
                # Stop requested during post-roll; fall through and snapshot what is buffered.
            except TimeoutError:
                pass

        segments = ordered_segments(self.buffer_dir)
        if not segments:
            raise RuntimeError(f"No ring buffer segments available for #{self.channel_login}")

        window_start = trigger_ts - max(1.0, float(pre_roll_sec)) - float(self.segment_time)
        selected = [seg for seg in segments if seg.stat().st_mtime >= window_start]
        if not selected:
            needed_count = max(
                1,
                math.ceil((max(1.0, float(pre_roll_sec)) + wait_sec) / self.segment_time) + 1,
            )
            selected = segments[-needed_count:]

        target_out = Path(out_path).resolve()
        target_out.parent.mkdir(parents=True, exist_ok=True)
        concat_path = safe_resolve_under(self.buffer_dir, f"concat_{uuid4().hex}.txt")
        write_concat_list(selected, concat_path)

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [
                    self.ffmpeg_path,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_path),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(target_out),
                ],
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
                check=False,
            )
            if proc.returncode != 0 and "aac_adtstoasc" in (proc.stderr or ""):
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [
                        self.ffmpeg_path,
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        str(concat_path),
                        "-c",
                        "copy",
                        "-bsf:a",
                        "aac_adtstoasc",
                        "-movflags",
                        "+faststart",
                        str(target_out),
                    ],
                    shell=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=90,
                    check=False,
                )
            if proc.returncode != 0 or not target_out.is_file() or target_out.stat().st_size == 0:
                detail = (proc.stderr or "").strip() or f"FFmpeg exited with code {proc.returncode}"
                raise RuntimeError(f"Failed to snapshot clip for #{self.channel_login}: {detail}")
        finally:
            with suppress(OSError):
                concat_path.unlink(missing_ok=True)

        return target_out
