"""SPEC R1 and R2 — stdlib only, Python 3.9 floor, on every module in the package.

This suite is the enforcement arm of the project's central portability promise: "point an
agent at the repo and it runs, with no install step, on whatever Python it already has".
Local development happens on a much newer interpreter than the floor, so nothing else
catches 3.10+ syntax or a stray third-party import. It reads source and walks the AST
rather than importing, so it keeps working while half the package is still being written.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

from tests.helpers import PACKAGE_DIR, REPO_ROOT

#: Modules the reference implementation is allowed to import. Anything outside this (and
#: outside the interpreter's own stdlib list) is a dependency, and SPEC R1 forbids those.
CURATED_STDLIB = frozenset(
    """
    __future__ abc argparse array ast base64 binascii bisect builtins calendar cgi cmd
    codecs collections colorsys concurrent configparser contextlib contextvars copy csv
    ctypes dataclasses datetime decimal difflib dis email encodings enum errno faulthandler
    filecmp fileinput fnmatch fractions ftplib functools gc getopt getpass gettext glob
    graphlib gzip hashlib heapq hmac html http imaplib importlib inspect io ipaddress
    itertools json keyword linecache locale logging lzma mailbox marshal math mimetypes
    mmap multiprocessing netrc numbers operator os pathlib pickle pkgutil platform plistlib
    poplib posixpath pprint pty pwd queue quopri random re reprlib resource runpy sched
    secrets select selectors shelve shlex shutil signal site smtplib socket socketserver
    sqlite3 ssl stat statistics string stringprep struct subprocess symtable sys sysconfig
    tarfile tempfile termios textwrap threading time timeit token tokenize traceback
    tracemalloc types typing unicodedata unittest urllib uu uuid warnings wave weakref
    webbrowser wsgiref xml xmlrpc zipapp zipfile zipimport zlib zoneinfo
    """.split()
)

#: Stdlib modules that only exist above the floor. Importing one breaks a 3.9 user.
BANNED_MODULES = frozenset(["tomllib"])

#: Names and attributes that only exist above the floor. Each entry is the bare attribute
#: or builtin name, which is how it will appear in the AST regardless of how it is spelled.
BANNED_NAMES = {
    "aiter": "builtin, Python 3.10",
    "anext": "builtin, Python 3.10",
    "ExceptionGroup": "builtin, Python 3.11",
    "BaseExceptionGroup": "builtin, Python 3.11",
    "TypeAlias": "typing.TypeAlias, Python 3.10",
    "ParamSpecArgs": "typing, Python 3.10",
    "TypeGuard": "typing.TypeGuard, Python 3.10",
    "Self": "typing.Self, Python 3.11",
    "Never": "typing.Never, Python 3.11",
    "LiteralString": "typing.LiteralString, Python 3.11",
    "NotRequired": "typing.NotRequired, Python 3.11",
    "Required": "typing.Required, Python 3.11",
    "assert_never": "typing.assert_never, Python 3.11",
    "override": "typing.override, Python 3.12",
    "TypeAliasType": "typing.TypeAliasType, Python 3.12",
    "StrEnum": "enum.StrEnum, Python 3.11",
    "pairwise": "itertools.pairwise, Python 3.10",
    "batched": "itertools.batched, Python 3.12",
    "bit_count": "int.bit_count, Python 3.10",
    "file_digest": "hashlib.file_digest, Python 3.11",
    "stdlib_module_names": "sys.stdlib_module_names, Python 3.10",
    "getdefaultencoding": "fine, but see sys.orig_argv note",
    "orig_argv": "sys.orig_argv, Python 3.10",
    "TaskGroup": "asyncio.TaskGroup, Python 3.11",
    "chdir": "contextlib.chdir, Python 3.11 (os.chdir is fine)",
    "get_annotations": "inspect.get_annotations, Python 3.10",
    "deprecated": "warnings.deprecated, Python 3.13",
    "pdb_set_trace_guard": "placeholder",
}
# A few of the above are too generic to ban outright; drop them again rather than carry a
# rule that will produce false positives on honest code.
for _benign in ("getdefaultencoding", "pdb_set_trace_guard", "chdir"):
    BANNED_NAMES.pop(_benign, None)

#: Keyword arguments introduced after the floor.
BANNED_KEYWORDS = {
    ("zip", "strict"): "zip(strict=...), Python 3.10",
    ("dataclass", "slots"): "@dataclass(slots=...), Python 3.10",
    ("dataclass", "kw_only"): "@dataclass(kw_only=...), Python 3.10",
    ("dataclass", "weakref_slot"): "@dataclass(weakref_slot=...), Python 3.11",
    ("field", "kw_only"): "dataclasses.field(kw_only=...), Python 3.10",
}

#: Names that make a ``X | Y`` expression a type union rather than honest bitwise maths.
TYPEISH = frozenset(
    """
    str int float bool bytes bytearray complex dict list tuple set frozenset object type
    None Path PurePath BinaryIO TextIO IO Any Dict List Tuple Set FrozenSet Optional Union
    Callable Mapping MutableMapping Sequence Iterable Iterator Generator Awaitable Coroutine
    Text AnyStr Pattern Match Store HubConfig Credentials ParleyClient Hub Transport
    LedgerResult LedgerLine StateView IgnoreRules WorkspaceSync Event
    """.split()
)

#: ``crypto.py`` is the one documented exception to R1: it may *try* to import an optional
#: accelerator, and must work without it (SPEC §3.6).
OPTIONAL_ACCELERATORS = {"crypto.py": frozenset(["cryptography", "nacl"])}

#: ``print()`` is a CLI affordance. Everywhere else it is a logging bug (INTERNAL-API
#: conventions). ``term.py`` is the CLI's own rendering layer — writing to a caller-supplied
#: stream is the entire point of it — so it counts as part of the CLI surface.
PRINT_ALLOWED = frozenset(["cli.py", "__main__.py", "term.py"])


def package_sources():
    if not PACKAGE_DIR.is_dir():
        return []
    return sorted(p for p in PACKAGE_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def interpreter_stdlib():
    names = getattr(sys, "stdlib_module_names", None)
    return frozenset(names) if names else frozenset()


ALLOWED_MODULES = (CURATED_STDLIB | interpreter_stdlib()) - BANNED_MODULES


class _AnnotationAwareVisitor(ast.NodeVisitor):
    """Collects every node that lives inside an annotation.

    Under ``from __future__ import annotations`` an annotation is never evaluated, so
    ``str | None`` there is a harmless string. Anywhere else it is a runtime ``BinOp`` that
    explodes on Python 3.9. The distinction is the whole point of this walk.
    """

    def __init__(self) -> None:
        self.in_annotation = set()

    def _mark(self, node) -> None:
        if node is None:
            return
        for child in ast.walk(node):
            self.in_annotation.add(id(child))

    def visit_AnnAssign(self, node):  # noqa: N802
        self._mark(node.annotation)
        self.generic_visit(node)

    def visit_arg(self, node):  # noqa: N802
        self._mark(node.annotation)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):  # noqa: N802
        self._mark(node.returns)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef


class TestPackageLayout(unittest.TestCase):
    def test_the_package_directory_exists_and_holds_modules(self):
        self.assertTrue(PACKAGE_DIR.is_dir(), "parley/ is missing: nothing to check")
        sources = package_sources()
        self.assertTrue(sources, "parley/ contains no Python modules")

    def test_this_suite_is_not_vacuous(self):
        names = {p.name for p in package_sources()}
        self.assertIn("ledger.py", names,
                      "the portability walk must at minimum be seeing parley/ledger.py")


class TestEveryModuleIsPortable(unittest.TestCase):
    """One subtest per module so a single bad file names itself instead of hiding."""

    def setUp(self):
        self.sources = package_sources()
        if not self.sources:
            self.skipTest("parley/ contains no Python modules yet")

    def _parse(self, path: Path):
        text = path.read_text(encoding="utf-8")
        return text, ast.parse(text, filename=str(path))

    def test_every_module_compiles(self):
        for path in self.sources:
            with self.subTest(module=rel(path)):
                source = path.read_text(encoding="utf-8")
                compile(source, str(path), "exec")

    def test_every_module_enables_postponed_annotations(self):
        for path in self.sources:
            with self.subTest(module=rel(path)):
                _, tree = self._parse(path)
                body = list(tree.body)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    body = body[1:]  # a module docstring may come first
                self.assertTrue(body, "{0} is empty".format(rel(path)))
                first = body[0]
                self.assertIsInstance(
                    first, ast.ImportFrom,
                    "{0}: first statement must be `from __future__ import annotations` "
                    "(SPEC R2)".format(rel(path)),
                )
                self.assertEqual(first.module, "__future__", rel(path))
                self.assertIn("annotations", [a.name for a in first.names], rel(path))

    def test_no_module_uses_a_match_statement(self):
        for path in self.sources:
            with self.subTest(module=rel(path)):
                _, tree = self._parse(path)
                match_node = getattr(ast, "Match", None)
                if match_node is not None:
                    offenders = [n.lineno for n in ast.walk(tree) if isinstance(n, match_node)]
                    self.assertEqual(
                        offenders, [],
                        "{0}: match/case is Python 3.10+ (SPEC R2)".format(rel(path)),
                    )

    def test_no_module_evaluates_a_pep604_union_at_runtime(self):
        for path in self.sources:
            with self.subTest(module=rel(path)):
                _, tree = self._parse(path)
                marker = _AnnotationAwareVisitor()
                marker.visit(tree)
                offenders = []
                for node in ast.walk(tree):
                    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.BitOr):
                        continue
                    if id(node) in marker.in_annotation:
                        continue
                    if self._looks_like_a_type(node.left) or self._looks_like_a_type(node.right):
                        offenders.append(node.lineno)
                self.assertEqual(
                    offenders, [],
                    "{0}: `X | Y` outside an annotation is evaluated at import time and "
                    "fails on Python 3.9 (SPEC R2); lines {1}".format(rel(path), offenders),
                )

    @staticmethod
    def _looks_like_a_type(node) -> bool:
        if isinstance(node, ast.Constant) and node.value is None:
            return True
        if isinstance(node, ast.Name):
            return node.id in TYPEISH
        if isinstance(node, ast.Attribute):
            return node.attr in TYPEISH
        if isinstance(node, ast.Subscript):
            return TestEveryModuleIsPortable._looks_like_a_type(node.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return (TestEveryModuleIsPortable._looks_like_a_type(node.left)
                    or TestEveryModuleIsPortable._looks_like_a_type(node.right))
        return False

    def test_no_module_uses_an_api_newer_than_the_floor(self):
        for path in self.sources:
            with self.subTest(module=rel(path)):
                _, tree = self._parse(path)
                offenders = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Attribute) and node.attr in BANNED_NAMES:
                        offenders.append((node.lineno, node.attr, BANNED_NAMES[node.attr]))
                    elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
                        offenders.append((node.lineno, node.id, BANNED_NAMES[node.id]))
                    elif isinstance(node, ast.ImportFrom):
                        for alias in node.names:
                            if alias.name in BANNED_NAMES:
                                offenders.append(
                                    (node.lineno, alias.name, BANNED_NAMES[alias.name])
                                )
                    elif isinstance(node, ast.Call):
                        offenders.extend(self._banned_keywords(node))
                self.assertEqual(
                    offenders, [],
                    "{0}: uses APIs newer than Python 3.9: {1}".format(rel(path), offenders),
                )

    @staticmethod
    def _banned_keywords(node):
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            return []
        found = []
        for keyword in node.keywords:
            if keyword.arg and (name, keyword.arg) in BANNED_KEYWORDS:
                found.append((node.lineno, name, BANNED_KEYWORDS[(name, keyword.arg)]))
        return found

    def test_every_module_imports_only_the_standard_library(self):
        for path in self.sources:
            with self.subTest(module=rel(path)):
                _, tree = self._parse(path)
                extra = OPTIONAL_ACCELERATORS.get(path.name, frozenset())
                offenders = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            root = alias.name.split(".")[0]
                            if root not in ALLOWED_MODULES and root not in extra and root != "parley":
                                offenders.append((node.lineno, alias.name))
                    elif isinstance(node, ast.ImportFrom):
                        if node.level:  # a relative import stays inside the package
                            continue
                        root = (node.module or "").split(".")[0]
                        if root and root not in ALLOWED_MODULES and root not in extra and root != "parley":
                            offenders.append((node.lineno, node.module))
                self.assertEqual(
                    offenders, [],
                    "{0}: SPEC R1 is stdlib only; found {1}".format(rel(path), offenders),
                )

    def test_optional_accelerators_never_run_at_import_time(self):
        """The property that matters: importing the module must not need the accelerator.

        Two patterns satisfy that — a ``try: import …`` guard, or a deferred import inside
        a function the caller invokes defensively. Either way the import must not sit at
        module scope unguarded, because then the optional dependency is not optional.
        """
        for path in self.sources:
            extra = OPTIONAL_ACCELERATORS.get(path.name)
            if not extra:
                continue
            with self.subTest(module=rel(path)):
                _, tree = self._parse(path)
                safe = set()
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.Try, ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for child in ast.walk(node):
                        if isinstance(child, ast.Import):
                            safe.update(id(child) for _ in (0,))
                        elif isinstance(child, ast.ImportFrom):
                            safe.add(id(child))
                        if isinstance(child, ast.Import):
                            safe.add(id(child))
                for node in ast.walk(tree):
                    roots = []
                    if isinstance(node, ast.Import):
                        roots = [a.name.split(".")[0] for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                        roots = [node.module.split(".")[0]]
                    for root in roots:
                        if root in extra:
                            self.assertIn(
                                id(node), safe,
                                "{0} line {1}: `{2}` is imported at module scope; wrap it in "
                                "try/except or defer it into a function so the pure-Python "
                                "fallback still works (SPEC R1/§3.6)".format(
                                    rel(path), node.lineno, root),
                            )

    @staticmethod
    def _main_guard_lines(tree):
        """Line numbers inside an ``if __name__ == "__main__":`` block.

        A module that doubles as a maintainer script may print there; that code never
        runs when the module is imported, so it is not a logging bug.
        """
        lines = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if (isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name) and test.left.id == "__name__"):
                for child in node.body:
                    for inner in ast.walk(child):
                        if hasattr(inner, "lineno"):
                            lines.add(inner.lineno)
        return lines

    def test_only_the_cli_prints(self):
        for path in self.sources:
            if path.name in PRINT_ALLOWED:
                continue
            with self.subTest(module=rel(path)):
                _, tree = self._parse(path)
                exempt = self._main_guard_lines(tree)
                offenders = [
                    node.lineno for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "print"
                    and node.lineno not in exempt
                ]
                self.assertEqual(
                    offenders, [],
                    "{0}: use logging, not print(), outside cli.py (INTERNAL-API "
                    "conventions); lines {1}".format(rel(path), offenders),
                )

    def test_no_module_shells_out_to_git_or_curl(self):
        """SPEC R3: no shelling out to git/curl on any path."""
        for path in self.sources:
            with self.subTest(module=rel(path)):
                text = path.read_text(encoding="utf-8")
                tree = ast.parse(text, filename=str(path))
                offenders = []
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if name not in ("run", "check_output", "call", "check_call", "Popen"):
                        continue
                    for arg in node.args[:1]:
                        for literal in ast.walk(arg):
                            if (isinstance(literal, ast.Constant)
                                    and isinstance(literal.value, str)
                                    and literal.value.split(" ")[0] in ("git", "curl", "wget")):
                                offenders.append((node.lineno, literal.value))
                self.assertEqual(offenders, [], "{0}: {1}".format(rel(path), offenders))


class TestSuiteItselfIsPortable(unittest.TestCase):
    """The tests have to run on the floor too, or they cannot certify it."""

    def _test_sources(self):
        return sorted(
            p for p in (REPO_ROOT / "tests").rglob("*.py")
            if "__pycache__" not in p.parts
        )

    def test_every_test_module_compiles_and_avoids_match_statements(self):
        match_node = getattr(ast, "Match", None)
        for path in self._test_sources():
            with self.subTest(module=rel(path)):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                if match_node is not None:
                    self.assertEqual(
                        [n.lineno for n in ast.walk(tree) if isinstance(n, match_node)], []
                    )

    def test_the_test_suite_imports_no_third_party_module(self):
        allowed = ALLOWED_MODULES | {"parley", "tests"}
        for path in self._test_sources():
            with self.subTest(module=rel(path)):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                offenders = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        offenders += [a.name for a in node.names
                                      if a.name.split(".")[0] not in allowed]
                    elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                        if node.module.split(".")[0] not in allowed:
                            offenders.append(node.module)
                self.assertEqual(offenders, [],
                                 "{0}: no pytest, no third-party anything".format(rel(path)))


if __name__ == "__main__":
    unittest.main()
