"""`.parleyignore` matching - the gitignore subset of SPEC 7.2.

Why not just ``fnmatch``
------------------------
``fnmatch`` is a different language. ``fnmatch("a/b", "*")`` is true; in
gitignore ``*`` deliberately does not cross a ``/``. Getting that wrong means a
single ``*`` line in someone's ``.parleyignore`` silently stops their whole
project from syncing, which is the kind of bug that is only noticed after the
work is lost. So the patterns are compiled to real regexes with gitignore's
segment semantics, and the three rules that actually trip people up are
implemented explicitly:

1. ``*`` and ``?`` never match ``/``; ``**`` is the only thing that spans
   directories, and only when it is a whole path segment.
2. A pattern containing a ``/`` anywhere other than at the very end is
   *anchored* to the workspace root. A pattern without one matches a basename at
   any depth.
3. **A negation cannot resurrect a file whose parent directory is excluded.**
   Git refuses to descend into an ignored directory at all, so ``!secret/keep``
   under an ignored ``secret/`` does nothing. We reproduce that by testing every
   ancestor directory first and short-circuiting.

The builtin list from SPEC 7.2 is absolute: a user negation cannot re-include
``.parley/``, because that directory holds the credentials and the sync index
and shipping it to the Hub would be a security bug, not a preference.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("parley.client.ignore")

IGNORE_FILENAME = ".parleyignore"

#: SPEC 7.2 "always ignored". Not overridable - see the module docstring.
BUILTIN_PATTERNS: Tuple[str, ...] = (
    ".parley/",
    ".git/",
    ".hg/",
    ".svn/",
    "__pycache__/",
    "*.pyc",
    "node_modules/",
    ".venv/",
    "venv/",
    ".DS_Store",
    "Thumbs.db",
    "*.swp",
    "*~",
    ".#*",
)

_CACHE_LIMIT = 20000


# --------------------------------------------------------------------------- #
# Pattern compilation
# --------------------------------------------------------------------------- #


class _Pattern:
    """One compiled ``.parleyignore`` line."""

    __slots__ = ("raw", "negated", "dir_only", "regex")

    def __init__(self, raw: str, negated: bool, dir_only: bool, regex: "re.Pattern[str]") -> None:
        self.raw = raw
        self.negated = negated
        self.dir_only = dir_only
        self.regex = regex

    def matches(self, rel_path: str, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        return self.regex.match(rel_path) is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<_Pattern {0!r}{1}{2}>".format(
            self.raw, " negated" if self.negated else "", " dir_only" if self.dir_only else ""
        )


def _strip_trailing_spaces(line: str) -> str:
    """Drop unescaped trailing whitespace, as git does."""
    i = len(line)
    while i > 0 and line[i - 1] in " \t":
        # A backslash immediately before the run escapes one space.
        backslashes = 0
        j = i - 2
        while j >= 0 and line[j] == "\\":
            backslashes += 1
            j -= 1
        if backslashes % 2 == 1:
            break
        i -= 1
    return line[:i]


def _translate_segment(seg: str) -> str:
    """Compile one path segment's glob to a regex fragment that never spans ``/``."""
    out: List[str] = []
    i = 0
    n = len(seg)
    while i < n:
        ch = seg[i]
        if ch == "\\" and i + 1 < n:
            out.append(re.escape(seg[i + 1]))
            i += 2
            continue
        if ch == "*":
            # A run of '*' inside a segment (e.g. "a**b") is not the "**"
            # path-spanning token; git treats it as a plain '*'.
            while i < n and seg[i] == "*":
                i += 1
            out.append("[^/]*")
            continue
        if ch == "?":
            out.append("[^/]")
            i += 1
            continue
        if ch == "[":
            end = _class_end(seg, i)
            if end < 0:
                out.append(re.escape("["))
                i += 1
                continue
            body = seg[i + 1:end]
            negated = body[:1] in ("!", "^")
            if negated:
                body = body[1:]
            body = body.replace("\\", "\\\\").replace("[", "\\[")
            # A negated class must still not swallow a separator.
            out.append("[^" + body + "/]" if negated else "[" + body + "]")
            i = end + 1
            continue
        out.append(re.escape(ch))
        i += 1
    return "".join(out)


