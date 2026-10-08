"""SPEC §14 — the PARLEY/1 conformance suite.

This is how a third-party implementation proves it speaks the protocol. It talks to a Hub
over plain HTTP and asserts only things SPEC.md makes normative, so it can be pointed at
**any** Hub, not only the reference one::

    PARLEY_CONFORMANCE_URL=http://10.0.0.5:7777 \\
    PARLEY_CONFORMANCE_WATCHWORD="copper otter climbs the quiet hill" \\
    python3 -m unittest tests.test_conformance -v

With no environment set it starts the bundled reference Hub on a loopback port, so the
suite is also a regression test for this repository.

Optional knobs:

``PARLEY_CONFORMANCE_ITERATIONS``
    PBKDF2 iteration count if the Hub under test does not use the default 200 000.

What is checked is exactly SPEC §14's list: §3.3 authentication, §4 event types with §2
validation, a conforming PSR, §7.6 conflict handling, and preservation of unknown fields
and ``x.*`` types. The Deck and the Ledger are explicitly **not** required for conformance —
a headless participant is a valid participant — so nothing here touches them.
"""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path

from parley import crypto
from tests.helpers import http_call, decode_json, wait_until

HUB_URL = ""
SESSION = ""
WATCHWORD = ""
ITERATIONS = 200000
_LOCAL = {"hub": None, "tmp": None}


def setUpModule():  # noqa: N802 - unittest's spelling
    global HUB_URL, SESSION, WATCHWORD, ITERATIONS

    WATCHWORD = os.environ.get("PARLEY_CONFORMANCE_WATCHWORD", "")
    HUB_URL = os.environ.get("PARLEY_CONFORMANCE_URL", "").rstrip("/")
    ITERATIONS = int(os.environ.get("PARLEY_CONFORMANCE_ITERATIONS", "200000"))

    if not HUB_URL:
        try:
            from parley.hub.server import create_parley
        except Exception as exc:
            raise unittest.SkipTest(
                "no PARLEY_CONFORMANCE_URL was given and the reference Hub is not "
                "importable yet ({0}: {1})".format(type(exc).__name__, exc)
            )
        _LOCAL["tmp"] = tempfile.TemporaryDirectory()
        hub, WATCHWORD = create_parley(Path(_LOCAL["tmp"].name), name="conformance", port=0)
        hub.start()
        _LOCAL["hub"] = hub
        HUB_URL = hub.url.rstrip("/")

    if not WATCHWORD:
        raise unittest.SkipTest(
            "PARLEY_CONFORMANCE_URL was given without PARLEY_CONFORMANCE_WATCHWORD; "
            "the suite needs an invite to enrol with"
        )

    status, _, body = http_call(HUB_URL + "/v1/hello")
    if status != 200:
        raise unittest.SkipTest(
            "{0}/v1/hello answered {1}; is the Hub running?".format(HUB_URL, status)
        )
    SESSION = (decode_json(body) or {}).get("session", "")
    if not SESSION:
        raise unittest.SkipTest("/v1/hello did not report a session id")


def tearDownModule():  # noqa: N802 - unittest's spelling
    hub = _LOCAL.get("hub")
    if hub is not None:
        hub.stop()
    tmp = _LOCAL.get("tmp")
    if tmp is not None:
        tmp.cleanup()


