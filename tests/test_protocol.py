"""SPEC §2, §6 and §7.1 — event validation, the PSR schema, and the path boundary.

``normalise_path`` and ``safe_join`` are a **security boundary**, not a formatting
convenience. SPEC §7.1 says a client MUST re-validate every path it receives from the Hub
before touching the filesystem, precisely because a malicious Hub or peer will try to write
outside the workspace. The ``TestPathsAreASecurityBoundary`` case below is therefore an
attack suite, and it is the single most important thing in this file.
"""

from __future__ import annotations

import os
import tempfile
import unicodedata
import unittest
from pathlib import Path

from parley import protocol
from parley.errors import BadPath
from tests.helpers import AGENT_A, AGENT_B, SESSION, ev, filesystem_is_case_insensitive, \
    supports_symlinks

#: Every type in SPEC §4. If the implementation and this list disagree, one of them is wrong.
SPEC_EVENT_TYPES = frozenset([
    "agent.hello", "agent.heartbeat", "agent.offline", "agent.bye", "agent.revoked",
    "status.update",
    "chat.message", "chat.reaction",
    "file.put", "file.delete", "file.conflict", "file.move",
    "lock.acquire", "lock.release", "lock.denied",
    "task.create", "task.claim", "task.release", "task.update", "task.done",
    "knowledge.contribution",
    "decision.propose", "decision.vote", "decision.resolve",
    # SPEC §4.9 / §15 -- the Exchange. Missing types here are not cosmetic: the
    # Hub validates on ingest, so an unregistered type rejects every message of
    # that kind with 422 bad_event and the whole feature is silently dead.
    "capability.announce", "capability.revoke",
    "request.create", "request.accept", "request.decline", "request.progress",
    "request.result", "request.cancel", "request.taken", "request.expired",
    "hub.started", "hub.policy", "hub.notice",
])


class TestEventTypeTable(unittest.TestCase):
    def test_the_implementation_knows_exactly_the_types_the_spec_lists(self):
        self.assertEqual(set(protocol.EVENT_TYPES), set(SPEC_EVENT_TYPES))

    def test_psr_states_are_the_closed_set_from_the_spec(self):
        self.assertEqual(
            tuple(protocol.PSR_STATES),
            ("idle", "planning", "working", "reviewing", "blocked", "waiting", "offline"),
        )

    def test_the_documented_size_limits_match_the_spec(self):
        self.assertEqual(protocol.MAX_BODY_BYTES, 256 * 1024)
        self.assertEqual(protocol.MAX_CHAT_BYTES, 16 * 1024)


class TestMakeEvent(unittest.TestCase):
    def test_a_fresh_event_has_the_spec_shape(self):
        event = protocol.make_event(AGENT_A, SESSION, "chat.message", {"text": "hi"})
        self.assertEqual(event["v"], "PARLEY/1")
        self.assertEqual(event["actor"], AGENT_A)
        self.assertEqual(event["session"], SESSION)
        self.assertEqual(event["type"], "chat.message")
        self.assertEqual(event["body"], {"text": "hi"})
        self.assertTrue(event["id"].startswith("evt_"))
        self.assertEqual(len(event["id"]), 20)

    def test_the_client_never_sets_seq(self):
        """SPEC §2: seq is assigned by the Hub. Clients MUST NOT set it."""
        event = protocol.make_event(AGENT_A, SESSION, "chat.message", {"text": "hi"})
        self.assertNotIn("seq", event)

    def test_the_timestamp_is_rfc3339_utc_with_milliseconds(self):
        event = protocol.make_event(AGENT_A, SESSION, "chat.message", {"text": "hi"})
        ts = event["ts"]
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_an_explicit_id_and_timestamp_are_honoured(self):
        event = protocol.make_event(AGENT_A, SESSION, "chat.message", {"text": "x"},
                                    event_id="evt_0123456789abcdef",
                                    ts="2026-10-08T12:34:56.789Z")
        self.assertEqual(event["id"], "evt_0123456789abcdef")
        self.assertEqual(event["ts"], "2026-10-08T12:34:56.789Z")

    def test_ids_are_unique(self):
        ids = {protocol.make_event(AGENT_A, SESSION, "chat.message", {})["id"]
               for _ in range(500)}
        self.assertEqual(len(ids), 500)