def _class_end(seg: str, start: int) -> int:
    """Index of the ``]`` closing the class opened at ``start``, or -1."""
    j = start + 1
    n = len(seg)
    if j < n and seg[j] in ("!", "^"):
        j += 1
    if j < n and seg[j] == "]":  # a literal ']' may lead the class
        j += 1
    while j < n:
        if seg[j] == "\\":
            j += 2
            continue
        if seg[j] == "]":
            return j
        j += 1
    return -1


def compile_pattern(line: str) -> Optional[_Pattern]:
    """Compile one ``.parleyignore`` line, or return ``None`` if it is inert."""
    raw = line
    if line.endswith("\n"):
        line = line[:-1]
    if line.endswith("\r"):
        line = line[:-1]
    line = _strip_trailing_spaces(line)
    if not line:
        return None
    if line.startswith("#"):
        return None
    negated = False
    if line.startswith("!"):
        negated = True
        line = line[1:]
    elif line.startswith("\\!") or line.startswith("\\#"):
        line = line[1:]
    if not line:
        return None

    dir_only = line.endswith("/")
    if dir_only:
        line = line[:-1]
        if not line:
            return None

    # Anchoring: a separator at the start or in the middle anchors the pattern
    # to the workspace root. A separator only at the end (already stripped
    # above, as `dir_only`) does not.
    anchored = "/" in line
    if line.startswith("/"):
        line = line[1:]
        anchored = True
    if not line:
        return None

    segments = [s for s in line.split("/")]
    # Collapse empty segments produced by "a//b" - harmless, and a user typo.
    segments = [s for s in segments if s != ""] or [""]

    parts: List[str] = []
    if not anchored:
        # Basename semantics: match at any depth.
        parts.append("(?:[^/]+/)*")
    last = len(segments) - 1
    for idx, seg in enumerate(segments):
        if seg == "**":
            if idx == last:
                # Trailing "/**" matches everything *inside* the directory.
                parts.append(".+")
            else:
                parts.append("(?:[^/]+/)*")
            continue
        parts.append(_translate_segment(seg))
        if idx != last:
            parts.append("/")

    # A directory pattern also covers everything beneath it: matching "build/"
    # against "build/x/y.o" must be true, otherwise pruning a tree would require
    # the caller to understand ancestry. (The caller does walk ancestors too,
    # but a flat path list must give the same answer.)
    body = "".join(parts)
    regex = re.compile("(?s:" + body + r")(?:/.*)?\Z")
    return _Pattern(raw.rstrip("\r\n"), negated, dir_only, regex)


def compile_patterns(lines: Sequence[str]) -> List[_Pattern]:
    out: List[_Pattern] = []
    for line in lines:
        try:
            pat = compile_pattern(line)
        except re.error as exc:
            log.warning("ignoring malformed .parleyignore line %r (%s)", line, exc)
            continue
        if pat is not None:
            out.append(pat)
    return out


# --------------------------------------------------------------------------- #
# IgnoreRules
# --------------------------------------------------------------------------- #