class ConformanceTestCase(unittest.TestCase):
    """Base: one enrolled agent per test *class*, plus signed-request plumbing.

    Deliberately per class rather than per test: SPEC §3.4 caps a parley at ``max_agents``
    (16 by default), so a suite that enrols for every test would exhaust the roster and
    then skip the rest of itself — looking green while testing almost nothing.
    """

    _agent = None

    @classmethod
    def setUpClass(cls):
        cls._agent = cls.enrol("conf-" + cls.__name__[4:24])

    @classmethod
    def enrol(cls, name):
        root = crypto.derive_root_key(WATCHWORD, SESSION, iterations=ITERATIONS)
        key = crypto.enroll_key(root)
        body = json.dumps({"session": SESSION, "agent": {
            "name": name, "kind": "conformance-suite", "os": "linux",
            "client_version": "1.0.0", "capabilities": ["chat", "sync", "tasks", "psr"],
        }}).encode("utf-8")
        status, _, payload = cls.signed("POST", "/v1/enroll", body, "enroll", key)
        parsed = decode_json(payload) or {}
        if status != 201:
            raise unittest.SkipTest(
                "could not enrol with the Hub under test ({0}): {1}. If the code is "
                "enroll_closed, the parley's max_agents/enroll_max_uses is already "
                "spent — start a fresh one to run the suite.".format(status, parsed)
            )
        return parsed["agent_id"], bytes.fromhex(parsed["agent_key"]), parsed

    @staticmethod
    def signed(method, path, body, agent, key, *, timestamp=None, nonce=None, extra=None):
        raw = body or b""
        stamp = str(int(time.time()) if timestamp is None else int(timestamp))
        chosen = nonce or crypto.new_nonce_hex()
        sts = crypto.string_to_sign(method, path, raw, stamp, chosen, SESSION, agent)
        headers = {
            "X-Parley-Version": "PARLEY/1",
            "X-Parley-Session": SESSION,
            "X-Parley-Agent": agent,
            "X-Parley-Timestamp": stamp,
            "X-Parley-Nonce": chosen,
            "Authorization": "Parley-HMAC-SHA256 " + crypto.sign(key, sts),
            "Content-Type": "application/json; charset=utf-8",
        }
        headers.update(extra or {})
        return http_call(HUB_URL + path, method, raw if raw else None, headers)

    def setUp(self):
        self.agent_id, self.key, self.enrolment = self._agent

    def call(self, method, path, obj=None, **kwargs):
        body = None if obj is None else (
            obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
        )
        status, headers, payload = self.signed(
            method, path, body, kwargs.pop("agent", self.agent_id),
            kwargs.pop("key", self.key), **kwargs
        )
        return status, headers, decode_json(payload)

    def event(self, etype, body, event_id=None):
        return {
            "v": "PARLEY/1",
            "id": event_id or ("evt_" + os.urandom(8).hex()),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "session": SESSION,
            "actor": self.agent_id,
            "type": etype,
            "body": body,
        }

    def emit(self, etype, body, **kwargs):
        status, _, payload = self.call("POST", "/v1/events", self.event(etype, body, **kwargs))
        self.assertIn(status, (200, 201), payload)
        return payload

    def log(self, since=0):
        status, _, payload = self.call("GET", "/v1/events?since={0}&limit=1000".format(since))
        self.assertEqual(status, 200, payload)
        if isinstance(payload, dict):
            return payload.get("events", [])
        return payload or []


class TestDiscoveryIsUnauthenticated(unittest.TestCase):
    """SPEC §5: ``/v1/hello`` takes no auth and leaks nothing."""

    def test_hello_reports_the_wire_version_and_the_required_fields(self):
        status, headers, body = http_call(HUB_URL + "/v1/hello")
        self.assertEqual(status, 200)
        payload = decode_json(body)
        self.assertEqual(payload["v"], "PARLEY/1")
        for field in ("session", "fingerprint", "name", "agents_online",
                      "requires_seal", "server_time"):
            self.assertIn(field, payload)

    def test_hello_does_not_reveal_the_invite(self):
        _, _, body = http_call(HUB_URL + "/v1/hello")
        text = body.decode("utf-8", "replace")
        self.assertNotIn(WATCHWORD, text)
        self.assertNotIn(crypto.normalise_watchword(WATCHWORD), text)

    def test_every_response_carries_the_time_and_head_seq_headers(self):
        _, headers, _ = http_call(HUB_URL + "/v1/hello")
        lowered = {k.lower(): v for k, v in headers.items()}
        self.assertIn("x-parley-time", lowered)
        self.assertIn("x-parley-seq", lowered)


