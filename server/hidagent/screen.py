"""The phone's screen, received over AirPlay mirroring by UxPlay, as JPEG frames."""

import asyncio
import logging
import os
import subprocess
import threading
import time

log = logging.getLogger(__name__)


class Screen:
    """Runs UxPlay with a GStreamer sink that writes JPEGs into a pipe; keeps the latest one."""

    def __init__(self, name: str = "iPhone Agent", fps: int = 8, quality: int = 75):
        self.name, self.fps, self.quality = name, fps, quality
        self.frame: bytes | None = None
        self.updated = 0.0
        self._proc = None
        self._stopping = False

    def start(self) -> None:
        threading.Thread(target=self._supervise, daemon=True).start()

    def stop(self) -> None:
        self._stopping = True
        if self._proc:
            self._proc.terminate()

    def _supervise(self) -> None:
        """Keep UxPlay running: it exits on some disconnects, so restart it every time."""
        while not self._stopping:
            read_fd, write_fd = os.pipe()
            sink = (f"videorate drop-only=true ! video/x-raw,framerate={self.fps}/1 ! "
                    f"jpegenc quality={self.quality} ! fdsink fd={write_fd}")
            self._proc = subprocess.Popen(
                ["uxplay", "-n", self.name, "-nh", "-a", "-reset", "0", "-vs", sink],
                pass_fds=(write_fd,))  # UxPlay logs to stdout: keep them in ours
            os.close(write_fd)
            log.info("AirPlay receiver '%s' started (pid %d)", self.name, self._proc.pid)
            self._read(read_fd)  # returns when UxPlay closes the pipe
            code = self._proc.wait()
            self.frame = None
            log.warning("AirPlay receiver exited (code %s), restarting", code)
            time.sleep(1)

    @property
    def live(self) -> bool:
        # AirPlay sends no frames while the screen is still, so allow a long quiet gap
        running = self._proc is not None and self._proc.poll() is None
        return running and self.frame is not None and time.time() - self.updated < 90

    @property
    def aspect(self) -> float:
        """Frame width / height, read from the JPEG header (portrait iPhone as a fallback)."""
        f = self.frame or b""
        i = f.find(b"\xff\xc0")  # baseline start-of-frame: ... height(2) width(2)
        if i < 0 or len(f) < i + 9:
            return 9 / 19.5
        return int.from_bytes(f[i + 7:i + 9], "big") / int.from_bytes(f[i + 5:i + 7], "big")

    async def settled(self, quiet: float = 0.5, least: float = 0.4, most: float = 4) -> None:
        """Wait until the screen stops changing (AirPlay only sends frames on change)."""
        started = time.time()
        await asyncio.sleep(least)
        while time.time() - self.updated < quiet and time.time() - started < most:
            await asyncio.sleep(0.1)

    async def fresh(self, since: float, timeout: float = 0.8) -> bytes | None:
        """The first frame received after `since`, or None if none arrives in time."""
        deadline = time.time() + timeout
        while self.updated <= since:
            if time.time() > deadline:
                return None
            await asyncio.sleep(0.02)
        return self.frame

    def _read(self, fd: int) -> None:
        buf = b""
        with os.fdopen(fd, "rb", buffering=0) as pipe:
            while chunk := pipe.read(1 << 16):
                buf += chunk
                while (start := buf.find(b"\xff\xd8")) >= 0 and (end := buf.find(b"\xff\xd9", start)) >= 0:
                    self.frame, self.updated = buf[start:end + 2], time.time()
                    buf = buf[end + 2:]