class IgnoreRules:
    """The effective ignore set for one workspace."""

    def __init__(
        self,
        patterns: Optional[List[_Pattern]] = None,
        *,
        source: Optional[Path] = None,
        source_mtime_ns: int = 0,
    ) -> None:
        self._builtin = compile_patterns(BUILTIN_PATTERNS)
        self._user = list(patterns or [])
        self.source = source
        self.source_mtime_ns = source_mtime_ns
        self._cache: Dict[Tuple[str, bool], bool] = {}

    # -- construction -------------------------------------------------------
    @classmethod
    def load(cls, workspace: Path) -> "IgnoreRules":
        """Builtin rules plus ``<workspace>/.parleyignore`` if it exists."""
        path = Path(workspace) / IGNORE_FILENAME
        try:
            raw = path.read_bytes()
            mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            return cls(source=path, source_mtime_ns=0)
        except OSError as exc:
            log.warning("cannot read %s (%s); using builtin ignore rules only", path, exc)
            return cls(source=path, source_mtime_ns=0)
        text = raw.decode("utf-8", "replace")
        rules = cls(compile_patterns(text.splitlines()), source=path, source_mtime_ns=mtime)
        log.debug("loaded %d ignore pattern(s) from %s", len(rules._user), path)
        return rules

    @classmethod
    def from_lines(cls, lines: Sequence[str]) -> "IgnoreRules":
        """Build directly from pattern lines - used by tests and by `doctor`."""
        return cls(compile_patterns(lines))

    def reload_if_changed(self) -> bool:
        """Re-read ``.parleyignore`` if it changed on disk. Returns True if it did.

        The sync loop calls this: a user who adds a rule mid-session expects it
        to take effect without restarting the daemon.
        """
        if self.source is None:
            return False
        try:
            mtime = self.source.stat().st_mtime_ns
        except OSError:
            mtime = 0
        if mtime == self.source_mtime_ns:
            return False
        fresh = IgnoreRules.load(self.source.parent)
        self._user = fresh._user
        self.source_mtime_ns = fresh.source_mtime_ns
        self._cache.clear()
        log.info("ignore rules reloaded from %s (%d pattern(s))", self.source, len(self._user))
        return True

    # -- matching -----------------------------------------------------------
    def ignored(self, rel_posix_path: str, *, is_dir: bool = False) -> bool:
        """True if ``rel_posix_path`` (workspace-relative, POSIX) is excluded."""
        path = rel_posix_path.strip("/")
        if not path or path == ".":
            return False
        key = (path, is_dir)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        result = self._evaluate(path, is_dir)
        if len(self._cache) >= _CACHE_LIMIT:
            self._cache.clear()
        self._cache[key] = result
        return result

    def _evaluate(self, path: str, is_dir: bool) -> bool:
        segments = path.split("/")

        # 1. Builtins, on every ancestor and on the path itself. Absolute.
        prefix = ""
        for i, seg in enumerate(segments):
            prefix = seg if i == 0 else prefix + "/" + seg
            last = i == len(segments) - 1
            as_dir = True if not last else is_dir
            if self._match_list(self._builtin, prefix, as_dir):
                return True

        # 2. User rules on every *ancestor directory*. If a parent is excluded
        #    we stop here: no later negation can bring the child back (git).
        for i in range(len(segments) - 1):
            ancestor = "/".join(segments[: i + 1])
            if self._match_list(self._user, ancestor, True):
                return True

        # 3. User rules on the path itself; last match wins, negation allowed.
        return self._match_list(self._user, path, is_dir)

    @staticmethod
    def _match_list(patterns: List[_Pattern], path: str, is_dir: bool) -> bool:
        """Last matching pattern decides - that is gitignore's whole ordering rule."""
        decision = False
        matched = False
        for pat in patterns:
            if pat.matches(path, is_dir):
                decision = not pat.negated
                matched = True
        return decision if matched else False

    # -- introspection ------------------------------------------------------
    def explain(self, rel_posix_path: str, *, is_dir: bool = False) -> str:
        """Human-readable reason, for ``parley doctor``'s ignore-rule sanity check."""
        path = rel_posix_path.strip("/")
        segments = path.split("/") if path else []
        prefix = ""
        for i, seg in enumerate(segments):
            prefix = seg if i == 0 else prefix + "/" + seg
            as_dir = True if i < len(segments) - 1 else is_dir
            for pat in self._builtin:
                if pat.matches(prefix, as_dir):
                    return "excluded by builtin rule {0!r} matching {1!r}".format(pat.raw, prefix)
        for i in range(len(segments) - 1):
            ancestor = "/".join(segments[: i + 1])
            if self._match_list(self._user, ancestor, True):
                return "excluded because the parent directory {0!r} is excluded".format(ancestor)
        winner = None
        for pat in self._user:
            if pat.matches(path, is_dir):
                winner = pat
        if winner is None:
            return "not excluded (no rule matches)"
        if winner.negated:
            return "re-included by {0!r}".format(winner.raw)
        return "excluded by {0!r}".format(winner.raw)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<IgnoreRules builtin={0} user={1}>".format(len(self._builtin), len(self._user))