class TestAuthentication(ConformanceTestCase):
    """SPEC §3.3 — the four things a conformant Hub must reject."""

    def test_a_correctly_signed_request_is_accepted(self):
        status, _, payload = self.call("GET", "/v1/state")
        self.assertEqual(status, 200, payload)

    def test_a_bad_signature_is_401(self):
        status, _, payload = self.call("GET", "/v1/state", key=b"\x00" * 32)
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"]["code"], "bad_signature")

    def test_a_timestamp_beyond_300_seconds_is_401(self):
        status, _, payload = self.call("GET", "/v1/state", timestamp=time.time() - 400)
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"]["code"], "stale_timestamp")

    def test_a_replayed_nonce_is_401(self):
        nonce = crypto.new_nonce_hex()
        stamp = int(time.time())
        first = self.call("GET", "/v1/state", nonce=nonce, timestamp=stamp)
        self.assertEqual(first[0], 200, first[2])
        status, _, payload = self.call("GET", "/v1/state", nonce=nonce, timestamp=stamp)
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"]["code"], "replayed_nonce")

    def test_an_unknown_agent_is_401(self):
        status, _, payload = self.call("GET", "/v1/state", agent="agt_ffffffffffffffff")
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"]["code"], "unknown_agent")

    def test_the_error_body_has_the_documented_shape(self):
        _, _, payload = self.call("GET", "/v1/state", key=b"\x00" * 32)
        for field in ("code", "message", "retryable"):
            self.assertIn(field, payload["error"])

    def test_no_error_body_contains_the_invite(self):
        bodies = []
        for kwargs in ({"key": b"\x00" * 32}, {"timestamp": time.time() - 400},
                       {"agent": "agt_ffffffffffffffff"}):
            bodies.append(json.dumps(self.call("GET", "/v1/state", **kwargs)[2]))
        for body in bodies:
            self.assertNotIn(WATCHWORD, body)
            self.assertNotIn(crypto.normalise_watchword(WATCHWORD), body)


class TestEnrolment(ConformanceTestCase):
    """SPEC §3.4."""

    def test_the_enrolment_response_carries_everything_a_client_needs(self):
        for field in ("agent_id", "agent_key", "session", "fingerprint",
                      "hub_time", "seq", "policy"):
            self.assertIn(field, self.enrolment)
        self.assertTrue(self.enrolment["agent_id"].startswith("agt_"))
        self.assertEqual(len(self.enrolment["agent_key"]), 64)
        self.assertEqual(self.enrolment["session"], SESSION)

    def test_the_policy_block_carries_the_documented_knobs(self):
        for field in ("heartbeat_s", "psr_max_age_s", "max_blob_bytes", "sealed", "poll_ms"):
            self.assertIn(field, self.enrolment["policy"])

    def test_the_fingerprint_matches_the_one_hello_advertises(self):
        _, _, body = http_call(HUB_URL + "/v1/hello")
        self.assertEqual(self.enrolment["fingerprint"], decode_json(body)["fingerprint"])

    def test_the_fingerprint_is_three_words(self):
        self.assertEqual(len(self.enrolment["fingerprint"].split("-")), 3)

    def test_a_wrong_invite_cannot_enrol(self):
        wrong = crypto.enroll_key(
            crypto.derive_root_key("not-the-right-words-at-all", SESSION, iterations=1000)
        )
        body = json.dumps({"session": SESSION, "agent": {
            "name": "Mallory", "kind": "x", "os": "linux",
            "client_version": "1", "capabilities": []}}).encode("utf-8")
        status, _, payload = self.signed("POST", "/v1/enroll", body, "enroll", wrong)
        self.assertGreaterEqual(status, 400, payload)


