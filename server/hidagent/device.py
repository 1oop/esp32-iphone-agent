"""TCP link to the HID bridge board (see firmware/hid_bridge)."""

import asyncio
import logging
import time

log = logging.getLogger(__name__)


class DeviceError(RuntimeError):
    pass


class Device:
    """Accepts one board connection and sends it commands, one at a time."""

    def __init__(self):
        self._reader = self._writer = None
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.links = 0  # bumped on every (re)connect, so gestures know to release and re-home

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def serve(self, host: str, port: int):
        return await asyncio.start_server(self._accept, host, port)

    async def _accept(self, reader, writer):
        if self.connected:
            self._writer.close()
        self._reader, self._writer = reader, writer
        self.links += 1
        log.info("board connected from %s", writer.get_extra_info("peername"))

    async def wait_connected(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while not self.connected and time.time() < deadline:
            await asyncio.sleep(0.2)
        return self.connected

    @staticmethod
    def _timeout(command: str) -> float:
        """Seconds the board may take: Wi-Fi replies need up to ~2 s at p99, moves are paced
        (10 units per 8 ms), typing is per character."""
        name, _, arg = command.partition(" ")
        if name == "move":
            return 5 + max(abs(int(n)) for n in arg.split()) / 10 * 0.012
        if name == "type":
            return 5 + 0.1 * len(arg)
        return 5

    async def send(self, command: str) -> None:
        if not self.connected:
            raise DeviceError("board not connected")
        timeout = self._timeout(command)
        async with self._lock:
            started = time.time()
            try:
                self._writer.write(command.encode() + b"\n")
                await self._writer.drain()
                reply = (await asyncio.wait_for(self._reader.readline(), timeout)).decode().strip()
            except (OSError, asyncio.TimeoutError) as e:
                self._writer.close()  # the board reconnects on its own within a few seconds
                raise DeviceError(f"board link lost during '{command[:30]}': {e!r}") from e
        self._last = time.time()
        log.debug("%s -> %s (%.0f ms)", command, reply, (time.time() - started) * 1000)
        if reply != "ok":
            raise DeviceError(reply or "board disconnected")

    async def keep_awake(self, idle: float = 25) -> None:
        """Nudge the pointer when idle so the phone does not auto-lock."""
        while True:
            await asyncio.sleep(5)
            if self.connected and time.time() - self._last > idle:
                try:
                    await self.send("move 1 0")
                    await self.send("move -1 0")
                except DeviceError as e:
                    log.debug("keep-awake failed: %s", e)