class TestEventValidation(unittest.TestCase):
    def good(self, **overrides):
        event = protocol.make_event(AGENT_A, SESSION, "chat.message", {"text": "hi"})
        event.update(overrides)
        return event

    def test_a_well_formed_event_has_no_problems(self):
        self.assertEqual(protocol.validate_event(self.good()), [])

    def test_a_missing_or_wrong_wire_version_is_a_problem(self):
        for value in (None, "", "PARLEY/2", "parley/1", 1):
            with self.subTest(v=value):
                event = self.good()
                if value is None:
                    event.pop("v")
                else:
                    event["v"] = value
                self.assertNotEqual(protocol.validate_event(event), [])

    def test_a_missing_actor_session_or_type_is_a_problem(self):
        for field in ("actor", "session", "type"):
            with self.subTest(field=field):
                event = self.good()
                event.pop(field)
                self.assertNotEqual(protocol.validate_event(event), [])

    def test_an_unknown_type_outside_the_extension_space_is_rejected(self):
        """SPEC §2.1: unknown types are rejected unless the namespace is x.*"""
        for etype in ("wibble.thing", "chat.shout", "agent.teleport", "", "CHAT.MESSAGE"):
            with self.subTest(etype=etype):
                self.assertNotEqual(protocol.validate_event(self.good(type=etype)), [])

    def test_an_extension_type_is_accepted(self):
        """SPEC §2.1: x.* MUST be accepted, stored and relayed."""
        for etype in ("x.custom", "x.vendor.thing", "x.a.b.c"):
            with self.subTest(etype=etype):
                event = self.good(type=etype, body={"anything": [1, {"deep": True}]})
                self.assertEqual(protocol.validate_event(event, strict=False), [])

    def test_a_body_over_256_kib_is_rejected(self):
        event = self.good(body={"text": "x" * (256 * 1024 + 10)})
        self.assertNotEqual(protocol.validate_event(event), [])

    def test_a_body_just_under_the_limit_is_accepted(self):
        event = self.good(type="x.bulk", body={"blob": "x" * (200 * 1024)})
        self.assertEqual(protocol.validate_event(event, strict=False), [])

    def test_chat_text_over_16_kib_is_rejected(self):
        """SPEC §4.3: chat text is <= 16 KiB."""
        self.assertNotEqual(
            protocol.validate_event(self.good(body={"text": "x" * (16 * 1024 + 1)})), []
        )
        self.assertEqual(
            protocol.validate_event(self.good(body={"text": "x" * (16 * 1024 - 10)})), []
        )

    def test_a_body_that_is_not_an_object_is_rejected(self):
        for body in ("text", 42, [1, 2], None):
            with self.subTest(body=body):
                self.assertNotEqual(protocol.validate_event(self.good(body=body)), [])

    def test_a_malformed_identifier_is_a_problem(self):
        for field, value in (("id", "not-an-event-id"), ("actor", "nope"),
                             ("session", "sess_1234"), ("id", "evt_ZZZZ")):
            with self.subTest(field=field, value=value):
                self.assertNotEqual(protocol.validate_event(self.good(**{field: value})), [])

    def test_a_malformed_timestamp_is_a_problem(self):
        for ts in ("yesterday", "2026-10-08", "", "12:34:56", "2026-13-45T99:99:99.000Z"):
            with self.subTest(ts=ts):
                self.assertNotEqual(protocol.validate_event(self.good(ts=ts)), [])

    def test_a_produced_timestamp_always_carries_milliseconds(self):
        """SPEC §1.2 is normative for producers; a validator may be lenient on input."""
        for _ in range(5):
            event = protocol.make_event(AGENT_A, SESSION, "chat.message", {"text": "x"})
            self.assertRegex(event["ts"], r"\.\d{3}Z$")

    def test_the_hub_actor_is_accepted(self):
        event = protocol.make_event("hub", SESSION, "hub.notice", {"text": "hello"})
        self.assertEqual(protocol.validate_event(event), [])
        self.assertTrue(protocol.is_hub_authored(event))
        self.assertFalse(protocol.is_hub_authored(self.good()))

    def test_validation_returns_a_list_of_strings_not_an_exception(self):
        problems = protocol.validate_event({"nonsense": True})
        self.assertIsInstance(problems, list)
        for problem in problems:
            self.assertIsInstance(problem, str)
            self.assertTrue(problem.strip())


