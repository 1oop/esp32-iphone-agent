"""Touch-like gestures on the phone, built from relative mouse moves.

The pointer is relative, so the server tracks where it is (in mouse units) and moves
from there; every few gestures it slams it into the top-left corner (`home`) again to
cancel drift. Coordinates are fractions of the screen (0..1, origin top-left), so they
work for any frame size.
"""

import json
import logging
import time
from pathlib import Path

from . import vision
from .device import Device

log = logging.getLogger(__name__)

CALIBRATION_MOVE = 400  # mouse units moved diagonally during calibration
REHOME_EVERY = 10       # gestures between re-homing
NUDGE = 12              # mouse units the pointer is wiggled by to find it on screen
AIM_TOLERANCE = 0.008   # screen fraction; a bigger miss is corrected before pressing


class Phone:
    def __init__(self, device: Device, calibration_file: Path, screen=None):
        self.device, self._file, self.screen = device, calibration_file, screen
        # mouse units needed to cross the full screen width / height
        self.span = json.loads(calibration_file.read_text()) if calibration_file.exists() else None
        self._pos = None  # pointer position in mouse units; None = unknown
        self._moves = 0
        self._link = 0  # device.links when the pointer was last homed

    @property
    def calibrated(self) -> bool:
        return self.span is not None

    async def start_calibration(self) -> None:
        self._pos = None
        await self.device.send("home")
        await self.device.send(f"move {CALIBRATION_MOVE} {CALIBRATION_MOVE}")

    def finish_calibration(self, x: float, y: float) -> None:
        """x, y: where the pointer ended up after start_calibration()."""
        self.span = {"w": round(CALIBRATION_MOVE / x), "h": round(CALIBRATION_MOVE / y)}
        self._file.write_text(json.dumps(self.span))

    async def point(self, x: float, y: float, aim: bool = True) -> None:
        if not self.calibrated:
            raise RuntimeError("pointer not calibrated")
        await self._move_to(self._dx(x), self._dy(y))
        if aim and self.screen is not None:
            await self._aim(x, y)

    async def _aim(self, x: float, y: float) -> None:
        """Closed loop: find the real pointer on screen, fix a miss, and refine the calibration."""
        nudge = NUDGE if x < 0.9 else -NUDGE  # stay clear of the screen edge
        await self.screen.settled(quiet=0.15, least=0.1, most=0.6)  # pointer at rest
        before, sent = self.screen.frame, time.time()
        await self.device.send(f"move {nudge} 0")
        after = await self.screen.fresh(sent, 0.6)
        if after:
            await self.screen.settled(quiet=0.1, least=0.05, most=0.4)
            after = self.screen.frame
        await self.device.send(f"move {-nudge} 0")
        found = before and after and vision.pointer(before, after, (x, y), nudge / self.span["w"])
        if not found:
            log.debug("aim (%.3f, %.3f): pointer not found", x, y)
            return
        fx, fy = found
        log.debug("aim (%.3f, %.3f): pointer at (%.3f, %.3f)", x, y, fx, fy)
        if abs(fx - x) <= AIM_TOLERANCE and abs(fy - y) <= AIM_TOLERANCE:
            return
        # moves are measured from the top-left corner, so a miss is mostly a wrong span
        for axis, want, got in (("w", x, fx), ("h", y, fy)):
            if want > 0.2 and 0.8 < got / want < 1.25:
                self.span[axis] = round(self.span[axis] * (1 + 0.5 * (want / got - 1)))
        self._file.write_text(json.dumps(self.span))
        dx, dy = round((x - fx) * self.span["w"]), round((y - fy) * self.span["h"])
        await self.device.send(f"move {dx} {dy}")
        self._pos = (self._pos[0] + dx, self._pos[1] + dy)

    async def _move_to(self, tx: int, ty: int, rehome: bool = True) -> None:
        if self._link != self.device.links:  # new link: a dropped gesture may have left the button down
            await self.device.send("release")
            self._pos, self._link = None, self.device.links
        if self._pos is None or (rehome and self._moves >= REHOME_EVERY):
            await self.device.send("home")
            self._pos, self._moves = (0, 0), 0
        x, y = self._pos
        self._pos = None  # unknown until the move succeeds
        await self.device.send(f"move {tx - x} {ty - y}")
        self._pos, self._moves = (tx, ty), self._moves + 1

    async def reset(self) -> None:
        """Let go of the button and re-home the pointer, whatever state a gesture left."""
        self._pos = None
        await self.device.send("release")
        await self.device.send("home")
        self._pos, self._moves = (0, 0), 0

    async def tap(self, x: float, y: float) -> None:
        await self.point(x, y)
        await self.device.send("click")

    async def swipe(self, x1: float, y1: float, x2: float, y2: float) -> None:
        await self.drag([(x1, y1), (x2, y2)])

    async def drag(self, points: list[tuple[float, float]]) -> None:
        """Press at the first point, move through the rest, release: one continuous stroke."""
        await self.point(*points[0])
        await self.device.send("press")
        try:
            for x, y in points[1:]:
                await self._move_to(self._dx(x), self._dy(y), rehome=False)  # never home mid-drag
        finally:
            await self.device.send("release")

    async def scroll(self, x: float, y: float, lines: int) -> None:
        await self.point(x, y)
        await self.device.send(f"scroll {lines} 0")

    async def type(self, text: str) -> None:
        await self.device.send(f"type {text}")

    async def key(self, combo: str) -> None:
        await self.device.send(f"key {combo}")

    async def go_home(self) -> None:
        await self.swipe(0.5, 0.995, 0.5, 0.6)

    async def go_back(self) -> None:
        await self.swipe(0.005, 0.45, 0.6, 0.45)

    def _dx(self, x: float) -> int:
        return round(min(max(x, 0), 1) * self.span["w"])

    def _dy(self, y: float) -> int:
        return round(min(max(y, 0), 1) * self.span["h"])
