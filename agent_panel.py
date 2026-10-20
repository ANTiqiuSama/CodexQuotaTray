"""Agent panel: compact quota header plus a field of task bubbles.

Each bubble is one task: a single agent, or several agents the user has
dropped onto each other (or that the client itself spawned) shown as one
larger bubble with orbiting member dots. Drag a bubble onto another to merge,
right-click a merged bubble to split it again.

Scene holds the animation state and is pure Python + PIL so it can be tested
anywhere; hover_details feeds it the pointer and presents its frames.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from detail_card import MARGIN, MUTED_TEXT, SECONDARY_TEXT, TEXT, TRACK, _compose, _wrap, figure_font, ui_font
from agent_tasks import width as text_units
from icon_art import accent, ring_color

WIDTH = 360
PAD = 18
FIELD_TOP = 118
HINT = "拖到一起 = 同一任务　右键 = 拆开"
BASE_RADIUS = 30.0
GAP = 10.0                  # Clearance between neighbouring bubble boxes.
LABEL = 20.0                # Caption height under a bubble.
SUPERSAMPLE = 3
MAX_BUBBLES = 9


@dataclass(frozen=True)
class BubbleModel:
    key: str
    title: str
    doing: str
    letter: str
    live: bool = True
    detail: str = ""
    members: tuple[tuple[str, bool], ...] = ()   # (letter, live) for grouped bubbles
    busy: bool = False                           # an optional summary is being made
    focus: bool = True                           # belongs to the hovered provider
    keys: tuple[str, ...] = ()                   # member agent ids, for continuity across regrouping

    @property
    def radius(self) -> float:
        count = len(self.members)
        return BASE_RADIUS if count < 2 else min(52.0, BASE_RADIUS + 9.0 * math.sqrt(count - 1))

    @property
    def half_width(self) -> float:
        """Half the footprint: the bubble or its caption, whichever is wider."""
        label = min(self.radius * 2 + 40, text_units(self.doing) * 10.5 + 16)
        return max(self.radius, label / 2) + GAP / 2


@dataclass(frozen=True)
class PanelModel:
    provider: str
    letter: str
    title: str
    status: str
    quota: tuple[tuple[str, float | None], ...]
    tps: float | None
    bubbles: tuple[BubbleModel, ...]


def pack(bubbles) -> tuple[dict[str, tuple[float, float]], float]:
    """Centred shelf layout: home position per bubble and the field height."""
    room = WIDTH - 2 * PAD
    rows: list[list] = [[]]
    for bubble in bubbles:
        used = sum(2 * b.half_width for b in rows[-1])
        if rows[-1] and used + 2 * bubble.half_width > room:
            rows.append([])
        rows[-1].append(bubble)
    homes, y = {}, FIELD_TOP + GAP / 2
    for row in rows:
        if not row:
            continue
        height = max(2 * b.radius for b in row) + LABEL + GAP
        x = PAD + (room - sum(2 * b.half_width for b in row)) / 2
        for bubble in row:
            homes[bubble.key] = (x + bubble.half_width, y + height / 2 - LABEL / 2)
            x += 2 * bubble.half_width
        y += height
    return homes, float(max(96.0, y - FIELD_TOP + GAP / 2))


def field_height(bubbles) -> float:
    return pack(bubbles)[1]


def panel_height(model: PanelModel, field: float) -> float:
    hint = 22 if len(model.bubbles) > 1 or any(b.members for b in model.bubbles) else 0
    return FIELD_TOP + field + hint + 12


# ----- motion helpers ------------------------------------------------------------

def ease_out_cubic(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return 1 - (1 - p) ** 3


class Spring:
    """Critically under-damped spring: quick, with a small elegant overshoot."""

    def __init__(self, value: float, stiffness=190.0, damping=15.0):
        self.value, self.target, self.velocity = value, value, 0.0
        self.stiffness, self.damping = stiffness, damping

    def step(self, dt: float) -> float:
        for _ in range(max(1, math.ceil(dt / 0.008))):
            h = dt / max(1, math.ceil(dt / 0.008))
            force = self.stiffness * (self.target - self.value) - self.damping * self.velocity
            self.velocity += force * h
            self.value += self.velocity * h
        return self.value

    @property
    def settled(self) -> bool:
        return abs(self.target - self.value) < 0.002 and abs(self.velocity) < 0.01


@dataclass
class Body:
    model: BubbleModel
    x: float
    y: float
    born: float
    phase: float = field(default_factory=lambda: random.uniform(0, math.tau))
    vx: float = 0.0
    vy: float = 0.0
    scale: Spring = field(default_factory=lambda: Spring(0.0))
    radius: Spring = field(default_factory=lambda: Spring(BASE_RADIUS, 150.0, 13.0))
    hover: Spring = field(default_factory=lambda: Spring(0.0, 260.0, 26.0))
    glow: Spring = field(default_factory=lambda: Spring(0.0, 260.0, 26.0))
    still: Spring = field(default_factory=lambda: Spring(1.0, 120.0, 22.0))   # 0 while dragged: no floating

    def box(self) -> tuple[float, float, float, float]:
        grow = max(0.3, self.scale.value)
        r, half = self.radius.value * grow, self.model.half_width * grow
        return self.x - half, self.y - r - GAP / 2, self.x + half, self.y + r + LABEL + GAP / 2

    @property
    def size(self) -> float:
        return self.radius.value * max(0.0, self.scale.value) * (1 + 0.05 * self.hover.value + 0.1 * self.glow.value)


@dataclass
class Ghost:
    """A bubble leaving the scene: absorbed into another, popped, or faded."""
    model: BubbleModel
    x: float
    y: float
    r: float
    start: float
    kind: str
    tx: float = 0.0
    ty: float = 0.0
    duration: float = 0.3


@dataclass
class Ripple:
    x: float
    y: float
    r: float
    start: float
    letter: str
    duration: float = 0.6
    key: str = ""               # follow this bubble while it settles


class Scene:
    def __init__(self):
        self.model: PanelModel | None = None
        self.bodies: dict[str, Body] = {}
        self.ghosts: list[Ghost] = []
        self.ripples: list[Ripple] = []
        self.field = Spring(field_height(()), 120.0, 20.0)
        self.time = 0.0
        self.last = None
        self.press = None           # (key, dx, dy, x, y)
        self.dragging: str | None = None
        self.target: str | None = None
        self.hovered: str | None = None
        self.left = self.right = False
        self.origins: dict[str, tuple[float, float, float]] = {}
        self.homes: dict[str, tuple[float, float]] = {}

    # ----- geometry --------------------------------------------------------
    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return PAD, FIELD_TOP, WIDTH - PAD, FIELD_TOP + self.field.value

    @property
    def height(self) -> float:
        return panel_height(self.model, self.field.value) if self.model else FIELD_TOP + self.field.value

    def _free_spot(self, radius: float) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bounds
        y1 = y0 + self.field.target
        best, best_score = ((x0 + x1) / 2, (y0 + y1) / 2), -1e9
        for index in range(48):
            angle, distance = index * 2.399963, 9 * math.sqrt(index)   # golden-angle spiral from the centre
            x = (x0 + x1) / 2 + math.cos(angle) * distance * 2.2
            y = (y0 + y1) / 2 + math.sin(angle) * distance
            if not (x0 + radius <= x <= x1 - radius and y0 + radius <= y <= y1 - radius - 14):
                continue
            gaps = [math.hypot(x - b.x, y - b.y) - b.radius.target - radius for b in self.bodies.values()]
            score = min(gaps, default=200) - 0.05 * distance
            if score > best_score:
                best, best_score = (x, y), score
        return best

    def body_at(self, x: float, y: float) -> str | None:
        order = sorted(self.bodies.items(), key=lambda item: item[0] == self.dragging, reverse=True)
        for key, body in order:
            if math.hypot(x - body.x, y - body.y) <= max(14.0, body.size):
                return key
        return None

    # ----- model -----------------------------------------------------------
    def set_model(self, model: PanelModel, now: float):
        first = self.model is None
        self.model = model
        bubbles = model.bubbles[:MAX_BUBBLES]
        self.homes, self.field.target = pack(bubbles)
        if first:
            self.field.value = self.field.target
        keys = {bubble.key for bubble in bubbles}
        removed = {key: self.bodies.pop(key) for key in [key for key in self.bodies if key not in keys]}
        if self.dragging in removed:
            self.dragging = self.target = self.press = None
        for index, bubble in enumerate(bubbles):
            body = self.bodies.get(bubble.key)
            if body is None and bubble.key not in self.origins:
                # The same agents regrouped by the client (e.g. a sub-agent spawned): keep the bubble.
                mine = set(bubble.keys or (bubble.key,))
                heir = next((key for key, old in removed.items() if mine & set(old.model.keys or (key,))), None)
                if heir:
                    body = self.bodies[bubble.key] = removed.pop(heir)
                    body.scale.velocity += 3.0
            if body is None:
                origin = self.origins.pop(bubble.key, None)
                if origin:
                    x, y, angle = origin
                    body = Body(bubble, x, y, now)
                    body.vx, body.vy = math.cos(angle) * 260, math.sin(angle) * 260
                    body.scale.value = 0.35
                else:
                    x, y = self.homes[bubble.key] if first else self._free_spot(bubble.radius)
                    body = Body(bubble, x, y, now + (index * 0.05 if first else 0.0))
                body.radius.value = body.radius.target = bubble.radius
                self.bodies[bubble.key] = body
            body.model = bubble
            body.radius.target = bubble.radius
            body.scale.target = 1.0
        for body in removed.values():
            self.ghosts.append(Ghost(body.model, body.x, body.y, body.size, now, "fade", duration=0.28))

    def absorb(self, source: str, destination: str, merged: str, now: float):
        """Animate `source` flowing into `destination`, which becomes `merged`."""
        moving, staying = self.bodies.pop(source, None), self.bodies.pop(destination, None)
        if staying is None:
            return
        if moving is not None:
            self.ghosts.append(Ghost(moving.model, moving.x, moving.y, moving.size, now, "absorb", staying.x, staying.y, 0.26))
        staying.scale.velocity += 4.5            # a soft "gulp" as it swallows the other
        self.bodies[merged] = staying
        self.ripples.append(Ripple(staying.x, staying.y, staying.size, now, staying.model.letter, 0.6, merged))
        self.dragging = self.target = self.press = None

    def burst(self, key: str, members: list[str], now: float):
        body = self.bodies.pop(key, None)
        if body is None:
            return
        self.ghosts.append(Ghost(body.model, body.x, body.y, body.size, now, "pop", duration=0.32))
        self.ripples.append(Ripple(body.x, body.y, body.size, now, body.model.letter, 0.7))
        offset = random.uniform(0, math.tau)
        for index, member in enumerate(members):
            angle = offset + index * math.tau / max(1, len(members))
            self.origins[member] = (body.x + math.cos(angle) * 6, body.y + math.sin(angle) * 6, angle)

    # ----- simulation ------------------------------------------------------
    def step(self, now: float, pointer: tuple[float, float] | None, left: bool, right: bool) -> list[tuple]:
        dt = 1 / 60 if self.last is None else min(1 / 20, max(0.0, now - self.last))
        self.last, self.time = now, self.time + dt
        events: list[tuple] = []
        under = self.body_at(*pointer) if pointer else None
        if left and not self.left and under:
            body = self.bodies[under]
            self.press = (under, pointer[0] - body.x, pointer[1] - body.y, *pointer)
        if left and self.press and pointer and not self.dragging and self.press[0] in self.bodies:
            if math.hypot(pointer[0] - self.press[3], pointer[1] - self.press[4]) > 4:
                self.dragging = self.press[0]
        if self.dragging and self.dragging in self.bodies and pointer:
            body = self.bodies[self.dragging]
            follow = 1 - math.exp(-dt * 32)
            body.x += (pointer[0] - self.press[1] - body.x) * follow
            body.y += (pointer[1] - self.press[2] - body.y) * follow
            body.vx = body.vy = 0.0
            self.target = None
            best = 1e9
            for key, other in self.bodies.items():
                if key == self.dragging:
                    continue
                distance = math.hypot(other.x - body.x, other.y - body.y)
                if distance < other.size * 0.95 + body.size * 0.4 and distance < best:
                    self.target, best = key, distance
        if not left and self.left:
            if self.dragging and self.target:
                events.append(("merge", self.dragging, self.target))
            self.dragging = self.target = self.press = None
        if right and not self.right and under and len(self.bodies[under].model.members) > 1:
            events.append(("split", under))
        self.left, self.right = left, right
        self.hovered = None if self.dragging else under

        self.field.step(dt)
        x0, y0, x1, y1 = self.bounds
        bodies = list(self.bodies.items())
        for key, body in bodies:
            body.scale.target = 1.0 if now >= body.born else 0.0
            body.scale.step(dt)
            body.radius.step(dt)
            body.hover.target = 1.0 if key == self.hovered else 0.0
            body.glow.target = 1.0 if key == self.target else 0.0
            body.still.target = 0.0 if key == self.dragging else 1.0
            for spring in (body.hover, body.glow, body.still):
                spring.step(dt)
            if key == self.dragging:
                continue
            hx, hy = self.homes.get(key, (body.x, body.y))
            pull = 1 - math.exp(-dt * 4.5)
            body.x += (hx - body.x) * pull + body.vx * dt
            body.y += (hy - body.y) * pull + body.vy * dt
            decay = math.exp(-dt * 7)
            body.vx, body.vy = body.vx * decay, body.vy * decay
        for index, (key, body) in enumerate(bodies):
            for other_key, other in bodies[index + 1:]:
                if self.dragging in (key, other_key):
                    continue        # the dragged bubble floats above the rest
                # Footprints are boxes (bubble + caption) so captions never collide.
                a, b = body.box(), other.box()
                overlap_x = min(a[2], b[2]) - max(a[0], b[0])
                overlap_y = min(a[3], b[3]) - max(a[1], b[1])
                if overlap_x <= 0 or overlap_y <= 0:
                    continue
                rate = min(1.0, dt * 14)
                share = 0.5
                if overlap_x * 0.8 < overlap_y:
                    sign = 1 if other.x > body.x or (other.x == body.x and index % 2) else -1
                    body.x -= sign * overlap_x * rate * share
                    other.x += sign * overlap_x * rate * (1 - share)
                else:
                    sign = 1 if other.y >= body.y else -1
                    body.y -= sign * overlap_y * rate * share
                    other.y += sign * overlap_y * rate * (1 - share)
        for key, body in bodies:
            r = body.radius.value * max(body.scale.value, 0.3)
            half = min(body.model.half_width - GAP / 2, (x1 - x0) / 2)
            body.x = min(max(body.x, x0 + half), x1 - half)
            body.y = min(max(body.y, y0 + r + 6), max(y0 + r + 6, y1 - r - LABEL))
        self.ghosts = [g for g in self.ghosts if now - g.start < g.duration]
        self.ripples = [r for r in self.ripples if now - r.start < r.duration]
        return events

    @property
    def animating(self) -> bool:
        return bool(self.dragging or self.ghosts or self.ripples or not self.field.settled or any(
            not (b.scale.settled and b.radius.settled and b.hover.settled and b.glow.settled) for b in self.bodies.values()))

    def float_offset(self, body: Body) -> tuple[float, float]:
        t, still = self.time, body.still.value
        return (1.6 * math.sin(t * 0.83 + body.phase) * still,
                2.4 * math.sin(t * 1.07 + body.phase * 1.7) * still)

    # ----- drawing ---------------------------------------------------------
    def render(self, scale: float) -> Image.Image:
        model = self.model
        field_h = round(self.field.value / 2) * 2
        frame = background(model.letter, model.title, model.status, model.quota, model.tps,
                           len(model.bubbles) > 1 or any(b.members for b in model.bubbles), field_h, scale,
                           not model.bubbles).copy()
        origin = round(MARGIN * scale)

        def at(value):
            return origin + value * scale

        for ripple in self.ripples:
            p = (self.time_since(ripple.start)) / ripple.duration
            radius = ripple.r * (1 + 0.9 * ease_out_cubic(p))
            body = self.bodies.get(ripple.key)
            cx, cy = (body.x, body.y) if body else (ripple.x, ripple.y)
            blit(frame, faded(ring(round(radius * scale), max(1, round(2 * scale * (1 - p) + 1)), ripple.letter), 0.7 * (1 - p) ** 1.5),
                 at(cx), at(cy))
        for ghost in self.ghosts:
            p = min(1.0, self.time_since(ghost.start) / ghost.duration)
            e = ease_out_cubic(p)
            if ghost.kind == "absorb":
                x, y = ghost.x + (ghost.tx - ghost.x) * e, ghost.y + (ghost.ty - ghost.y) * e
                r, alpha = ghost.r * (1 - 0.7 * e), 1 - p ** 2
            elif ghost.kind == "pop":
                x, y, r, alpha = ghost.x, ghost.y, ghost.r * (1 + 0.35 * e), (1 - e) ** 1.4
            else:
                x, y, r, alpha = ghost.x, ghost.y, ghost.r * (1 - 0.4 * e), 1 - e
            self._bubble(frame, ghost.model, at(x), at(y), r, scale, alpha, label=False)
        order = sorted(self.bodies.items(), key=lambda item: (item[0] == self.dragging, item[0] == self.hovered))
        for key, body in order:
            ox, oy = self.float_offset(body)
            x, y, r = at(body.x + ox), at(body.y + oy), body.size
            if r <= 0.5:
                continue
            m = body.model
            if m.live and key != self.dragging:
                period = 2.4
                p = ((self.time + body.phase) % period) / period
                blit(frame, faded(ring(round(r * (1 + 0.32 * ease_out_cubic(p)) * scale), max(1, round(1.4 * scale)), m.letter),
                                  0.5 * (1 - p) ** 2 * (1 if m.focus else 0.5)), x, y)
            if body.glow.value > 0.02:
                blit(frame, faded(halo(round((r + 4) * scale), m.letter), min(1.0, body.glow.value)), x, y)
            note = "松开合并为同一任务" if key == self.target else ""
            self._bubble(frame, m, x, y, r, scale, min(1.0, max(0.0, body.scale.value)), label=key != self.dragging,
                         expanded=body.hover.value > 0.5, note=note)
            if m.members:
                count = min(6, len(m.members))
                for index, (letter, live) in enumerate(m.members[:count]):
                    angle = self.time * 0.55 + body.phase + index * math.tau / count
                    pulse = 1 + (0.25 * math.sin(self.time * 4 + index) if live else 0)
                    blit(frame, faded(dot(round(3.4 * scale * pulse), letter), 1.0 if live else 0.5),
                         x + math.cos(angle) * (r + 1) * scale, y + math.sin(angle) * (r + 1) * scale)
            if m.busy:
                spinner = arc(round((r + 3) * scale), m.letter)
                blit(frame, spinner.rotate(-self.time * 300 % 360, resample=Image.BICUBIC), x, y)
        return frame

    def time_since(self, moment: float) -> float:
        return max(0.0, (self.last or moment) - moment)

    def _bubble(self, frame, m: BubbleModel, x, y, r, scale, alpha, label=True, expanded=False, note=""):
        base = bubble(m.title, m.letter, m.live, m.focus, round(m.radius * scale), scale)
        factor = r / m.radius
        if abs(factor - 1) > 0.01:
            size = max(2, round(base.width * factor))
            base = base.resize((size, size), Image.BILINEAR)
        blit(frame, faded(base, alpha), x, y)
        if label and alpha > 0.3:
            text = note or (m.detail if expanded and m.detail and m.detail != m.title else m.doing)
            tag = caption(text, m.letter if m.live or note else "", round(r * 2 + 40), expanded or bool(note), scale)
            blit(frame, faded(tag, alpha * (1 if m.focus or expanded else 0.75)), x, y + (r + 4) * scale + tag.height / 2)


# ----- sprites -----------------------------------------------------------------

def blit(frame: Image.Image, sprite: Image.Image, cx: float, cy: float):
    """Alpha-composite `sprite` centred at (cx, cy), clipped to the frame."""
    left, top = round(cx - sprite.width / 2), round(cy - sprite.height / 2)
    crop = (max(0, -left), max(0, -top), min(sprite.width, frame.width - left), min(sprite.height, frame.height - top))
    if crop[2] <= crop[0] or crop[3] <= crop[1]:
        return
    piece = sprite if crop == (0, 0, sprite.width, sprite.height) else sprite.crop(crop)
    frame.alpha_composite(piece, (left + crop[0], top + crop[1]))


@lru_cache(maxsize=64)
def _alpha_table(level: int):
    return [value * level // 255 for value in range(256)]


def faded(sprite: Image.Image, alpha: float) -> Image.Image:
    level = round(255 * min(1.0, max(0.0, alpha)))
    if level >= 255:
        return sprite
    out = sprite.copy()
    out.putalpha(sprite.getchannel("A").point(_alpha_table(level)))
    return out


def _with_alpha(color, alpha: int):
    return tuple(color[:3]) + (max(0, min(255, round(alpha))),)


@lru_cache(maxsize=1)
def _radial():
    return Image.radial_gradient("L")      # 0 at the centre, 181 at the inscribed circle


@lru_cache(maxsize=128)
def bubble(title: str, letter: str, live: bool, focus: bool, radius: int, scale: float) -> Image.Image:
    """Glass bubble: tinted rim-heavy fill, hairline rim, specular highlight, title."""
    pad = max(4, round(9 * scale))
    size = 2 * (radius + pad)
    color = accent(letter)
    strength = (1.0 if live else 0.62) * (1.0 if focus else 0.7)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse((pad, pad + round(3 * scale), size - pad, size - pad + round(3 * scale)), fill=(0, 0, 0, round(95 * strength)))
    image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(max(1, round(4 * scale)))))

    big = 2 * radius * SUPERSAMPLE
    lut = [round(255 * min(1.0, (0.16 + 0.66 * (v / 181) ** 3.0) * strength)) if v <= 181 else 0 for v in range(256)]
    fill_alpha = _radial().resize((big, big), Image.BILINEAR).point(lut)
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    body = Image.new("RGBA", (big, big), tuple(color[:3]) + (0,))
    body.putalpha(ImageChops.multiply(fill_alpha, mask))
    draw = ImageDraw.Draw(body)
    rim = max(2, round(1.3 * scale * SUPERSAMPLE))
    draw.ellipse((rim // 2, rim // 2, big - 1 - rim // 2, big - 1 - rim // 2), outline=_with_alpha(color, 215 * strength), width=rim)
    gloss = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(gloss).ellipse((big * 0.2, big * 0.12, big * 0.56, big * 0.34), fill=(255, 255, 255, round(70 * strength)))
    body.alpha_composite(gloss.filter(ImageFilter.GaussianBlur(big * 0.035)))
    image.alpha_composite(body.resize((2 * radius, 2 * radius), Image.LANCZOS), (pad, pad))

    weight = "bold"
    face = ui_font(max(6, round((12.5 if radius > 34 * scale else 11.5) * scale)), weight)
    draw = ImageDraw.Draw(image)
    lines = _wrap(draw, title, face, radius * 1.52)
    if len(lines) > 2:
        lines = lines[:2]
        while lines[1] and draw.textlength(lines[1] + "…", font=face) > radius * 1.45:
            lines[1] = lines[1][:-1]
        lines[1] += "…"
    top, bottom = face.getbbox("国Ag")[1::2]
    step = (bottom - top) * 1.18
    start = size / 2 - step * (len(lines) - 1) / 2
    ink = _with_alpha(TEXT, 255 * (1.0 if live else 0.78) * (1.0 if focus else 0.85))
    for index, line in enumerate(lines):
        y = start + index * step
        draw.text((size / 2, y + max(1, round(scale))), line, font=face, fill=(0, 0, 0, 110), anchor="mm")
        draw.text((size / 2, y), line, font=face, fill=ink, anchor="mm")
    return image


@lru_cache(maxsize=128)
def caption(text: str, letter: str, max_width: int, expanded: bool, scale: float) -> Image.Image:
    face = ui_font(max(6, round(10.5 * scale)))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    limit = max_width * scale if not expanded else 200 * scale
    lines = _wrap(probe, text, face, limit)
    keep = 2 if expanded else 1
    if len(lines) > keep:
        lines = lines[:keep]
        while lines[-1] and probe.textlength(lines[-1] + "…", font=face) > limit:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    top, bottom = face.getbbox("国Ag")[1::2]
    step = round((bottom - top) * 1.35)
    width = max(2, round(max(probe.textlength(line, font=face) for line in lines)) + 4 + (round(10 * scale) if letter else 0))
    image = Image.new("RGBA", (width, step * len(lines) + 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    x = 2
    if letter:
        d = round(5 * scale)
        draw.ellipse((x, step / 2 - d / 2, x + d, step / 2 + d / 2), fill=accent(letter))
        x += round(10 * scale)
    for index, line in enumerate(lines):
        draw.text((x, step * index + step / 2), line, font=face, fill=TEXT if expanded else SECONDARY_TEXT, anchor="lm")
    return image


@lru_cache(maxsize=256)
def ring(radius: int, stroke: int, letter: str) -> Image.Image:
    big = (radius + stroke + 1) * 2 * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    inset = SUPERSAMPLE * 1
    ImageDraw.Draw(image).ellipse((inset, inset, big - inset - 1, big - inset - 1), outline=accent(letter), width=stroke * SUPERSAMPLE)
    return image.resize((big // SUPERSAMPLE, big // SUPERSAMPLE), Image.LANCZOS)


@lru_cache(maxsize=64)
def halo(radius: int, letter: str) -> Image.Image:
    pad = max(4, radius // 3)
    size = 2 * (radius + pad)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((pad, pad, size - pad, size - pad), outline=_with_alpha(accent(letter), 255), width=max(3, radius // 6))
    glow = image.filter(ImageFilter.GaussianBlur(max(2, radius // 7)))
    glow.alpha_composite(glow)
    glow.alpha_composite(ring(radius, max(1, radius // 24), letter), ((size - ring(radius, max(1, radius // 24), letter).width) // 2,) * 2)
    return glow


@lru_cache(maxsize=64)
def dot(radius: int, letter: str) -> Image.Image:
    big = (radius + 2) * 2 * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    edge = 2 * SUPERSAMPLE
    draw.ellipse((edge - SUPERSAMPLE, edge - SUPERSAMPLE, big - edge + SUPERSAMPLE, big - edge + SUPERSAMPLE), fill=(36, 35, 32, 255))
    draw.ellipse((edge, edge, big - edge, big - edge), fill=accent(letter))
    return image.resize((big // SUPERSAMPLE, big // SUPERSAMPLE), Image.LANCZOS)


@lru_cache(maxsize=32)
def arc(radius: int, letter: str) -> Image.Image:
    big = (radius + 2) * 2 * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    stroke = max(2, round(radius / 14)) * SUPERSAMPLE
    color = accent(letter)
    for step in range(24):                 # tapering tail
        start = -90 + step * 4
        draw.arc((stroke, stroke, big - stroke, big - stroke), start, start + 5, fill=_with_alpha(color, 30 + step * 9), width=stroke)
    return image.resize((big // SUPERSAMPLE, big // SUPERSAMPLE), Image.LANCZOS)


@lru_cache(maxsize=24)
def background(letter: str, title: str, status: str, quota: tuple, tps: float | None, hint: bool, field_h: int, scale: float,
               empty: bool = False) -> Image.Image:
    height = FIELD_TOP + field_h + (22 if hint else 0) + 12
    s = scale
    px = lambda value: round(value * s)  # noqa: E731
    content = Image.new("RGBA", (px(WIDTH), px(height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(content)
    color = accent(letter)
    right = WIDTH - PAD
    y = PAD
    draw.ellipse((px(PAD), px(y + 5), px(PAD + 8), px(y + 13)), fill=color)
    draw.text((px(PAD + 16), px(y + 9)), title, font=ui_font(px(14), "bold"), fill=TEXT, anchor="lm")
    pill = ui_font(px(11))
    width = draw.textlength(status, font=pill) / s + 18
    draw.rounded_rectangle((px(right - width), px(y), px(right), px(y + 18)), radius=px(9), fill=color)
    draw.text((px(right - width / 2), px(y + 9)), status, font=pill, fill=(31, 30, 28, 255), anchor="mm")

    y += 32
    columns = list(quota[:2]) + [("TPS", None)]
    column = (WIDTH - 2 * PAD) / len(columns)
    label_font, value_font, unit_font = ui_font(px(11)), figure_font(px(21)), ui_font(px(11), "bold")
    for index, (label, value) in enumerate(columns):
        x = PAD + index * column
        draw.text((px(x), px(y)), label, font=label_font, fill=MUTED_TEXT, anchor="lt")
        if label == "TPS":
            figure, unit, tone = ("—", "", TEXT) if tps is None else (f"{tps:.0f}", "tok/s", color)
        else:
            figure, unit, tone = ("—", "", TEXT) if value is None else (f"{value:.0f}", "%", TEXT if value >= 50 else ring_color(value, letter))
        draw.text((px(x), px(y + 38)), figure, font=value_font, fill=tone, anchor="ls")
        if unit:
            draw.text((px(x) + draw.textlength(figure, font=value_font) + px(3), px(y + 37)), unit, font=unit_font, fill=SECONDARY_TEXT if label == "TPS" else tone, anchor="ls")
        if label != "TPS":
            end = x + column - 16
            draw.rounded_rectangle((px(x), px(y + 45), px(end), px(y + 49)), radius=px(2), fill=TRACK)
            if value:
                draw.rounded_rectangle((px(x), px(y + 45), px(x + max(4, (end - x) * min(100.0, value) / 100)), px(y + 49)),
                                       radius=px(2), fill=ring_color(value, letter))
    draw.rounded_rectangle((px(PAD - 6), px(FIELD_TOP - 6), px(right + 6), px(FIELD_TOP + field_h + 4)), radius=px(12),
                           fill=(240, 238, 230, 7), outline=(240, 238, 230, 14), width=max(1, round(s)))
    if empty:
        draw.text((px(WIDTH / 2), px(FIELD_TOP + field_h / 2)), "暂无工作中的 agent", font=ui_font(px(11)), fill=MUTED_TEXT, anchor="mm")
    if hint:
        draw.text((px(WIDTH / 2), px(FIELD_TOP + field_h + 16)), HINT, font=ui_font(px(10)), fill=_with_alpha(MUTED_TEXT, 190), anchor="mm")
    return _compose(content, scale)