class TestForwardCompatibility(unittest.TestCase):
    """SPEC §2.1: unknown fields MUST be preserved and forwarded untouched."""

    def test_unknown_fields_inside_a_known_body_survive_validation(self):
        body = {"text": "hi", "x_future": {"nested": [1, 2, 3]}, "unknown": None}
        event = protocol.make_event(AGENT_A, SESSION, "chat.message", dict(body))
        protocol.validate_event(event)
        self.assertEqual(event["body"]["x_future"], {"nested": [1, 2, 3]})
        self.assertIn("unknown", event["body"])

    def test_unknown_top_level_fields_survive_validation(self):
        event = protocol.make_event(AGENT_A, SESSION, "chat.message", {"text": "hi"})
        event["x_routing_hint"] = "eu-west"
        protocol.validate_event(event)
        self.assertEqual(event["x_routing_hint"], "eu-west")

    def test_a_round_trip_through_canonical_json_is_lossless(self):
        from parley import jsonutil

        body = {"text": "hi", "x_future": {"n": [1, 2, 3]}, "unicode": "héllo ☃"}
        event = protocol.make_event(AGENT_A, SESSION, "x.vendor.thing", body)
        restored = jsonutil.loads(jsonutil.canonical(event))
        self.assertEqual(restored, event)

    def test_an_extension_event_survives_a_round_trip_untouched(self):
        from parley import jsonutil

        event = protocol.make_event(AGENT_A, SESSION, "x.telemetry",
                                    {"samples": [1.5, 2.5], "meta": {"unit": "ms"}})
        self.assertEqual(jsonutil.loads(jsonutil.canonical(event)), event)

    def test_event_summary_never_raises_on_anything(self):
        for event in (
            {},
            {"type": "x.unknown"},
            protocol.make_event(AGENT_A, SESSION, "chat.message", {"text": "hi"}),
            protocol.make_event("hub", SESSION, "hub.notice", {"text": "n"}),
            {"type": "chat.message", "body": None, "actor": None},
            {"type": "file.put", "body": {"path": "a/b.py"}, "actor": AGENT_A, "seq": 3},
        ):
            with self.subTest(event=event):
                summary = protocol.event_summary(event)
                self.assertIsInstance(summary, str)
                self.assertNotIn("\n", summary, "a watch line must stay on one line")