class TestEventPlane(ConformanceTestCase):
    """SPEC §2 and §4."""

    def test_an_appended_event_is_assigned_a_seq(self):
        payload = self.emit("chat.message", {"text": "conformance hello"})
        self.assertIn("seq", json.dumps(payload))

    def test_seq_is_strictly_increasing(self):
        before = self.log()
        for index in range(5):
            self.emit("chat.message", {"text": "n{0}".format(index)})
        seqs = [e["seq"] for e in self.log(since=before[-1]["seq"] if before else 0)]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_each_of_the_documented_event_types_is_accepted(self):
        task_id = "tsk_" + os.urandom(4).hex()
        cases = [
            ("agent.heartbeat", {}),
            ("status.update", {"state": "working", "headline": "Running the suite"}),
            ("chat.message", {"text": "hello"}),
            ("chat.reaction", {"target": "evt_0123456789abcdef", "reaction": "+1"}),
            ("lock.acquire", {"paths": ["conformance.txt"], "ttl_s": 60, "intent": "test"}),
            ("lock.release", {"paths": ["conformance.txt"]}),
            ("task.create", {"id": task_id, "title": "Conformance task"}),
            ("task.claim", {"id": task_id}),
            ("task.update", {"id": task_id, "status": "doing", "progress": 0.5}),
            ("task.done", {"id": task_id, "result": "ok"}),
            ("knowledge.contribution", {"kind": "finding", "title": "It conforms"}),
            ("decision.propose", {"id": "dec_" + os.urandom(3).hex(),
                                  "question": "ship?",
                                  "options": [{"key": "y", "label": "Yes"}]}),
        ]
        for etype, body in cases:
            with self.subTest(etype=etype):
                self.emit(etype, body)

    def test_an_unknown_type_outside_the_extension_space_is_422(self):
        status, _, payload = self.call("POST", "/v1/events",
                                       self.event("wibble.thing", {}))
        self.assertEqual(status, 422, payload)

    def test_an_event_body_over_256_kib_is_refused(self):
        status, _, payload = self.call("POST", "/v1/events",
                                       self.event("x.bulk", {"blob": "x" * (257 * 1024)}))
        self.assertIn(status, (413, 422), payload)

    def test_an_actor_that_is_not_the_caller_is_refused(self):
        event = self.event("chat.message", {"text": "forged"})
        event["actor"] = "agt_ffffffffffffffff"
        status, _, payload = self.call("POST", "/v1/events", event)
        self.assertGreaterEqual(status, 400, payload)

    def test_a_bad_path_is_422(self):
        status, _, payload = self.call("POST", "/v1/events", self.event(
            "file.put", {"path": "../escape.py", "hash": "sha256:" + "ab" * 32, "size": 1}
        ))
        self.assertEqual(status, 422, payload)

    def test_a_resend_of_the_same_event_id_returns_the_original_seq(self):
        """SPEC §5.2: idempotent by ``event.id`` within a 24 h window."""
        event_id = "evt_" + os.urandom(8).hex()
        event = self.event("chat.message", {"text": "exactly once"}, event_id=event_id)
        first_status, _, first = self.call("POST", "/v1/events", dict(event))
        self.assertIn(first_status, (200, 201), first)
        second_status, _, second = self.call("POST", "/v1/events", dict(event))
        self.assertIn(second_status, (200, 201, 409), second)
        matches = [e for e in self.log() if e.get("id") == event_id]
        self.assertEqual(len(matches), 1, "a resend must not produce a duplicate")


class TestForwardCompatibility(ConformanceTestCase):
    """SPEC §2.1 and §14 — the rule that lets the protocol grow."""

    def test_an_extension_event_is_accepted_stored_and_relayed(self):
        event_id = "evt_" + os.urandom(8).hex()
        self.emit("x.vendor.telemetry", {"samples": [1, 2, 3]}, event_id=event_id)
        stored = [e for e in self.log() if e.get("id") == event_id]
        self.assertEqual(len(stored), 1, "an x.* event MUST be stored and relayed")
        self.assertEqual(stored[0]["body"]["samples"], [1, 2, 3])

    def test_unknown_fields_inside_a_known_body_survive_untouched(self):
        event_id = "evt_" + os.urandom(8).hex()
        body = {"text": "hi", "x_future": {"nested": [1, {"deep": True}]},
                "unseen_field": "kept"}
        self.emit("chat.message", body, event_id=event_id)
        stored = [e for e in self.log() if e.get("id") == event_id][0]
        self.assertEqual(stored["body"]["x_future"], {"nested": [1, {"deep": True}]})
        self.assertEqual(stored["body"]["unseen_field"], "kept")

    def test_unicode_in_a_body_survives_a_round_trip(self):
        event_id = "evt_" + os.urandom(8).hex()
        text = "héllo ☃ — naïve 日本語"
        self.emit("chat.message", {"text": text}, event_id=event_id)
        stored = [e for e in self.log() if e.get("id") == event_id][0]
        self.assertEqual(stored["body"]["text"], text)


