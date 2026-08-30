#!/usr/bin/env python3
"""Deterministic, evidence-aware Palimpsest social-card rendering.

The renderer intentionally uses only Python's standard library.  A tiny
Palimpsest-owned bitmap alphabet keeps the 1200x630 output identical across
publisher hosts without relying on an installed font, browser, or image
library.  PNGs contain no timestamps or ancillary chunks: their SHA-256 is a
stable address for the exact card bytes.
"""

from __future__ import annotations

import binascii
import hashlib
import json
import re
import struct
import unicodedata
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts import share_card_cjk_data


WIDTH = 1200
HEIGHT = 630
PNG_MIME = "image/png"
RENDERER_VERSION = "palimpsest.share-card-renderer.v1"
SPEC_VERSION = "palimpsest.share-card-spec.v1"
MANIFEST_VERSION = "palimpsest.share-card-manifest.v1"
OUTPUT_ROOT = Path("assets/share-cards")
MANIFEST_PATH = OUTPUT_ROOT / "manifest.json"
MAX_TEXT_LENGTH = 1_000
_CARD_FILENAME = re.compile(r"sha256-([0-9a-f]{64})\.png")
NONCURRENT_STATUSES = frozenset(
    {
        "missing",
        "stale",
        "corrupt",
        "degraded",
        "unavailable",
        "restricted",
        "abstained",
        "evidence_gathering",
        "warming_up",
        "instrument-warning",
    }
)
_CONTROL_OR_BIDI = frozenset(
    {
        "RLO",
        "LRO",
        "RLE",
        "LRE",
        "PDF",
        "RLI",
        "LRI",
        "FSI",
        "PDI",
        "BN",
    }
)


class ShareCardError(ValueError):
    """A card specification or generated-card manifest is invalid."""


@dataclass(frozen=True)
class RenderedCard:
    """One exact card artifact and its crawler-facing metadata."""

    spec: dict[str, Any]
    spec_sha256: str
    png: bytes
    sha256: str
    path: Path
    url: str
    alt: str


