"""See-think-act loop: a planner model writes the plan, an actor model picks each step."""

import asyncio
import json
import logging
import math
import re
import time

from . import vision
from .debug import Trace
from .device import DeviceError
from .llm import LLM
from .phone import Phone
from .screen import Screen

log = logging.getLogger(__name__)

PLANNER_PROMPT = """You plan tasks on an iPhone. Given the goal and the current screen,
write a short numbered plan (at most 6 steps) of what to do on screen. Plain text only, in English.
Plan only what the goal needs, from what you can see; never invent accounts, emails, logins or
screens you have not seen. Later steps can stay general (e.g. "Draw the face on the canvas").
To open an app, prefer search over paging through home screens: tap the "Search" pill above
the dock (or swipe down from the middle of the home screen), type the app name, tap the result."""

ACTOR_PROMPT = """You operate an iPhone through a mouse pointer. You see the current screen.
Reply in this format, nothing else:
THINK: <plain English a non-technical user understands: first check your last action against the
       screen (did it do what you intended?), then what you do now and why>
DO: <action>

Usually give ONE DO line. When the next few actions do not depend on seeing the result
(e.g. several drawing strokes, or typing then pressing enter), give up to 6 DO lines; they run in order.

Coordinates are integers 0-1000 relative to the screenshot (x left->right, y top->bottom).
Aim at the visual center of the element you want, not its edge or its label.
Look before you act: describe what is really on the screen now, not what you expect.
Each recent action says whether the screen changed after it. If it did NOT change, the action
missed or had no effect: do not assume it worked; aim again more precisely or try another way.
Only type when a text field is visibly focused (the keyboard is showing).
Only tap things you can actually see on this screenshot; never guess where a button might be.
If you are unsure what an element does, tap it alone (one DO line) and check the result next round.
Actions:
  tap X Y
  swipe X1 Y1 X2 Y2         (finger moves from X1,Y1 to X2,Y2: "swipe down" means Y2 > Y1)
  path X1 Y1 X2 Y2 X3 Y3 …  (one continuous stroke through 2-20 points, for drawing)
  circle X Y R              (draw a circle centered at X Y; R is the radius in x units)
  type TEXT                 (ASCII only; tap the input field first)
  key COMBO                 (enter, backspace, esc, cmd+a, ...)
  home | back | wait
  replan                    (the plan no longer fits the screen)
  done ANSWER               (the goal is reached; ANSWER is the result for the user)
  fail REASON

Example:
THINK: My last tap opened the home screen as intended. The App Store icon is visible, so I tap it.
DO: tap 152 362"""

META = ("t", "thought", "thought_zh", "think_s", "act_s", "shot", "_shot", "changed")  # not part of the action

# action name -> argument names; numeric arguments are 0-1000 screen coordinates
ACTIONS = {"tap": ("x", "y"), "swipe": ("x1", "y1", "x2", "y2"), "circle": ("x", "y", "r"), "path": ("points",),
           "type": ("text",), "key": ("combo",), "done": ("answer",), "fail": ("reason",),
           "home": (), "back": (), "wait": (), "replan": ()}
TEXT_ARGS = ("text", "combo", "answer", "reason")


def parse_command(line: str) -> dict:
    """One DO line, e.g. "tap 152 362" -> {"action": "tap", "x": 152, "y": 362}."""
    name, _, rest = line.strip().partition(" ")
    name = name.lower()
    if name not in ACTIONS:
        raise ValueError(f"unknown action: {line[:100]}")
    keys = ACTIONS[name]
    if keys and keys[0] in TEXT_ARGS:
        return {"action": name, keys[0]: rest.strip().strip('"')}
    numbers = [int(float(n)) for n in re.findall(r"-?\d+(?:\.\d+)?", rest)]
    if keys == ("points",):
        if len(numbers) < 4 or len(numbers) % 2:
            raise ValueError(f"path needs 2+ x y pairs: {line[:100]}")
        return {"action": name, "points": [numbers[i:i + 2] for i in range(0, len(numbers), 2)][:20]}
    if len(numbers) != len(keys):
        raise ValueError(f"{name} needs {len(keys)} numbers: {line[:100]}")
    return {"action": name, **dict(zip(keys, numbers, strict=True))}