class TestPSRValidation(unittest.TestCase):
    """SPEC §6."""

    def good(self, **overrides):
        psr = {
            "state": "working",
            "headline": "Wiring the SSE reconnect backoff",
            "detail": "Full-jitter backoff, resume from last seq.",
            "focus": ["parley/client/client.py"],
            "task": "tsk_4b19ac72",
            "progress": 0.4,
            "since": "2026-10-08T12:30:00.000Z",
        }
        psr.update(overrides)
        return psr

    def test_a_conforming_psr_has_no_problems(self):
        self.assertEqual(protocol.validate_psr(self.good()), [])

    SPEC_EXAMPLE = {
        "state": "working",
        "headline": "Wiring the SSE reconnect backoff",
        "detail": "Full-jitter backoff, resume from last seq; testing against a killed hub.",
        "focus": ["parley/client/client.py", "tests/test_reconnect.py"],
        "task": "tsk_4b19ac72",
        "progress": 0.4,
        "blocked_on": {"agent": AGENT_B, "reason": "needs the conflict-naming decision"},
        "needs": ["decision on conflict file naming"],
        "eta_s": 900,
        "since": "2026-10-08T12:30:00.000Z",
    }

    def test_the_spec_example_raises_only_the_documented_warning(self):
        """The §6 example pairs ``blocked_on`` with ``state: working``, which §6.1 calls a
        warning rather than an error — so it must pass in the non-strict mode the Hub uses
        for warnings, and never be the thing that rejects a conforming agent's PSR."""
        problems = protocol.validate_psr(dict(self.SPEC_EXAMPLE))
        self.assertTrue(all("blocked_on" in problem for problem in problems),
                        "the only complaint about the spec's own example may be the "
                        "documented blocked_on warning; got {0}".format(problems))
        event = protocol.make_event(AGENT_A, SESSION, "status.update", dict(self.SPEC_EXAMPLE))
        self.assertEqual(protocol.validate_event(event, strict=False), [])

    def test_the_spec_example_is_clean_once_the_state_matches(self):
        psr = dict(self.SPEC_EXAMPLE, state="blocked")
        self.assertEqual(protocol.validate_psr(psr), [])

    def test_headline_is_required(self):
        for headline in (None, "", "   "):
            with self.subTest(headline=headline):
                psr = self.good()
                if headline is None:
                    psr.pop("headline")
                else:
                    psr["headline"] = headline
                self.assertNotEqual(protocol.validate_psr(psr), [])

    def test_headline_is_capped_at_eighty_characters(self):
        self.assertEqual(protocol.validate_psr(self.good(headline="x" * 80)), [])
        self.assertNotEqual(protocol.validate_psr(self.good(headline="x" * 81)), [])

    def test_an_unknown_state_is_reported_but_does_not_crash_a_consumer(self):
        problems = protocol.validate_psr(self.good(state="transcending"))
        self.assertNotEqual(problems, [])
        self.assertIsInstance(problems, list)

    def test_every_documented_state_is_accepted(self):
        for state in protocol.PSR_STATES:
            with self.subTest(state=state):
                self.assertEqual(protocol.validate_psr(self.good(state=state)), [])

    def test_focus_is_capped_at_eight_paths(self):
        self.assertEqual(protocol.validate_psr(
            self.good(focus=["a{0}.py".format(i) for i in range(8)])), [])
        self.assertNotEqual(protocol.validate_psr(
            self.good(focus=["a{0}.py".format(i) for i in range(9)])), [])

    def test_focus_entries_must_be_workspace_relative_posix_paths(self):
        for bad in (["/etc/passwd"], ["../escape.py"], ["C:\\win.ini"], ["a\\b.py"]):
            with self.subTest(focus=bad):
                self.assertNotEqual(protocol.validate_psr(self.good(focus=bad)), [])

    def test_progress_must_lie_between_zero_and_one(self):
        for value in (0.0, 0.5, 1.0):
            with self.subTest(progress=value):
                self.assertEqual(protocol.validate_psr(self.good(progress=value)), [])
        for value in (-0.1, 1.1, 42, "half"):
            with self.subTest(progress=value):
                self.assertNotEqual(protocol.validate_psr(self.good(progress=value)), [])

    def test_blocked_on_without_the_blocked_state_is_a_warning_not_an_error(self):
        """SPEC §6.1 says so explicitly, and strict=False is where warnings are dropped."""
        psr = self.good(state="working",
                        blocked_on={"agent": AGENT_B, "reason": "waiting on a decision"})
        self.assertNotEqual(protocol.validate_psr(psr), [])
        event = protocol.make_event(AGENT_A, SESSION, "status.update", psr)
        self.assertEqual(protocol.validate_event(event, strict=False), [])

    def test_blocked_on_with_the_blocked_state_is_clean(self):
        psr = self.good(state="blocked",
                        blocked_on={"agent": AGENT_B, "reason": "waiting on a decision"})
        self.assertEqual(protocol.validate_psr(psr), [])

    def test_a_status_update_event_is_validated_against_the_psr_schema(self):
        event = protocol.make_event(AGENT_A, SESSION, "status.update", {"state": "working"})
        self.assertNotEqual(protocol.validate_event(event), [],
                            "a PSR with no headline is non-conforming (SPEC §6.1)")