# Five-by-seven glyphs. Lowercase is deliberately rendered as uppercase: the
# compact archival face is part of Palimpsest's provenance-tape visual system.
_GLYPHS = {
    " ": ("00000",) * 7,
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    ".": ("00000", "00000", "00000", "00000", "00000", "00110", "00110"),
    ",": ("00000", "00000", "00000", "00000", "00110", "00110", "00100"),
    ":": ("00000", "00110", "00110", "00000", "00110", "00110", "00000"),
    ";": ("00000", "00110", "00110", "00000", "00110", "00110", "00100"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    "\\": ("10000", "01000", "01000", "00100", "00010", "00010", "00001"),
    "|": ("00100", "00100", "00100", "00100", "00100", "00100", "00100"),
    "'": ("00110", "00110", "00100", "00000", "00000", "00000", "00000"),
    '"': ("01010", "01010", "01010", "00000", "00000", "00000", "00000"),
    "!": ("00100", "00100", "00100", "00100", "00100", "00000", "00100"),
    "?": ("01110", "10001", "00001", "00010", "00100", "00000", "00100"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    "=": ("00000", "11111", "00000", "11111", "00000", "00000", "00000"),
    "%": ("11001", "11010", "00100", "01000", "10110", "00110", "00000"),
    "#": ("01010", "11111", "01010", "01010", "11111", "01010", "00000"),
    "&": ("01100", "10010", "10100", "01000", "10101", "10010", "01101"),
    "@": ("01110", "10001", "10111", "10101", "10111", "10000", "01110"),
    "(": ("00010", "00100", "01000", "01000", "01000", "00100", "00010"),
    ")": ("01000", "00100", "00010", "00010", "00010", "00100", "01000"),
    "[": ("01110", "01000", "01000", "01000", "01000", "01000", "01110"),
    "]": ("01110", "00010", "00010", "00010", "00010", "00010", "01110"),
    "<": ("00010", "00100", "01000", "10000", "01000", "00100", "00010"),
    ">": ("01000", "00100", "00010", "00001", "00010", "00100", "01000"),
    "*": ("00000", "10101", "01110", "11111", "01110", "10101", "00000"),
}


def _canonical_json(value: object, *, pretty: bool = False) -> bytes:
    kwargs: dict[str, Any] = {
        "allow_nan": False,
        "ensure_ascii": False,
        "sort_keys": True,
    }
    if pretty:
        kwargs["indent"] = 2
    else:
        kwargs["separators"] = (",", ":")
    return (json.dumps(value, **kwargs) + "\n").encode("utf-8")


def display_text(value: object, *, limit: int = MAX_TEXT_LENGTH) -> str:
    """Normalize untrusted display text and remove invisible direction controls."""

    if not isinstance(value, str):
        value = str(value)
    normalized = unicodedata.normalize("NFKC", value)
    safe: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if (
            category in {"Cc", "Cs"}
            or unicodedata.bidirectional(character) in _CONTROL_OR_BIDI
        ):
            safe.append(" ")
        elif category in {"Zl", "Zp"}:
            safe.append(" ")
        else:
            safe.append(character)
    return " ".join("".join(safe).split())[:limit]


def _ascii_display(value: object) -> str:
    normalized = display_text(value)
    transliterated = (
        unicodedata.normalize("NFKD", normalized)
        .encode("ascii", "replace")
        .decode("ascii")
    )
    return " ".join(transliterated.split()).upper()


_PUNCTUATION_FALLBACK = {
    "–": "-",
    "—": "-",
    "―": "-",
    "…": "...",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "·": ".",
    "、": ",",
    "。": ".",
    "《": "<",
    "》": ">",
    "「": "[",
    "」": "]",
    "（": "(",
    "）": ")",
    "：": ":",
    "；": ";",
    "！": "!",
    "？": "?",
}


def _latin_glyphs(character: str) -> str:
    fallback = _PUNCTUATION_FALLBACK.get(character)
    if fallback is not None:
        return fallback
    normalized = (
        unicodedata.normalize("NFKD", character)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return normalized.upper() or "?"


def _character_advance(character: str, scale: int) -> int:
    if share_card_cjk_data.glyph_rows(character) is not None:
        pixel = max(1, round(7 * scale / 16))
        return 17 * pixel
    return 6 * scale * len(_latin_glyphs(character))


def _estimated_width(value: object, *, scale: int) -> int:
    return sum(
        _character_advance(character, scale) for character in display_text(value)
    )


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ShareCardError(f"{field} must be a string or null")
    normalized = display_text(value)
    if not normalized:
        raise ShareCardError(f"{field} must not be blank")
    return normalized


def normalize_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the strict canonical card specification used by the renderer."""

    if not isinstance(value, Mapping):
        raise ShareCardError("share-card spec must be an object")
    expected = {
        "schema_version",
        "kind",
        "kicker",
        "title",
        "status",
        "status_label",
        "metric",
        "as_of",
        "source",
        "receipt",
        "target_url",
    }
    if set(value) != expected:
        raise ShareCardError("share-card spec has an invalid shape")
    if value["schema_version"] != SPEC_VERSION:
        raise ShareCardError("share-card spec has an unsupported schema")
    kind = _optional_text(value["kind"], field="kind")
    kicker = _optional_text(value["kicker"], field="kicker")
    title = _optional_text(value["title"], field="title")
    status = _optional_text(value["status"], field="status")
    status_label = _optional_text(value["status_label"], field="status_label")
    as_of = _optional_text(value["as_of"], field="as_of")
    source = _optional_text(value["source"], field="source")
    receipt = _optional_text(value["receipt"], field="receipt")
    target_url = _optional_text(value["target_url"], field="target_url")
    if any(
        item is None for item in (kind, kicker, title, status, status_label, target_url)
    ):
        raise ShareCardError("required share-card text must not be null")
    if not target_url.startswith("https://palimpsest.info/"):
        raise ShareCardError("share-card target URL must use the Palimpsest origin")
    metric_value = value["metric"]
    metric: dict[str, str] | None
    if metric_value is None:
        metric = None
    else:
        if not isinstance(metric_value, Mapping) or set(metric_value) != {
            "value",
            "label",
        }:
            raise ShareCardError("share-card metric has an invalid shape")
        metric_text = _optional_text(metric_value["value"], field="metric.value")
        metric_label = _optional_text(metric_value["label"], field="metric.label")
        if metric_text is None or metric_label is None:
            raise ShareCardError("share-card metric text must not be null")
        metric = {"value": metric_text, "label": metric_label}
    if status in NONCURRENT_STATUSES and metric is not None:
        raise ShareCardError(f"{status} cards must not render a current metric")
    return {
        "schema_version": SPEC_VERSION,
        "kind": kind,
        "kicker": kicker,
        "title": title,
        "status": status,
        "status_label": status_label,
        "metric": metric,
        "as_of": as_of,
        "source": source,
        "receipt": receipt,
        "target_url": target_url,
    }


class _Canvas:
    def __init__(self, width: int, height: int, color: tuple[int, int, int]):
        self.width = width
        self.height = height
        self.pixels = bytearray(bytes(color) * width * height)

    def rect(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        color: tuple[int, int, int],
    ) -> None:
        left = max(0, x)
        top = max(0, y)
        right = min(self.width, x + width)
        bottom = min(self.height, y + height)
        if left >= right or top >= bottom:
            return
        row = bytes(color) * (right - left)
        for py in range(top, bottom):
            offset = (py * self.width + left) * 3
            self.pixels[offset : offset + len(row)] = row

    def text(
        self,
        x: int,
        y: int,
        value: object,
        *,
        scale: int,
        color: tuple[int, int, int],
        max_chars: int | None = None,
    ) -> None:
        text = display_text(value)
        if max_chars is not None:
            text = text[:max_chars]
        cursor = x
        for character in text:
            cjk_rows = share_card_cjk_data.glyph_rows(character)
            if cjk_rows is not None:
                pixel = max(1, round(7 * scale / 16))
                for gy, row in enumerate(cjk_rows):
                    for gx in range(16):
                        if row & (1 << (15 - gx)):
                            self.rect(
                                cursor + gx * pixel,
                                y + gy * pixel,
                                pixel,
                                pixel,
                                color,
                            )
                cursor += 17 * pixel
                continue
            for fallback in _latin_glyphs(character):
                glyph = _GLYPHS.get(fallback, _GLYPHS["?"])
                for gy, row in enumerate(glyph):
                    for gx, bit in enumerate(row):
                        if bit == "1":
                            self.rect(
                                cursor + gx * scale,
                                y + gy * scale,
                                scale,
                                scale,
                                color,
                            )
                cursor += 6 * scale

    def png(self) -> bytes:
        scanlines = bytearray()
        row_size = self.width * 3
        for y in range(self.height):
            scanlines.append(0)
            start = y * row_size
            scanlines.extend(self.pixels[start : start + row_size])
        ihdr = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(bytes(scanlines), level=9))
            + _png_chunk(b"IEND", b"")
        )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def png_dimensions(raw: bytes) -> tuple[int, int]:
    """Return dimensions from a structurally recognizable PNG header."""

    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        raise ShareCardError("share-card bytes are not a PNG")
    return struct.unpack(">II", raw[16:24])


def _wrap(
    value: object, *, max_width: int, scale: int, lines: int
) -> tuple[list[str], bool]:
    remaining = display_text(value)
    result: list[str] = []
    while remaining and len(result) < lines:
        width = 0
        boundary = 0
        last_space = 0
        for index, character in enumerate(remaining, 1):
            next_width = width + _character_advance(character, scale)
            if next_width > max_width:
                break
            width = next_width
            boundary = index
            if character.isspace():
                last_space = index
        if boundary == len(remaining):
            result.append(remaining.strip())
            remaining = ""
            break
        if boundary == 0:
            boundary = 1
        cut = last_space if last_space else boundary
        result.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    truncated = bool(remaining)
    if truncated and result:
        ellipsis = "..."
        line = result[-1]
        while line and _estimated_width(line + ellipsis, scale=scale) > max_width:
            line = line[:-1].rstrip()
        result[-1] = (line + ellipsis) if line else ellipsis
    return result or ["UNTITLED EVIDENCE RECORD"], truncated


def _title_layout(title: str) -> tuple[int, list[str]]:
    for scale in (7, 6, 5, 4):
        wrapped, truncated = _wrap(title, max_width=930, scale=scale, lines=3)
        if not truncated or scale == 4:
            return scale, wrapped
    wrapped, _ = _wrap(title, max_width=930, scale=4, lines=3)
    return 4, wrapped


def _fit_scale(value: str, *, max_width: int, preferred: int, minimum: int) -> int:
    for scale in range(preferred, minimum - 1, -1):
        if _estimated_width(value, scale=scale) <= max_width:
            return scale
    return minimum


def _trace(canvas: _Canvas, seed: bytes) -> None:
    cyan_dim = (10, 44, 54)
    cyan_faint = (7, 27, 34)
    for x in range(72, WIDTH, 96):
        canvas.rect(x, 0, 1, HEIGHT, cyan_faint)
    for y in range(42, HEIGHT, 72):
        canvas.rect(0, y, WIDTH, 1, cyan_faint)
    for index, byte in enumerate(seed[:24]):
        y = 34 + ((byte * 19 + index * 41) % 470)
        x = 620 + ((seed[(index + 7) % len(seed)] * 7) % 420)
        width = 28 + (byte % 9) * 19
        canvas.rect(x, y, min(width, WIDTH - x - 32), 2 if index % 4 else 4, cyan_dim)
    # A broken provenance spine: every gap/segment is derived from the spec hash.
    for index in range(13):
        y = 42 + index * 36
        gap = 7 + seed[index] % 17
        canvas.rect(32, y, 5, 24 - gap // 2, (0, 214, 230))


def _footer_cell(
    canvas: _Canvas,
    *,
    x: int,
    width: int,
    label: str,
    value: str | None,
) -> None:
    canvas.text(x, 548, label, scale=2, color=(0, 197, 216), max_chars=20)
    shown = value or "NOT AVAILABLE"
    scale = _fit_scale(shown, max_width=width, preferred=3, minimum=2)
    if _estimated_width(shown, scale=scale) > width:
        wrapped, _ = _wrap(shown, max_width=width, scale=2, lines=2)
        for index, line in enumerate(wrapped):
            canvas.text(
                x,
                573 + index * 20,
                line,
                scale=2,
                color=(218, 234, 235),
            )
    else:
        canvas.text(
            x,
            579,
            shown,
            scale=scale,
            color=(218, 234, 235),
            max_chars=max(1, width // (6 * scale)),
        )


def _card_alt(spec: Mapping[str, Any]) -> str:
    detail = spec["status_label"]
    metric = spec["metric"]
    if metric is not None:
        detail = f"{metric['value']} {metric['label']}"
    as_of = spec["as_of"] or "not available"
    return display_text(
        f"Palimpsest evidence card: {spec['title']}. {detail}. "
        f"Status {spec['status_label']}. As of {as_of}.",
        limit=420,
    )


def render_card(
    value: Mapping[str, Any], *, site: str = "https://palimpsest.info"
) -> RenderedCard:
    """Render one validated card and address it by the exact PNG digest."""

    spec = normalize_spec(value)
    spec_raw = _canonical_json(spec)
    spec_digest = hashlib.sha256(spec_raw).hexdigest()
    seed = bytes.fromhex(spec_digest)
    canvas = _Canvas(WIDTH, HEIGHT, (3, 8, 11))
    _trace(canvas, seed)
    canvas.rect(0, 0, WIDTH, 7, (0, 219, 233))
    canvas.rect(72, 52, 158, 28, (0, 219, 233))
    canvas.text(84, 59, "PALIMPSEST", scale=2, color=(2, 15, 18))
    canvas.text(252, 59, spec["kicker"], scale=2, color=(118, 181, 188), max_chars=68)
    canvas.text(1000, 59, "TRACE / 01", scale=2, color=(0, 219, 233), max_chars=14)

    noncurrent = spec["status"] in NONCURRENT_STATUSES
    if noncurrent:
        canvas.rect(72, 101, 1056, 38, (70, 32, 19))
        canvas.rect(72, 101, 8, 38, (255, 126, 63))
        canvas.text(
            94, 111, spec["status_label"], scale=3, color=(255, 177, 126), max_chars=50
        )
        title_y = 168
    else:
        canvas.text(
            72, 108, spec["status_label"], scale=2, color=(0, 219, 233), max_chars=72
        )
        title_y = 146

    title_scale, title_lines = _title_layout(spec["title"])
    line_height = title_scale * 9
    for index, line in enumerate(title_lines):
        canvas.text(
            72,
            title_y + index * line_height,
            line,
            scale=title_scale,
            color=(231, 242, 242),
        )

    metric = spec["metric"]
    if metric is not None:
        metric_y = min(418, title_y + len(title_lines) * line_height + 24)
        canvas.rect(72, metric_y - 12, 6, 90, (0, 219, 233))
        metric_scale = _fit_scale(
            metric["value"], max_width=640, preferred=11, minimum=3
        )
        canvas.text(
            98,
            metric_y,
            metric["value"],
            scale=metric_scale,
            color=(0, 219, 233),
            max_chars=max(1, 640 // (6 * metric_scale)),
        )
        label_x = 98 + min(
            650, _estimated_width(metric["value"], scale=metric_scale) + 26
        )
        label_lines, _ = _wrap(
            metric["label"],
            max_width=WIDTH - label_x - 72,
            scale=3,
            lines=2,
        )
        for index, label_line in enumerate(label_lines):
            canvas.text(
                label_x,
                metric_y + 22 + index * 27,
                label_line,
                scale=3,
                color=(146, 196, 201),
            )
    elif noncurrent:
        state_y = min(425, title_y + len(title_lines) * line_height + 28)
        canvas.text(
            72,
            state_y,
            "NO CURRENT METRIC",
            scale=5,
            color=(255, 126, 63),
            max_chars=25,
        )

    canvas.rect(0, 522, WIDTH, 108, (6, 20, 25))
    canvas.rect(0, 522, WIDTH, 2, (0, 219, 233))
    _footer_cell(canvas, x=72, width=300, label="AS OF", value=spec["as_of"])
    _footer_cell(canvas, x=421, width=315, label="STATUS", value=spec["status_label"])
    footer_label = "RECEIPT" if spec["receipt"] else "SOURCE"
    footer_value = spec["receipt"] or spec["source"]
    _footer_cell(canvas, x=785, width=343, label=footer_label, value=footer_value)

    png = canvas.png()
    if png_dimensions(png) != (WIDTH, HEIGHT):
        raise ShareCardError("renderer emitted the wrong PNG dimensions")
    digest = hashlib.sha256(png).hexdigest()
    path = OUTPUT_ROOT / f"sha256-{digest}.png"
    return RenderedCard(
        spec=spec,
        spec_sha256=spec_digest,
        png=png,
        sha256=digest,
        path=path,
        url=f"{site.rstrip('/')}/{path.as_posix()}",
        alt=_card_alt(spec),
    )


def manifest_document(cards: Sequence[RenderedCard]) -> dict[str, Any]:
    """Build the exact renderer/spec/digest inventory consumed by rights gates."""

    by_path: dict[str, RenderedCard] = {}
    for card in cards:
        relative = card.path.as_posix()
        previous = by_path.setdefault(relative, card)
        if previous.png != card.png or previous.spec != card.spec:
            raise ShareCardError(f"share-card digest collision: {relative}")
    rows = [
        {
            "bytes": len(card.png),
            "path": path,
            "sha256": card.sha256,
            "spec_sha256": card.spec_sha256,
            "spec": card.spec,
        }
        for path, card in sorted(by_path.items())
    ]
    return {
        "schema_version": MANIFEST_VERSION,
        "renderer": RENDERER_VERSION,
        "width": WIDTH,
        "height": HEIGHT,
        "cards": rows,
    }


def manifest_bytes(cards: Sequence[RenderedCard]) -> bytes:
    return _canonical_json(manifest_document(cards), pretty=True)


def validate_manifest_document(
    value: object,
) -> list[tuple[dict[str, Any], RenderedCard]]:
    """Re-render every manifest row and prove its path, spec, bytes, and digest."""

    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "renderer",
        "width",
        "height",
        "cards",
    }:
        raise ShareCardError("share-card manifest has an invalid shape")
    if (
        value["schema_version"] != MANIFEST_VERSION
        or value["renderer"] != RENDERER_VERSION
        or value["width"] != WIDTH
        or value["height"] != HEIGHT
    ):
        raise ShareCardError("share-card manifest renderer contract does not match")
    rows = value["cards"]
    if not isinstance(rows, list) or rows != sorted(
        rows, key=lambda row: row.get("path", "") if isinstance(row, dict) else ""
    ):
        raise ShareCardError("share-card manifest rows are not path-sorted")
    validated: list[tuple[dict[str, Any], RenderedCard]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "bytes",
            "path",
            "sha256",
            "spec_sha256",
            "spec",
        }:
            raise ShareCardError("share-card manifest row has an invalid shape")
        if type(row["bytes"]) is not int or row["bytes"] <= 0:
            raise ShareCardError("share-card manifest byte count is invalid")
        if not isinstance(row["path"], str) or row["path"] in seen:
            raise ShareCardError("share-card manifest path is invalid or duplicated")
        seen.add(row["path"])
        path = Path(row["path"])
        filename = _CARD_FILENAME.fullmatch(path.name)
        if (
            path.parent != OUTPUT_ROOT
            or filename is None
            or not isinstance(row["sha256"], str)
            or row["sha256"] != filename.group(1)
            or not isinstance(row["spec_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", row["spec_sha256"]) is None
        ):
            raise ShareCardError("share-card manifest identity is invalid")
        card = render_card(row["spec"])
        if (
            row["sha256"] != card.sha256
            or row["spec_sha256"] != card.spec_sha256
            or row["bytes"] != len(card.png)
            or row["path"] != card.path.as_posix()
            or row["spec"] != card.spec
        ):
            raise ShareCardError("share-card manifest row does not reproduce")
        validated.append((row, card))
    return validated


def parse_manifest(raw: bytes) -> list[tuple[dict[str, Any], RenderedCard]]:
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShareCardError("share-card manifest is invalid JSON") from exc
    if raw != _canonical_json(document, pretty=True):
        raise ShareCardError("share-card manifest is not canonical JSON")
    return validate_manifest_document(document)
