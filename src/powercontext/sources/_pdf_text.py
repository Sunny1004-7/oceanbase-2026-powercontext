"""Extract positioned text with the physical page and PDF graphics clips applied.

pypdf supplies stream parsing, font encodings and character widths. Cursor motion
is kept separate from rendered text: a final TJ adjustment does not draw a space.
All state and font caches are local to this extraction; the input PDF is untouched.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from itertools import combinations
from typing import Any, cast

from pypdf import PageObject
from pypdf._codecs.adobe_glyphs import adobe_glyphs
from pypdf._font import Font
from pypdf.generic import ArrayObject, ContentStream, DictionaryObject

Point = tuple[float, float]
Matrix = tuple[float, ...]
Polygon = tuple[Point, ...]
_IDENTITY: Matrix = (1, 0, 0, 1, 0, 0)


def _glyph_name(value: str) -> str:
    """Adobe glyph naming: stylistic suffixes do not change the base character."""
    if not value.startswith("/"):
        return value
    name = value.split(".", 1)[0]
    if name in adobe_glyphs:
        return adobe_glyphs[name]
    return value


def _glyphs(value: bytes | str, font: Font) -> Iterable[tuple[str, str]]:
    # Widths are keyed by original character codes, not by decoded Unicode.
    if isinstance(value, bytes) and isinstance(font.encoding, dict):
        for byte in value:
            decoded = font.encoding.get(byte, chr(byte))
            yield chr(byte), font.character_map[decoded] if decoded in font.character_map else _glyph_name(decoded)
    else:
        decoded = (
            value.decode(font.encoding, "surrogatepass")
            if isinstance(value, bytes) and isinstance(font.encoding, str)
            else str(value)
        )
        for code in decoded:
            yield code, font.character_map.get(code, code)


def _multiply(a: Matrix, b: Matrix) -> Matrix:
    return (
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    )


def _point(x: float, y: float, matrix: Matrix) -> Point:
    return (x * matrix[0] + y * matrix[2] + matrix[4], x * matrix[1] + y * matrix[3] + matrix[5])


def _rectangle(box: Iterable[float], matrix: Matrix = _IDENTITY) -> Polygon:
    x0, y0, x1, y1 = map(float, box)
    return tuple(_point(x, y, matrix) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


def _bounds(points: Polygon) -> tuple[float, float, float, float]:
    return (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))


def _edges(points: Polygon) -> Iterable[tuple[Point, Point]]:
    return zip(points, (*points[1:], points[0]), strict=True)


def _cross(a: Point, b: Point) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _difference(a: Point, b: Point) -> Point:
    return (a[0] - b[0], a[1] - b[1])


def _intersection(a: Point, b: Point, c: Point, d: Point) -> Point | None:
    ab, cd, ca = _difference(b, a), _difference(d, c), _difference(c, a)
    denominator = _cross(ab, cd)
    if abs(denominator) < 1e-10:
        return None
    t, u = _cross(ca, cd) / denominator, _cross(ca, ab) / denominator
    if -1e-8 <= t <= 1 + 1e-8 and -1e-8 <= u <= 1 + 1e-8:
        return (a[0] + t * ab[0], a[1] + t * ab[1])
    return None


def _winding(point: Point, polygon: Polygon) -> tuple[int, bool]:
    winding = 0
    for a, b in _edges(polygon):
        side = _cross(_difference(b, a), _difference(point, a))
        if (
            abs(side) < 1e-7
            and min(a[0], b[0]) - 1e-7 <= point[0] <= max(a[0], b[0]) + 1e-7
            and min(a[1], b[1]) - 1e-7 <= point[1] <= max(a[1], b[1]) + 1e-7
        ):
            return 0, True
        if a[1] <= point[1] < b[1] and side > 0:
            winding += 1
        elif b[1] <= point[1] < a[1] and side < 0:
            winding -= 1
    return winding, False


@dataclass(frozen=True)
class _Clip:
    paths: tuple[Polygon, ...]
    even_odd: bool = False
    bounds: tuple[float, float, float, float] = field(init=False)
    rectangle: bool = field(init=False)

    def __post_init__(self) -> None:
        points = tuple(p for path in self.paths for p in path)
        bounds = _bounds(points) if points else (0, 0, 0, 0)
        object.__setattr__(self, "bounds", bounds)
        corners = {(x, y) for x in (bounds[0], bounds[2]) for y in (bounds[1], bounds[3])}
        object.__setattr__(self, "rectangle", len(self.paths) == 1 and set(points) == corners)

    def contains(self, point: Point) -> bool:
        x0, y0, x1, y1 = self.bounds
        if not (x0 - 1e-7 <= point[0] <= x1 + 1e-7 and y0 - 1e-7 <= point[1] <= y1 + 1e-7):
            return False
        if self.rectangle:
            return True
        values = [_winding(point, path) for path in self.paths]
        return any(boundary for _, boundary in values) or (
            sum(w for w, _ in values) % 2 != 0 if self.even_odd else sum(w for w, _ in values) != 0
        )


def _visible(glyph: Polygon, clips: tuple[_Clip, ...]) -> bool:
    bounds = _bounds(glyph)
    if any(
        bounds[2] < clip.bounds[0]
        or bounds[0] > clip.bounds[2]
        or bounds[3] < clip.bounds[1]
        or bounds[1] > clip.bounds[3]
        or not clip.paths
        for clip in clips
    ):
        return False
    if all(
        clip.rectangle and clip.contains((bounds[0], bounds[1])) and clip.contains((bounds[2], bounds[3]))
        for clip in clips
    ):
        return True
    # Intersections of polygon regions have a vertex from one region or from a
    # pair of edges. This also handles holes and multiple clipping subpaths.
    candidates = list(glyph)
    boundaries = [tuple(edge for path in clip.paths for edge in _edges(path)) for clip in clips]
    for clip, edges in zip(clips, boundaries, strict=True):
        candidates.extend(p for path in clip.paths for p in path)
        candidates.extend(p for a, b in _edges(glyph) for c, d in edges if (p := _intersection(a, b, c, d)) is not None)
    for first, second in combinations(boundaries, 2):
        candidates.extend(p for a, b in first for c, d in second if (p := _intersection(a, b, c, d)) is not None)
    return any(
        (inside or boundary) and all(clip.contains(p) for clip in clips)
        for p in candidates
        for inside, boundary in [_winding(p, glyph)]
    )


@dataclass
class _State:
    matrix: Matrix = _IDENTITY
    clips: tuple[_Clip, ...] = ()
    font: Font | None = None
    size: float = 12
    ascent: float = 800
    descent: float = -200
    char_spacing: float = 0
    word_spacing: float = 0
    scale: float = 1
    leading: float = 0
    rise: float = 0


@dataclass(frozen=True)
class _Run:
    text: str
    start: Point
    end: Point
    axis: Point
    height: float
    space: float


def _join(runs: list[_Run]) -> str:
    result: list[str] = []
    previous: _Run | None = None
    for run in runs:
        if previous is not None:
            dx, dy = _difference(run.start, previous.end)
            along = dx * previous.axis[0] + dy * previous.axis[1]
            normal = abs(dx * previous.axis[1] - dy * previous.axis[0])
            parallel = sum(a * b for a, b in zip(run.axis, previous.axis, strict=True)) > 0.99
            space = max(0.01, min(run.space, previous.space))
            if not parallel or normal > max(0.5, min(run.height, previous.height) * 0.5) or along < -space * 2:
                result.append("\n")
            elif along > space * 0.5 and not previous.text[-1].isspace() and not run.text[0].isspace():
                result.append(" ")
        result.append(run.text)
        previous = run
    return "".join(result)


class _PageText:
    def __init__(self, page: PageObject) -> None:
        self.page = page
        self.fonts: dict[int, tuple[Font, float, float]] = {}
        self.runs: list[_Run] = []

    def font(self, resource: DictionaryObject) -> tuple[Font, float, float]:
        key = id(resource)
        if key not in self.fonts:
            descriptor = resource
            if resource.get("/Subtype") == "/Type0":
                descriptor = cast("DictionaryObject", cast("ArrayObject", resource["/DescendantFonts"])[0].get_object())
            descriptor = descriptor.get("/FontDescriptor", {}).get_object() if "/FontDescriptor" in descriptor else {}
            self.fonts[key] = (
                Font.from_font_resource(resource),
                float(descriptor.get("/Ascent", 800)),
                float(descriptor.get("/Descent", -200)),
            )
        return self.fonts[key]

    def show(self, value: bytes | str, state: _State, tm: Matrix) -> Matrix:
        font = state.font
        if font is None:
            raise ValueError("text uses an undeclared font")  # noqa: TRY003
        transform = _multiply(tm, state.matrix)
        scale = math.hypot(transform[0], transform[1])
        if scale < 1e-10:
            return tm
        axis = (transform[0] / scale, transform[1] / scale)
        cursor = 0.0
        text: list[str] = []
        start: Point | None = None
        end: Point = (0, 0)
        for code, char in _glyphs(value, font):
            width = (font.space_width if code == font.space_char else font.get_text_width(code)) * state.size / 1000
            advance = (
                width + state.char_spacing + (state.word_spacing if code == font.space_char else 0)
            ) * state.scale
            glyph = _rectangle(
                (
                    cursor,
                    state.rise + state.descent * state.size / 1000,
                    cursor + width * state.scale,
                    state.rise + state.ascent * state.size / 1000,
                ),
                transform,
            )
            if char and _visible(glyph, state.clips):
                if start is None:
                    start = _point(cursor, state.rise, transform)
                text.append(char)
                end = _point(cursor + advance, state.rise, transform)
            elif text and text[-1] != " ":
                text.append(" ")
            cursor += advance
        if start is not None and (body := "".join(text)):
            self.runs.append(
                _Run(
                    body,
                    start,
                    end,
                    axis,
                    abs(state.size) * math.hypot(transform[2], transform[3]),
                    abs(font.space_width * state.size * state.scale * scale / 1000),
                )
            )
        return _multiply((1, 0, 0, 1, cursor, 0), tm)

    def text_operation(  # noqa: C901
        self, op: bytes, args: list[Any], state: _State, tm: Matrix, line: Matrix
    ) -> tuple[Matrix, Matrix]:
        if op == b"BT":
            return _IDENTITY, _IDENTITY
        if op == b"Tm":
            tm = line = tuple(map(float, args))
        elif op in (b"Td", b"TD", b"T*", b"'", b'"'):
            if op == b"TD":
                state.leading = -float(args[1])
            offset = tuple(map(float, args[:2])) if op in (b"Td", b"TD") else (0, -state.leading)
            line = _multiply((1, 0, 0, 1, *offset), line)
            tm = line
            if op == b'"':
                state.word_spacing, state.char_spacing = map(float, args[:2])
            if op in (b"'", b'"'):
                tm = self.show(args[-1], state, tm)
        elif op == b"Tj":
            tm = self.show(args[0], state, tm)
        elif op == b"TJ":
            for value in args[0]:
                if isinstance(value, (bytes, str)):
                    tm = self.show(value, state, tm)
                else:
                    tm = _multiply((1, 0, 0, 1, -float(value) * state.size * state.scale / 1000, 0), tm)
        else:
            names = {b"Tc": "char_spacing", b"Tw": "word_spacing", b"Tz": "scale", b"TL": "leading", b"Ts": "rise"}
            if op in names:
                setattr(state, names[op], float(args[0]) / (100 if op == b"Tz" else 1))
        return tm, line

    def form(self, obj: DictionaryObject, resources: DictionaryObject, state: _State, active: frozenset[int]) -> None:
        if obj.get("/Subtype") != "/Form":
            return
        key = id(obj)
        if key in active:
            raise ValueError("recursive PDF form")  # noqa: TRY003
        matrix = _multiply(tuple(map(float, obj.get("/Matrix", _IDENTITY))), state.matrix)
        clips = (*state.clips, _Clip((_rectangle(cast("Iterable[float]", obj["/BBox"]), matrix),)))
        self.walk(obj, obj.get("/Resources", resources), replace(state, matrix=matrix, clips=clips), active | {key})

    def walk(self, content: Any, resources: DictionaryObject, state: _State, active: frozenset[int]) -> None:  # noqa: C901
        stream = ContentStream(content, self.page.pdf, "bytes")
        tm = line = _IDENTITY
        stack: list[_State] = []
        paths: list[list[Point]] = []
        pending_clip: bool | None = None
        for args, op in stream.operations:
            if op == b"q":
                stack.append(replace(state))
            elif op == b"Q":
                if not stack:
                    raise ValueError("unbalanced PDF graphics state")  # noqa: TRY003
                state = stack.pop()
            elif op == b"cm":
                state.matrix = _multiply(tuple(map(float, args)), state.matrix)
            elif op == b"Tf":
                fonts = cast("DictionaryObject", resources["/Font"])
                state.font, state.ascent, state.descent = self.font(
                    cast("DictionaryObject", fonts[args[0]].get_object())
                )
                state.size = float(args[1])
            elif op == b"Do":
                forms = cast("DictionaryObject", resources["/XObject"])
                self.form(cast("DictionaryObject", forms[args[0]].get_object()), resources, state, active)
            elif op in (b"m", b"l", b"re", b"c", b"v", b"y", b"h"):
                self.path(op, args, state.matrix, paths)
            elif op in (b"W", b"W*"):
                pending_clip = op == b"W*"
            elif op in (b"n", b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"):
                if pending_clip is not None:
                    state.clips += (_Clip(tuple(tuple(p) for p in paths if len(p) >= 3), pending_clip),)
                paths = []
                pending_clip = None
            elif op.startswith(b"T") or op in (b"BT", b"'", b'"'):
                tm, line = self.text_operation(op, args, state, tm, line)

    @staticmethod
    def path(op: bytes, args: list[Any], matrix: Matrix, paths: list[list[Point]]) -> None:
        if op == b"re":
            x, y, width, height = map(float, args)
            paths.append(list(_rectangle((x, y, x + width, y + height), matrix)))
        elif op == b"m":
            paths.append([_point(float(args[0]), float(args[1]), matrix)])
        elif op == b"l" and paths:
            paths[-1].append(_point(float(args[0]), float(args[1]), matrix))
        elif op == b"h" and paths:
            paths[-1].append(paths[-1][0])
        elif op in (b"c", b"v", b"y") and paths:
            start = paths[-1][-1]
            controls = [_point(float(args[i]), float(args[i + 1]), matrix) for i in range(0, len(args), 2)]
            if op == b"v":
                controls.insert(0, start)
            elif op == b"y":
                controls.append(controls[-1])
            for i in range(1, 17):
                t = i / 16
                weights = ((1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t**2, t**3)
                paths[-1].append((
                    sum(p[0] * w for p, w in zip((start, *controls), weights, strict=True)),
                    sum(p[1] * w for p, w in zip((start, *controls), weights, strict=True)),
                ))


def extract_page_text(page: PageObject) -> str:
    """Return the page's positioned glyphs, retaining text touching a visible edge."""
    if page.get("/Contents") is None:
        return ""
    extractor = _PageText(page)
    clips = (_Clip((_rectangle(page.mediabox),)), _Clip((_rectangle(page.cropbox),)))
    extractor.walk(page["/Contents"], page.get("/Resources", DictionaryObject()), _State(clips=clips), frozenset())
    return _join(extractor.runs)