def parse_actions(text: str) -> list[dict]:
    """Parse a THINK / DO… reply (an optional ZH line is kept); the thought goes on the first action."""
    thought = re.search(r"^\s*THINK\s*[:：]\s*(.*?)\s*$", text, re.M | re.I)
    zh = re.search(r"^\s*ZH\s*[:：]\s*(.*?)\s*$", text, re.M | re.I)
    acts = [parse_command(line) for line in re.findall(r"^\s*DO\s*[:：]\s*(.*?)\s*$", text, re.M | re.I)][:6]
    if not acts:
        raise ValueError(f"no DO line in reply: {text[:200]}")
    acts[0] = {"thought": thought.group(1) if thought else "", "thought_zh": zh.group(1) if zh else "", **acts[0]}
    return acts


def parse_plan(text: str) -> tuple[str, str]:
    """Split an optional EN: / ZH: plan reply; a plain reply is used for both."""
    m = re.search(r"EN\s*[:：](.*?)ZH\s*[:：](.*)", text, re.S | re.I)
    return (m.group(1).strip(), m.group(2).strip()) if m else (text.strip(), text.strip())


class BlindError(RuntimeError):
    """The screen feed is gone; acting now would be guessing."""


class Agent:
    def __init__(self, phone: Phone, screen: Screen, actor: LLM, planner: LLM, trace: Trace,
                 max_steps: int = 40, max_failures: int = 3):
        self.phone, self.screen, self.actor, self.planner = phone, screen, actor, planner
        self.trace = trace
        self.max_steps, self.max_failures = max_steps, max_failures
        self.task, self.plan, self.steps = "", "", []
        self.shots: dict[int, bytes] = {}  # step index -> screenshot the model decided on
        self._job: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._job is not None and not self._job.done()

    def start(self, task: str) -> None:
        if self.running:
            raise RuntimeError("agent already running")
        if not self.screen.live:
            raise RuntimeError("no phone screen: start Screen Mirroring first")
        self.task, self.plan, self.steps, self.shots = task, "", [], {}
        self.trace.start(task)
        self._job = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self.running:
            self._job.cancel()

    def clear(self) -> None:
        if self.running:
            raise RuntimeError("stop the agent first")
        self.task, self.plan, self.steps, self.shots = "", "", [], {}

    def _record(self, shot: bytes | None = None, **step) -> None:
        if shot:
            self.shots[len(self.steps)] = shot
        self.steps.append({"t": time.time(), "shot": bool(shot), **step})
        self.trace.write(kind="step", **step)
        log.info("agent: %s", step)

    async def _ask(self, role: str, llm: LLM, system: str, prompt: str,
                   temperature: float = 0) -> tuple[str, float, bytes | None]:
        if not self.screen.live:  # never let the model act blind
            raise BlindError("lost the phone screen: reconnect Screen Mirroring")
        frame, started = self.screen.frame, time.time()
        reply = await llm.chat(system, prompt, frame, temperature=temperature)
        seconds = round(time.time() - started, 2)
        self.trace.write(frame, kind="llm", role=role, model=llm.model, prompt=prompt,
                         reply=reply, seconds=seconds)
        return reply, seconds, frame

    async def _run(self) -> None:
        failures = 0
        try:
            await self._replan()
            for _ in range(self.max_steps):
                try:
                    if await self._step():
                        return
                    failures = 0
                except BlindError:
                    raise
                except Exception as e:  # a bad reply or a dropped link: recover, don't crash
                    self._record(action="hiccup", reason=str(e)[:300])
                    board_drop = isinstance(e, DeviceError) and await self.phone.device.wait_connected(15)
                    failures += 0 if board_drop else 1  # the board came back: not the agent's fault
                    if failures >= self.max_failures:
                        self._record(action="fail", reason=f"{failures} errors in a row")
                        return
                    await self.screen.settled()
                    await self._replan(stuck=True)
            self._record(action="fail", reason="step limit reached")
        except asyncio.CancelledError:
            self._record(action="stopped")
        except Exception as e:  # e.g. the planner itself is unreachable
            self._record(action="error", reason=repr(e))

    async def _step(self) -> bool:
        """One see-think-act round (one or more actions). Returns True when the task is over."""
        acts = await self._next_actions()
        seen = acts[0]["_shot"]
        stuck = self._repeating(acts[0])
        for i, act in enumerate(acts):
            name = act["action"]
            if name == "replan" or stuck:
                self._record(act.pop("_shot"), **act)
                await self._replan(stuck=stuck)
                return False
            if name not in ("done", "fail"):
                started = time.time()
                await self._do(act)
                act["act_s"] = round(time.time() - started, 2)
            self._record(act.pop("_shot"), **act)
            if name in ("done", "fail"):
                return True
            if name == "tap" and i < len(acts) - 1:  # verify before building on a tap that may have missed
                await self.screen.settled()
                if not vision.changed(seen, self.screen.frame):
                    self.steps[-1]["changed"] = False
                    return False  # skip the rest; the actor looks again
        await self.screen.settled()
        self.steps[-1]["changed"] = vision.changed(seen, self.screen.frame)  # feedback for the next round
        return False

    def _repeating(self, act: dict, limit: int = 3) -> bool:
        """True when the actor picks the same action as its last limit-1 moves."""
        key = lambda s: {k: v for k, v in s.items() if k not in META}  # noqa: E731
        moves = [s for s in self.steps if "plan" not in s][-(limit - 1):]
        return len(moves) == limit - 1 and all(key(m) == key(act) for m in moves)

    async def _replan(self, stuck: bool = False) -> None:
        prompt = f"Goal: {self.task}"
        if stuck:
            last = self.steps[-1] if self.steps else {}
            prompt += (f"\n\nThe previous plan got stuck (last event: {last.get('action')} {last.get('reason', '')}):"
                       f"\n{self.plan}\nLook at the current screen and plan from here, another way if needed.")
        reply, seconds, frame = await self._ask("planner", self.planner, PLANNER_PROMPT, prompt)
        self.plan, plan_zh = parse_plan(reply)
        self._record(frame, action="replan (stuck)" if stuck else "plan", plan=self.plan, plan_zh=plan_zh,
                     think_s=seconds)

    async def _next_actions(self) -> list[dict]:
        hidden = ("t", "plan", "think_s", "act_s", "shot", "thought_zh", "changed")
        effect = {True: " -> screen changed", False: " -> screen did NOT change"}
        history = "\n".join(json.dumps({k: v for k, v in s.items() if k not in hidden})[:300]
                            + effect.get(s.get("changed"), "")
                            for s in self.steps[-8:] if "plan" not in s) or "(none)"
        prompt = f"Goal: {self.task}\n\nPlan:\n{self.plan}\n\nRecent actions:\n{history}\n\nNext action?"
        for attempt in range(3):  # retry unparseable replies, a little less deterministically
            reply, seconds, frame = await self._ask("actor", self.actor, ACTOR_PROMPT, prompt, 0.4 * attempt)
            try:
                acts = parse_actions(reply)
            except ValueError as e:
                log.warning("unparseable actor reply (%s): %s", e, reply[:200])
                continue
            acts[0]["think_s"] = seconds
            return [{**a, "_shot": frame} for a in acts]  # every action is drawn on the same screenshot
        raise ValueError(f"actor gave no valid action: {reply[:200]}")

    async def _do(self, a: dict) -> None:
        p = lambda k: a[k] / 1000  # noqa: E731
        match a["action"]:
            case "tap": await self.phone.tap(p("x"), p("y"))
            case "swipe": await self.phone.swipe(p("x1"), p("y1"), p("x2"), p("y2"))
            case "path": await self.phone.drag([(x / 1000, y / 1000) for x, y in a["points"]])
            case "circle": await self.phone.drag(circle(p("x"), p("y"), p("r"), self.screen.aspect))
            case "type": await self.phone.type(a["text"])
            case "key": await self.phone.key(a["combo"])
            case "home": await self.phone.go_home()
            case "back": await self.phone.go_back()
            case "wait": pass
            case other: raise ValueError(f"unknown action {other!r}")


def circle(x: float, y: float, r: float, aspect: float, n: int = 24) -> list[tuple[float, float]]:
    """Points around a circle; r is a fraction of the width, aspect = width / height."""
    return [(x + r * math.cos(2 * math.pi * i / n), y + r * aspect * math.sin(2 * math.pi * i / n))
            for i in range(n + 1)]