class TestStandingReport(ConformanceTestCase):
    """SPEC §6 — the PSR must be accepted and reflected in the snapshot."""

    def test_a_conforming_psr_is_accepted_and_shows_up_in_the_state(self):
        self.emit("status.update", {
            "state": "working",
            "headline": "Proving conformance",
            "detail": "Running tests/test_conformance.py",
            "focus": ["tests/test_conformance.py"],
            "progress": 0.5,
        })
        snapshot = wait_until(self._my_psr, timeout=10.0,
                              message="my PSR never appeared in /v1/state")
        self.assertEqual(snapshot["state"], "working")
        self.assertEqual(snapshot["headline"], "Proving conformance")

    def _my_psr(self):
        status, _, payload = self.call("GET", "/v1/state")
        if status != 200:
            return None
        for agent in payload.get("agents", []):
            if agent.get("agent_id") == self.agent_id and agent.get("psr"):
                return agent["psr"]
        return None

    def test_a_psr_without_a_headline_is_refused(self):
        status, _, payload = self.call("POST", "/v1/events",
                                       self.event("status.update", {"state": "working"}))
        self.assertEqual(status, 422, payload)

    def test_an_unknown_psr_state_does_not_take_the_hub_down(self):
        self.call("POST", "/v1/events", self.event(
            "status.update", {"state": "transcending", "headline": "Beyond"}))
        status, _, payload = self.call("GET", "/v1/hello")
        self.assertEqual(status, 200, payload)

    def test_the_state_snapshot_has_the_documented_top_level_shape(self):
        status, _, payload = self.call("GET", "/v1/state")
        self.assertEqual(status, 200, payload)
        for field in ("session", "head_seq", "agents", "tasks", "files"):
            self.assertIn(field, payload)


class TestConflictHandling(ConformanceTestCase):
    """SPEC §7.6 — the R5 guarantee, checked over the wire."""

    def upload(self, data):
        digest = hashlib.sha256(data).hexdigest()
        status, _, payload = self.signed(
            "POST", "/v1/blobs", data, self.agent_id, self.key,
            extra={"X-Parley-Blob-SHA256": digest,
                   "Content-Type": "application/octet-stream"},
        )
        self.assertIn(status, (200, 201), payload)
        return "sha256:" + digest

    def index(self):
        status, _, payload = self.call("GET", "/v1/index")
        self.assertEqual(status, 200, payload)
        files = payload.get("files", payload)
        if isinstance(files, list):
            return {f["path"]: f for f in files}
        return files

    def test_a_divergent_put_preserves_both_versions(self):
        path = "conformance/{0}.txt".format(os.urandom(4).hex())
        first = self.upload(b"version one\n")
        self.emit("file.put", {"path": path, "hash": first, "size": 12})
        second = self.upload(b"version two\n")
        self.emit("file.put", {"path": path, "hash": second, "size": 12})

        index = self.index()
        self.assertEqual(index[path]["hash"], second,
                         "SPEC §7.6: last-writer-wins at the path")
        sidecars = [p for p in index if p.startswith(path) and p != path]
        self.assertTrue(sidecars, "the displaced version MUST be preserved (R5)")
        self.assertIn(".parley-conflict-", sidecars[0])
        self.assertEqual(index[sidecars[0]]["hash"], first)

    def test_a_divergence_emits_a_file_conflict_event(self):
        path = "conformance/{0}.txt".format(os.urandom(4).hex())
        first = self.upload(b"alpha\n")
        self.emit("file.put", {"path": path, "hash": first, "size": 6})
        second = self.upload(b"beta\n")
        self.emit("file.put", {"path": path, "hash": second, "size": 5})
        conflicts = [e for e in self.log()
                     if e["type"] == "file.conflict" and e["body"]["path"] == path]
        self.assertEqual(len(conflicts), 1)
        body = conflicts[0]["body"]
        for field in ("path", "ours", "theirs", "kept_as"):
            self.assertIn(field, body)
        self.assertEqual(conflicts[0]["actor"], "hub",
                         "file.conflict is Hub-authored (SPEC §4.4)")

    def test_a_matching_base_is_accepted_without_a_conflict(self):
        path = "conformance/{0}.txt".format(os.urandom(4).hex())
        first = self.upload(b"one\n")
        self.emit("file.put", {"path": path, "hash": first, "size": 4})
        second = self.upload(b"two\n")
        self.emit("file.put", {"path": path, "hash": second, "size": 4, "base": first})
        conflicts = [e for e in self.log()
                     if e["type"] == "file.conflict" and e["body"]["path"] == path]
        self.assertEqual(conflicts, [])
        self.assertEqual(self.index()[path]["hash"], second)

    def test_a_blob_round_trips_byte_for_byte(self):
        data = bytes(range(256)) * 17
        blob_hash = self.upload(data)
        status, _, payload = self.signed("GET", "/v1/blobs/" + blob_hash, b"",
                                         self.agent_id, self.key)
        self.assertEqual(status, 200)
        self.assertEqual(payload, data)