class TestNormalisePath(unittest.TestCase):
    """SPEC §7.1."""

    def test_an_ordinary_relative_path_passes_through(self):
        for path in ("a.py", "parley/hub/server.py", "docs/SPEC.md", "a/b/c/d/e.txt",
                     "with space.txt", "dash-and_underscore.py", "UPPER/Case.MD"):
            with self.subTest(path=path):
                self.assertEqual(protocol.normalise_path(path), path)

    def test_paths_are_nfc_normalised(self):
        decomposed = "cafe\u0301.txt"
        self.assertEqual(protocol.normalise_path(decomposed),
                         unicodedata.normalize("NFC", decomposed))
        self.assertEqual(protocol.normalise_path(decomposed), "caf\u00e9.txt")

    def test_case_is_preserved_exactly(self):
        """The Hub keeps the exact path so it can tell a case-only collision apart."""
        self.assertEqual(protocol.normalise_path("README.md"), "README.md")
        self.assertNotEqual(protocol.normalise_path("README.md"),
                            protocol.normalise_path("readme.md"))

    def test_the_empty_path_is_rejected(self):
        for path in ("", "   ", "\t"):
            with self.subTest(path=path):
                with self.assertRaises(BadPath):
                    protocol.normalise_path(path)

    def test_a_leading_slash_is_rejected(self):
        for path in ("/a.py", "/", "//etc/passwd", "/etc/shadow"):
            with self.subTest(path=path):
                with self.assertRaises(BadPath):
                    protocol.normalise_path(path)

    def test_dotdot_segments_are_rejected(self):
        for path in ("../a.py", "a/../b.py", "a/..", "..",
                     "a/b/../../../etc/passwd", "..../a.py".replace("....", "..")):
            with self.subTest(path=path):
                with self.assertRaises(BadPath):
                    protocol.normalise_path(path)

    def test_single_dot_segments_are_rejected(self):
        """SPEC §7.1: a wire path has 'no `.` or `..` segment', and the Hub MUST reject
        anything else. Silently dropping `.` is safe but non-canonical input then gets a
        200, which a conformance suite pointed at another Hub would flag."""
        for path in (".", "./a.py", "a/./b.py", "a/."):
            with self.subTest(path=path):
                with self.assertRaises(BadPath):
                    protocol.normalise_path(path)

    def test_backslashes_are_rejected(self):
        """SPEC §7.1 lists 'no backslash' among the things the Hub MUST reject.

        Rewriting `\\` to `/` is safe against traversal, but it means the Hub stores a
        path the author never sent, and two authors can address one file by two spellings.
        """
        for path in ("a\\b.py", "..\\..\\windows\\win.ini", "a/b\\c"):
            with self.subTest(path=path):
                with self.assertRaises(BadPath):
                    protocol.normalise_path(path)

    def test_drive_letters_are_rejected(self):
        for path in ("C:\\windows\\win.ini", "C:/windows/win.ini", "c:a.py", "Z:"):
            with self.subTest(path=path):
                with self.assertRaises(BadPath):
                    protocol.normalise_path(path)

    def test_unc_paths_are_rejected(self):
        for path in ("\\\\server\\share\\x", "//server/share/x", "\\\\?\\C:\\x"):
            with self.subTest(path=path):
                with self.assertRaises(BadPath):
                    protocol.normalise_path(path)

    def test_a_trailing_slash_is_rejected(self):
        for path in ("a/", "a/b/", "dir/"):
            with self.subTest(path=path):
                with self.assertRaises(BadPath):
                    protocol.normalise_path(path)

    def test_empty_segments_are_rejected(self):
        for path in ("a//b.py", "a///b", "a/"):
            with self.subTest(path=path):
                with self.assertRaises(BadPath):
                    protocol.normalise_path(path)

    def test_null_bytes_and_control_characters_are_rejected(self):
        for path in ("a\x00b", "\x00", "a.py\x00.txt", "a\nb", "a\rb"):
            with self.subTest(path=repr(path)):
                with self.assertRaises(BadPath):
                    protocol.normalise_path(path)

    @staticmethod
    def _path_of_bytes(total, char="a"):
        """Build a path of exactly ``total`` bytes from segments a filesystem will take.

        Individual segments are kept short because a real filesystem caps a name at 255
        bytes; SPEC \u00a77.1's 1024 is the limit on the *whole* path.
        """
        segments = []
        remaining = total
        while remaining > 0:
            take = min(100, remaining)
            segments.append(char * take)
            remaining -= take
        return "/".join(segments)[:total] if total else ""

    def test_a_path_of_1024_bytes_is_accepted(self):
        path = self._path_of_bytes(1024)
        self.assertEqual(len(path.encode("utf-8")), 1024)
        self.assertEqual(protocol.normalise_path(path), path)

    def test_a_path_of_1025_bytes_is_rejected(self):
        with self.assertRaises(BadPath):
            protocol.normalise_path(self._path_of_bytes(1025))

    def test_the_length_limit_counts_utf8_bytes_not_characters(self):
        two_byte = "\u00e9"  # 2 bytes in UTF-8
        path = "/".join([two_byte * 50] * 10)  # 500 chars + 9 slashes = 1009 bytes
        self.assertEqual(len(path.encode("utf-8")), 1009)
        self.assertEqual(protocol.normalise_path(path), path)
        too_long = "/".join([two_byte * 50] * 11)  # 1110 bytes
        self.assertGreater(len(too_long.encode("utf-8")), 1024)
        self.assertLess(len(too_long), 1024, "it is under 1024 *characters* on purpose")
        with self.assertRaises(BadPath):
            protocol.normalise_path(too_long)

    def test_a_two_thousand_byte_path_is_rejected(self):
        with self.assertRaises(BadPath):
            protocol.normalise_path("deep/" * 400 + "file.txt")

    def test_url_encoded_traversal_is_not_silently_decoded(self):
        """Decoding here would turn an inert string into a real escape."""
        for path in ("%2e%2e/secret", "%2E%2E%2Fsecret", "a/%2e%2e/b", "%252e%252e/x"):
            with self.subTest(path=path):
                try:
                    result = protocol.normalise_path(path)
                except BadPath:
                    continue
                self.assertNotIn("..", result,
                                 "normalise_path must not URL-decode its input")

    def test_non_ascii_lookalike_separators_are_not_folded_into_traversal(self):
        """NFC must not turn a fullwidth full stop into a real `..` segment."""
        for path in ("\uff0e\uff0e/secret", "\u2024\u2024/secret", "\uff0f..\uff0fsecret"):
            with self.subTest(path=repr(path)):
                try:
                    result = protocol.normalise_path(path)
                except BadPath:
                    continue
                self.assertNotIn("../", result)
                self.assertNotEqual(result.split("/")[0], "..")

    def test_a_non_string_input_is_rejected(self):
        for value in (None, 42, b"a.py", ["a.py"]):
            with self.subTest(value=value):
                with self.assertRaises((BadPath, TypeError, AttributeError)):
                    protocol.normalise_path(value)


