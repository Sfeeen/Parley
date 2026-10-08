"""Terminal rendering helpers for the Parley CLI.

Why this module exists: the first thing a human sees of Parley is a watchword they
are about to read down a phone line, and the second thing is usually a ``doctor``
table.  Both have to look deliberate in a good terminal and stay perfectly legible
in a bad one -- a Windows ``cmd.exe``, a CI log, a pipe into ``jq``.

Two rules are absolute here:

* **Never write an escape sequence into a pipe.**  Colour is decided per output
  stream, from that stream's own ``isatty()``, plus ``NO_COLOR`` / ``TERM`` /
  Windows VT enablement.
* **Never write a character the stream cannot encode.**  Box drawing degrades to
  ``+-|`` when ``stdout.encoding`` cannot carry it.

Stdlib only, Python 3.9 floor.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from typing import Iterable, List, Optional, Sequence, Tuple, Union

__all__ = [
    "Term",
    "colour_supported",
    "unicode_supported",
    "enable_windows_ansi",
    "strip_ansi",
    "visible_width",
    "truncate",
]

# --------------------------------------------------------------------------- #
# Capability detection
# --------------------------------------------------------------------------- #

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

#: Probe string: if the stream's encoding can carry these, it can carry the whole
#: glyph table below.
_UNICODE_PROBE = "┌─┐│└┘·✔✖▲…"


def enable_windows_ansi() -> bool:
    """Turn on virtual-terminal processing for the Windows console.

    Returns True when ANSI sequences are safe to emit afterwards.  Harmless and
    False on every non-Windows platform.
    """
    if sys.platform != "win32":  # pragma: no cover - platform specific
        return False
    try:  # pragma: no cover - platform specific
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        enable_vt = 0x0004
        ok = False
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            if handle in (0, -1, None):
                continue
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            if kernel32.SetConsoleMode(handle, mode.value | enable_vt):
                ok = True
        return ok
    except Exception:  # pragma: no cover - defensive
        return False


def colour_supported(stream=None) -> bool:
    """Decide whether *stream* may receive ANSI colour.

    Order of authority: ``NO_COLOR`` (any value, even empty, disables -- that is
    the published contract of no-color.org) > forced colour > TTY-ness > ``TERM``
    > Windows VT enablement.
    """
    stream = sys.stdout if stream is None else stream

    if os.environ.get("PARLEY_NO_COLOR") or os.environ.get("NO_COLOR") is not None:
        return False

    forced = os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE")
    if forced not in (None, "", "0"):
        if sys.platform == "win32":  # pragma: no cover - platform specific
            enable_windows_ansi()
        return True

    if os.environ.get("CLICOLOR") == "0":
        return False

    try:
        if not stream.isatty():
            return False
    except Exception:
        return False

    term = os.environ.get("TERM", "")
    if sys.platform == "win32":  # pragma: no cover - platform specific
        if os.environ.get("WT_SESSION") or os.environ.get("ANSICON"):
            return True
        if term and term not in ("dumb",):
            return True
        return enable_windows_ansi()

    if term in ("", "dumb"):
        return False
    return True


def unicode_supported(stream=None) -> bool:
    """True when *stream* can encode the box-drawing/glyph set."""
    stream = sys.stdout if stream is None else stream
    if os.environ.get("PARLEY_ASCII") not in (None, "", "0"):
        return False
    encoding = getattr(stream, "encoding", None) or ""
    if not encoding:
        return False
    try:
        _UNICODE_PROBE.encode(encoding)
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------- #
# Width-aware string helpers (ANSI sequences are zero width)
# --------------------------------------------------------------------------- #


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def visible_width(text: str) -> int:
    """Printable column count: ANSI stripped, combining marks 0, wide chars 2."""
    width = 0
    for ch in strip_ansi(text):
        if unicodedata.combining(ch):
            continue
        if ch in ("​", "﻿"):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """Truncate to *width* printable columns.  Styling is not preserved; callers
    truncate plain text and style afterwards."""
    if width <= 0:
        return ""
    plain = strip_ansi(text)
    if visible_width(plain) <= width:
        return plain
    keep = max(0, width - visible_width(ellipsis))
    out = []
    used = 0
    for ch in plain:
        step = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
        if used + step > keep:
            break
        out.append(ch)
        used += step
    return "".join(out) + ellipsis


# --------------------------------------------------------------------------- #
# Styles and glyphs
# --------------------------------------------------------------------------- #

_SGR = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "italic": "3",
    "underline": "4",
    "reverse": "7",
    "black": "30",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "white": "37",
    "grey": "90",
    "bred": "91",
    "bgreen": "92",
    "byellow": "93",
    "bblue": "94",
    "bmagenta": "95",
    "bcyan": "96",
    "bwhite": "97",
    "on_red": "41",
    "on_green": "42",
    "on_yellow": "43",
    "on_blue": "44",
    "on_grey": "100",
}

_GLYPHS_UNICODE = {
    "tl": "┌", "tr": "┐", "bl": "└", "br": "┘",
    "h": "─", "v": "│", "ml": "├", "mr": "┤",
    "dtl": "╔", "dtr": "╗", "dbl": "╚", "dbr": "╝",
    "dh": "═", "dv": "║",
    "dot": "·", "arrow": "→", "bullet": "•",
    "pass": "✔", "fail": "✖", "warn": "▲", "skip": "–",
    "ellipsis": "…",
    "n1": "①", "n2": "②", "n3": "③", "n4": "④",
}

_GLYPHS_ASCII = {
    "tl": "+", "tr": "+", "bl": "+", "br": "+",
    "h": "-", "v": "|", "ml": "+", "mr": "+",
    "dtl": "#", "dtr": "#", "dbl": "#", "dbr": "#",
    "dh": "=", "dv": "#",
    "dot": "-", "arrow": "->", "bullet": "*",
    "pass": "+", "fail": "x", "warn": "!", "skip": "-",
    "ellipsis": "...",
    "n1": "1.", "n2": "2.", "n3": "3.", "n4": "4.",
}

#: A fixed, deterministic 256-colour ramp so an agent keeps one colour everywhere.
_HUE_RAMP = (
    196, 202, 208, 214, 220, 190, 148, 118, 84, 48, 44, 39,
    33, 69, 99, 135, 171, 201, 199, 197,
)

Cell = Union[str, Tuple[str, str]]


class Term:
    """A renderer bound to one output stream.

    ``colour`` and ``unicode`` may be forced (``True``/``False``) by the caller --
    the CLI exposes ``--color``/``--no-color``/``--ascii`` for that -- otherwise
    they are detected from the stream.
    """

    def __init__(
        self,
        stream=None,
        *,
        colour: Optional[bool] = None,
        unicode: Optional[bool] = None,
        width: Optional[int] = None,
    ) -> None:
        self.stream = sys.stdout if stream is None else stream
        self.colour = colour_supported(self.stream) if colour is None else bool(colour)
        self.unicode = unicode_supported(self.stream) if unicode is None else bool(unicode)
        self._forced_width = width
        self.g = _GLYPHS_UNICODE if self.unicode else _GLYPHS_ASCII

    # -- geometry ---------------------------------------------------------- #

    @property
    def width(self) -> int:
        if self._forced_width:
            return max(40, self._forced_width)
        env = os.environ.get("COLUMNS")
        if env and env.isdigit():
            return max(40, int(env))
        try:
            cols = shutil.get_terminal_size((80, 24)).columns
        except Exception:  # pragma: no cover - defensive
            cols = 80
        return max(40, cols)

    def layout_width(self, maximum: int = 80) -> int:
        """Width to lay content out in: never wider than the terminal, never so
        wide that a box becomes hard to scan."""
        return max(46, min(maximum, self.width - 2))

    # -- styling ----------------------------------------------------------- #

    def paint(self, text: str, *styles: str) -> str:
        if not self.colour or not styles:
            return text
        codes = [_SGR[s] for s in styles if s in _SGR]
        if not codes:
            return text
        return "\x1b[" + ";".join(codes) + "m" + text + "\x1b[0m"

    def bold(self, text: str) -> str:
        return self.paint(text, "bold")

    def dim(self, text: str) -> str:
        return self.paint(text, "dim")

    def ok(self, text: str) -> str:
        return self.paint(text, "green")

    def bad(self, text: str) -> str:
        return self.paint(text, "red")

    def warn(self, text: str) -> str:
        return self.paint(text, "yellow")

    def key(self, text: str) -> str:
        return self.paint(text, "cyan")

    def agent_colour(self, agent_id: str, text: str) -> str:
        """SPEC 8.3 agent colour, mapped onto a 256-colour terminal ramp."""
        if not self.colour or not agent_id:
            return text
        try:
            hue = int(agent_id[-4:], 16) % 360
        except ValueError:
            hue = sum(ord(c) for c in agent_id) % 360
        code = _HUE_RAMP[(hue * len(_HUE_RAMP)) // 360]
        return "\x1b[38;5;%dm%s\x1b[0m" % (code, text)

    # -- composition ------------------------------------------------------- #

    def rule(self, title: str = "", width: Optional[int] = None) -> str:
        width = width or self.layout_width()
        if not title:
            return self.dim(self.g["h"] * width)
        label = " " + title + " "
        left = 2
        right = max(0, width - left - visible_width(label))
        return self.dim(self.g["h"] * left) + self.bold(label) + self.dim(self.g["h"] * right)

    def box(
        self,
        lines: Sequence[str],
        *,
        title: str = "",
        width: Optional[int] = None,
        pad: int = 2,
        double: bool = False,
        style: str = "",
        indent: int = 0,
    ) -> List[str]:
        """Draw a box around *lines*.  Lines may already contain ANSI styling."""
        width = width or self.layout_width()
        inner = max(4, width - 2 - 2 * pad)
        if double:
            tl, tr, bl, br, h, v = (
                self.g["dtl"], self.g["dtr"], self.g["dbl"], self.g["dbr"], self.g["dh"], self.g["dv"],
            )
        else:
            tl, tr, bl, br, h, v = (
                self.g["tl"], self.g["tr"], self.g["bl"], self.g["br"], self.g["h"], self.g["v"],
            )

        def edge(text: str) -> str:
            return self.paint(text, style) if style else text

        top = tl + h * (width - 2) + tr
        if title:
            label = " " + title + " "
            if visible_width(label) < width - 6:
                top = tl + h * 2 + label + h * (width - 4 - visible_width(label)) + tr
        out = [edge(top)]
        for raw in lines:
            text = raw
            if visible_width(text) > inner:
                text = truncate(text, inner)
            fill = " " * max(0, inner - visible_width(text))
            out.append(edge(v) + " " * pad + text + fill + " " * pad + edge(v))
        out.append(edge(bl + h * (width - 2) + br))
        if indent:
            pre = " " * indent
            out = [pre + line for line in out]
        return out

    def centre(self, text: str, width: Optional[int] = None) -> str:
        width = width or self.layout_width()
        pad = max(0, (width - visible_width(text)) // 2)
        return " " * pad + text

    def kv(self, pairs: Sequence[Tuple[str, str]], *, gap: int = 2, label_style: str = "dim") -> List[str]:
        if not pairs:
            return []
        label_w = max(visible_width(k) for k, _ in pairs)
        out = []
        for k, v in pairs:
            label = k + " " * (label_w - visible_width(k))
            out.append(self.paint(label, label_style) + " " * gap + v)
        return out

    def table(
        self,
        headers: Sequence[str],
        rows: Sequence[Sequence[Cell]],
        *,
        aligns: Optional[Sequence[str]] = None,
        max_width: Optional[int] = None,
        min_widths: Optional[Sequence[int]] = None,
    ) -> List[str]:
        """Render an aligned table.

        A cell is either a plain string or ``(text, style)``.  Text is truncated
        *before* styling so escape sequences never land mid-sequence.
        """
        ncols = len(headers)
        if ncols == 0:
            return []
        aligns = list(aligns or ["l"] * ncols)
        while len(aligns) < ncols:
            aligns.append("l")

        def cell_text(cell: Cell) -> str:
            return cell[0] if isinstance(cell, tuple) else cell

        def cell_style(cell: Cell) -> str:
            return cell[1] if isinstance(cell, tuple) else ""

        widths = [visible_width(h) for h in headers]
        for row in rows:
            for i in range(ncols):
                cell = row[i] if i < len(row) else ""
                widths[i] = max(widths[i], visible_width(cell_text(cell)))
        if min_widths:
            for i, mw in enumerate(min_widths[:ncols]):
                widths[i] = max(widths[i], mw)

        limit = max_width or self.width - 1
        gap = 2
        total = sum(widths) + gap * (ncols - 1)
        if total > limit:
            # Shrink the widest column first until it fits; never below 6 columns.
            overflow = total - limit
            while overflow > 0:
                widest = max(range(ncols), key=lambda i: widths[i])
                if widths[widest] <= 6:
                    break
                take = min(overflow, widths[widest] - 6)
                widths[widest] -= take
                overflow -= take

        def fmt(text: str, w: int, align: str) -> str:
            text = truncate(text, w)
            padding = " " * max(0, w - visible_width(text))
            if align == "r":
                return padding + text
            if align == "c":
                left = len(padding) // 2
                return " " * left + text + " " * (len(padding) - left)
            return text + padding

        out = [(" " * gap).join(
            self.paint(fmt(h, widths[i], aligns[i]), "bold", "underline") for i, h in enumerate(headers)
        ).rstrip()]
        for row in rows:
            parts = []
            for i in range(ncols):
                cell = row[i] if i < len(row) else ""
                text = fmt(cell_text(cell), widths[i], aligns[i])
                st = cell_style(cell)
                parts.append(self.paint(text, *st.split("+")) if st else text)
            out.append((" " * gap).join(parts).rstrip())
        return out

    # -- output ------------------------------------------------------------ #

    def write(self, *lines: Union[str, Iterable[str]]) -> None:
        for item in lines:
            if isinstance(item, str):
                print(item, file=self.stream)
            else:
                for line in item:
                    print(line, file=self.stream)

    def blank(self, n: int = 1) -> None:
        for _ in range(n):
            print("", file=self.stream)

    def flush(self) -> None:
        try:
            self.stream.flush()
        except Exception:  # pragma: no cover - defensive
            pass