class TestLiveFeed(ConformanceTestCase):
    """SPEC §5.1 — long-poll is the documented fallback and must work on its own."""

    def test_long_poll_returns_events_after_a_since_cursor(self):
        status, _, payload = self.call("GET", "/v1/events?since=0&limit=1")
        self.assertEqual(status, 200, payload)
        head = self.log()[-1]["seq"] if self.log() else 0
        self.emit("chat.message", {"text": "after the cursor"})
        fresh = self.log(since=head)
        self.assertTrue(fresh)
        self.assertTrue(all(e["seq"] > head for e in fresh))

    def test_a_long_poll_wait_is_capped_and_returns(self):
        started = time.monotonic()
        status, _, payload = self.call("GET", "/v1/events?since=999999999&wait=1")
        elapsed = time.monotonic() - started
        self.assertEqual(status, 200, payload)
        self.assertLess(elapsed, 30.0, "wait is capped at 30 s (SPEC §5)")

    def test_the_sse_endpoint_announces_itself_as_an_event_stream(self):
        """Headers only: a live SSE body never ends, so reading it would hang."""
        import urllib.request

        path = "/v1/stream?since=-1"
        stamp = str(int(time.time()))
        nonce = crypto.new_nonce_hex()
        sts = crypto.string_to_sign("GET", path, b"", stamp, nonce, SESSION, self.agent_id)
        request = urllib.request.Request(HUB_URL + path, method="GET")
        for name, value in (
            ("X-Parley-Version", "PARLEY/1"),
            ("X-Parley-Session", SESSION),
            ("X-Parley-Agent", self.agent_id),
            ("X-Parley-Timestamp", stamp),
            ("X-Parley-Nonce", nonce),
            ("Authorization", "Parley-HMAC-SHA256 " + crypto.sign(self.key, sts)),
        ):
            request.add_header(name, value)
        response = urllib.request.urlopen(request, timeout=10.0)
        try:
            self.assertEqual(response.getcode(), 200)
            headers = {k.lower(): v for k, v in dict(response.headers).items()}
            self.assertIn("text/event-stream", headers.get("content-type", ""))
            self.assertIn("no-store", headers.get("cache-control", ""),
                          "SPEC §5.1: the Hub MUST set Cache-Control: no-store")
            self.assertEqual(headers.get("x-accel-buffering"), "no",
                             "SPEC §5.1: the Hub MUST set X-Accel-Buffering: no")
        finally:
            response.close()


if __name__ == "__main__":
    unittest.main()