class TestPathsAreASecurityBoundary(unittest.TestCase):
    """``safe_join`` is the last line of defence before the filesystem (SPEC §7.1)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "ws"
        self.workspace.mkdir()
        self.outside = Path(self._tmp.name) / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("classified", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def assert_contained(self, wire_path):
        """Either reject the path, or resolve it strictly inside the workspace."""
        try:
            joined = protocol.safe_join(self.workspace, wire_path)
        except BadPath:
            return
        except Exception as exc:
            self.fail("safe_join({0!r}) raised {1} instead of BadPath: {2}".format(
                wire_path, type(exc).__name__, exc))
        root = os.path.realpath(str(self.workspace))
        target = os.path.realpath(str(joined))
        self.assertTrue(
            target == root or target.startswith(root + os.sep),
            "safe_join({0!r}) escaped the workspace: {1}".format(wire_path, target),
        )

    def test_an_ordinary_path_lands_inside_the_workspace(self):
        joined = protocol.safe_join(self.workspace, "a/b/c.py")
        self.assertEqual(joined, self.workspace / "a" / "b" / "c.py")

    def test_traversal_attacks_cannot_escape(self):
        for attack in (
            "../outside/secret.txt",
            "../../etc/passwd",
            "a/../../outside/secret.txt",
            "a/b/../../../outside/secret.txt",
            "./../outside/secret.txt",
            "..",
            "../",
            "a/..",
            "x/" * 50 + "../" * 60 + "outside/secret.txt",
        ):
            with self.subTest(attack=attack):
                self.assert_contained(attack)

    def test_absolute_paths_cannot_escape(self):
        for attack in ("/etc/passwd", "/", str(self.outside / "secret.txt"),
                       "//etc/passwd", "/proc/self/environ"):
            with self.subTest(attack=attack):
                self.assert_contained(attack)

    def test_windows_shaped_attacks_cannot_escape(self):
        for attack in ("C:\\windows\\win.ini", "C:/windows/win.ini",
                       "..\\..\\windows\\win.ini", "\\\\server\\share\\x",
                       "\\\\?\\C:\\windows\\win.ini", "a\\..\\..\\b"):
            with self.subTest(attack=attack):
                self.assert_contained(attack)

    def test_encoded_and_unicode_tricks_cannot_escape(self):
        for attack in (
            "%2e%2e/outside/secret.txt",
            "%2E%2E%2Foutside%2Fsecret.txt",
            "\uff0e\uff0e/outside/secret.txt",
            "\u2024\u2024/outside/secret.txt",
            "..\u200d/outside/secret.txt",
            "\ufeff../outside/secret.txt",
        ):
            with self.subTest(attack=repr(attack)):
                self.assert_contained(attack)

    def test_null_bytes_cannot_truncate_the_path(self):
        for attack in ("a.py\x00/../../outside/secret.txt", "\x00", "a\x00b"):
            with self.subTest(attack=repr(attack)):
                self.assert_contained(attack)

    def test_an_absurdly_long_path_is_rejected_not_truncated(self):
        self.assert_contained("a/" * 5000 + "b.txt")
        self.assert_contained("x" * 2000)

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_a_symlink_pointing_out_of_the_workspace_is_refused(self):
        if not supports_symlinks(self.workspace):
            self.skipTest("this filesystem does not support symlinks")
        (self.workspace / "escape").symlink_to(self.outside)
        self.assert_contained("escape/secret.txt")
        self.assert_contained("escape")

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_a_symlink_staying_inside_the_workspace_is_fine(self):
        if not supports_symlinks(self.workspace):
            self.skipTest("this filesystem does not support symlinks")
        (self.workspace / "sub").mkdir()
        (self.workspace / "link").symlink_to(self.workspace / "sub")
        self.assert_contained("link/file.txt")

    def test_a_case_only_collision_is_visible_to_the_caller(self):
        """SPEC §7.1: on a case-insensitive filesystem the client must emit a conflict,
        which means the two wire paths have to stay distinguishable up to that point."""
        self.assertNotEqual(protocol.normalise_path("README.md"),
                            protocol.normalise_path("readme.md"))
        upper = protocol.safe_join(self.workspace, "README.md")
        lower = protocol.safe_join(self.workspace, "readme.md")
        self.assertNotEqual(upper, lower, "safe_join must not case-fold the path")
        if filesystem_is_case_insensitive(self.workspace):
            upper.write_text("A", encoding="utf-8")
            self.assertTrue(
                lower.exists(),
                "this filesystem is case-insensitive, so the client must detect the "
                "collision before writing (SPEC §7.1)",
            )
            self.assertEqual(lower.read_text(encoding="utf-8"), "A")

    def test_safe_join_accepts_a_string_workspace_as_well_as_a_path(self):
        joined = protocol.safe_join(Path(str(self.workspace)), "a.py")
        self.assertEqual(Path(joined).name, "a.py")

    def test_safe_join_does_not_create_anything_on_disk(self):
        before = sorted(p.name for p in self.workspace.iterdir())
        protocol.safe_join(self.workspace, "a/b/c.py")
        self.assertEqual(sorted(p.name for p in self.workspace.iterdir()), before)


class TestLockAndTaskBodies(unittest.TestCase):
    """SPEC §4.5 and §4.6 shapes that the Hub has to accept."""

    def test_the_documented_bodies_validate(self):
        cases = [
            ("lock.acquire", {"paths": ["parley/hub/server.py"], "ttl_s": 600,
                              "intent": "rewriting the router"}),
            ("lock.release", {"paths": ["parley/hub/server.py"]}),
            ("task.create", {"id": "tsk_4b19ac72", "title": "Sync reconciler",
                             "tags": ["sync"], "priority": 2}),
            ("task.claim", {"id": "tsk_4b19ac72"}),
            ("task.update", {"id": "tsk_4b19ac72", "status": "doing", "progress": 0.3}),
            ("task.done", {"id": "tsk_4b19ac72", "result": "merged"}),
            ("knowledge.contribution", {"kind": "decision", "title": "SSE beats WebSockets",
                                        "detail": "Survives proxies.",
                                        "refs": [{"kind": "file", "value": "parley/hub/server.py"}]}),
            ("decision.propose", {"id": "dec_1", "question": "Which transport?",
                                  "options": [{"key": "sse", "label": "SSE"}],
                                  "quorum": "majority"}),
            ("decision.vote", {"id": "dec_1", "option": "sse"}),
            ("file.put", {"path": "a.py", "hash": "sha256:" + "ab" * 32, "size": 10}),
            ("file.delete", {"path": "a.py"}),
            ("file.move", {"from": "a.py", "to": "b.py", "hash": "sha256:" + "ab" * 32}),
            ("chat.reaction", {"target": "evt_0123456789abcdef", "reaction": "+1"}),
            ("agent.heartbeat", {"psr_seq": 12, "workspace_files": 3, "workspace_bytes": 99}),
            ("agent.bye", {"reason": "done"}),
        ]
        for etype, body in cases:
            with self.subTest(etype=etype):
                event = protocol.make_event(AGENT_A, SESSION, etype, body)
                self.assertEqual(protocol.validate_event(event), [], etype)

    def test_a_file_event_with_a_bad_path_is_rejected(self):
        event = protocol.make_event(AGENT_A, SESSION, "file.put",
                                    {"path": "../escape.py", "hash": "sha256:" + "ab" * 32,
                                     "size": 1})
        self.assertNotEqual(protocol.validate_event(event), [])


if __name__ == "__main__":
    unittest.main()
