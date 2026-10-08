"""SPEC §3, §5 and §12 — the Hub's HTTP surface, driven in process.

``api.handle`` is a pure-ish router, so none of this needs a socket: the tests call it
directly with crafted headers. That keeps them fast, deterministic and free of port
conflicts, and it means a failure points at the routing bug rather than at the network.

Every response body produced here is fed to ``helpers.SECRETS``, and
``TestNoSecretEverLeaks`` searches the whole collection at the end. SPEC §3.7 and §12 both
forbid the watchword, a key or a token appearing in a log line, an event body or an error
message, and that is only provable in aggregate.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from parley import crypto, ids, protocol
from parley.hub import api
from parley.hub.server import create_parley
from tests.helpers import SECRETS, decode_json


class HubAPITestCase(unittest.TestCase):
    REQUIRE_APPROVAL = False
    PUBLIC = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.hub, self.watchword = create_parley(
            self.workspace, name="api-test", port=0,
            public=self.PUBLIC, require_approval=self.REQUIRE_APPROVAL,
        )
        self.addCleanup(self._shutdown)
        self.session = self.hub.config.session
        self.root_key = crypto.derive_root_key(
            self.watchword, self.session,
            iterations=getattr(self.hub.config, "pbkdf2_iterations", 200000),
        )
        self.enroll_key = crypto.enroll_key(self.root_key)
        self.host_token = self.hub.config.host_token

        SECRETS.add_secret(self.watchword, "watchword")
        SECRETS.add_secret(crypto.normalise_watchword(self.watchword), "watchword")
        SECRETS.add_secret(self.root_key.hex(), "root_key")
        SECRETS.add_secret(self.enroll_key.hex(), "enroll_key")
        SECRETS.add_secret(self.host_token, "host_token")

    def _shutdown(self):
        try:
            self.hub.stop()
        except Exception:
            pass

    # ---------------------------------------------------------------- plumbing

    def call(self, method, path, body=None, *, agent="", key=None, timestamp=None,
             nonce=None, headers=None, sign=True):
        raw = b"" if body is None else (
            body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        )
        request_headers = {"Content-Type": "application/json; charset=utf-8"}
        if sign and key is not None:
            stamp = str(int(time.time()) if timestamp is None else int(timestamp))
            chosen_nonce = nonce or crypto.new_nonce_hex()
            sts = crypto.string_to_sign(method, path, raw, stamp, chosen_nonce,
                                        self.session, agent)
            request_headers.update({
                "X-Parley-Version": "PARLEY/1",
                "X-Parley-Session": self.session,
                "X-Parley-Agent": agent,
                "X-Parley-Timestamp": stamp,
                "X-Parley-Nonce": chosen_nonce,
                "Authorization": "Parley-HMAC-SHA256 " + crypto.sign(key, sts),
            })
        request_headers.update(headers or {})
        status, response_headers, response_body = api.handle(
            self.hub, method, path, request_headers, raw
        )
        parsed = decode_json(response_body)
        if status >= 400:
            SECRETS.record("{0} {1} -> {2}".format(method, path, status), response_body)
        return status, response_headers, parsed

    def enrol(self, name="Ada", kind="claude-code", capabilities=None):
        body = {"session": self.session, "agent": {
            "name": name, "kind": kind, "model": "claude-opus-5", "os": "linux",
            "host": "workbench", "client_version": "1.0.0",
            "capabilities": capabilities or ["chat", "sync", "tasks", "psr"],
        }}
        status, _, payload = self.call("POST", "/v1/enroll", body,
                                       agent="enroll", key=self.enroll_key)
        return status, payload

    def enrolled_agent(self, name="Ada"):
        status, payload = self.enrol(name=name)
        self.assertEqual(status, 201, payload)
        key = bytes.fromhex(payload["agent_key"])
        SECRETS.add_secret(payload["agent_key"], "agent_key")
        return payload["agent_id"], key, payload

    def event(self, actor, etype="chat.message", body=None, event_id=None):
        return protocol.make_event(actor, self.session, etype,
                                   {"text": "hello"} if body is None else body,
                                   event_id=event_id)

    def post_event(self, agent_id, key, event=None, **kwargs):
        event = event or self.event(agent_id)
        return self.call("POST", "/v1/events", event, agent=agent_id, key=key, **kwargs)

    def assert_error(self, status, payload, expected_status, *codes):
        self.assertEqual(status, expected_status, payload)
        self.assertIsInstance(payload, dict, payload)
        self.assertIn("error", payload)
        self.assertIn(payload["error"]["code"], codes, payload)
        for field in ("code", "message", "retryable"):
            self.assertIn(field, payload["error"])


class TestDiscovery(HubAPITestCase):
    def test_hello_needs_no_authentication(self):
        status, _, payload = self.call("GET", "/v1/hello", sign=False)
        self.assertEqual(status, 200)
        for field in ("v", "session", "fingerprint", "name", "agents_online",
                      "requires_seal", "server_time"):
            self.assertIn(field, payload)
        self.assertEqual(payload["v"], "PARLEY/1")

    def test_hello_never_leaks_the_watchword_or_the_roster_detail(self):
        _, _, payload = self.call("GET", "/v1/hello", sign=False)
        blob = json.dumps(payload)
        SECRETS.record("GET /v1/hello", blob)
        self.assertNotIn(self.watchword, blob)
        self.assertNotIn("agents", payload)
        self.assertNotIn("host_token", blob)

    def test_every_response_carries_the_documented_headers(self):
        status, headers, _ = self.call("GET", "/v1/hello", sign=False)
        lowered = {k.lower(): v for k, v in headers.items()}
        self.assertIn("x-parley-time", lowered)
        self.assertIn("x-parley-seq", lowered)
        self.assertEqual(status, 200)

    def test_an_unknown_route_is_a_clean_404_for_an_authenticated_caller(self):
        agent_id, key, _ = self.enrolled_agent()
        status, _, payload = self.call("GET", "/v1/nope", agent=agent_id, key=key)
        self.assertEqual(status, 404, payload)
        self.assertIn("error", payload or {})

    def test_an_unknown_route_does_not_reveal_itself_to_an_unauthenticated_caller(self):
        status, _, payload = self.call("GET", "/v1/nope", sign=False)
        self.assertIn(status, (401, 404), payload)
        self.assertIn("error", payload or {})


class TestEnrolment(HubAPITestCase):
    def test_a_correct_watchword_enrols(self):
        status, payload = self.enrol()
        self.assertEqual(status, 201, payload)
        self.assertTrue(payload["agent_id"].startswith("agt_"))
        self.assertEqual(len(payload["agent_key"]), 64)
        self.assertEqual(payload["session"], self.session)
        self.assertEqual(payload["fingerprint"], self.hub.config.fingerprint)
        self.assertIn("hub_time", payload)
        self.assertIn("seq", payload)
        for field in ("heartbeat_s", "psr_max_age_s", "max_blob_bytes", "sealed", "poll_ms"):
            self.assertIn(field, payload["policy"])

    def test_the_wrong_watchword_cannot_enrol(self):
        wrong = crypto.enroll_key(crypto.derive_root_key("wrong-words-entirely",
                                                         self.session, iterations=1000))
        body = {"session": self.session, "agent": {"name": "Mallory", "kind": "x",
                                                   "os": "linux", "client_version": "1",
                                                   "capabilities": []}}
        status, _, payload = self.call("POST", "/v1/enroll", body, agent="enroll", key=wrong)
        self.assert_error(status, payload, 401, "bad_signature")

    def test_each_enrolment_mints_a_distinct_agent_and_key(self):
        first_id, first_key, _ = self.enrolled_agent("Ada")
        second_id, second_key, _ = self.enrolled_agent("Bob")
        self.assertNotEqual(first_id, second_id)
        self.assertNotEqual(first_key, second_key)

    def test_the_agent_key_does_not_derive_from_the_watchword(self):
        """SPEC §3.8: rotating the watchword must not kick existing agents out."""
        _, key, _ = self.enrolled_agent()
        self.assertNotEqual(key, self.enroll_key)
        self.assertNotEqual(key, self.root_key)
        self.assertNotIn(key.hex(), self.root_key.hex())

    def test_enrolment_emits_an_agent_hello(self):
        agent_id, _, _ = self.enrolled_agent()
        hellos = [e for e in self.hub.store.read(since=0, limit=1000)
                  if e["type"] == "agent.hello" and e["body"].get("name") == "Ada"]
        self.assertTrue(hellos)
        self.assertEqual(hellos[-1]["actor"], agent_id)

    def test_a_session_mismatch_is_refused(self):
        body = {"session": "ses_0000000000000000",
                "agent": {"name": "X", "kind": "x", "os": "linux",
                          "client_version": "1", "capabilities": []}}
        status, _, payload = self.call("POST", "/v1/enroll", body,
                                       agent="enroll", key=self.enroll_key)
        self.assertGreaterEqual(status, 400)
        self.assertIn("error", payload or {})


class TestEnrolmentPolicy(HubAPITestCase):
    """SPEC §3.4's policy table."""

    def test_enroll_open_false_closes_the_door(self):
        self.hub.policy["enroll_open"] = False
        status, payload = self.enrol()
        self.assert_error(status, payload, 403, "enroll_closed")

    def test_enroll_ttl_expires_the_watchword(self):
        self.hub.policy["enroll_ttl_s"] = 3600
        self.hub.store.set_meta("enroll_opened_at", repr(time.time() - 7200))
        status, payload = self.enrol()
        self.assert_error(status, payload, 403, "enroll_closed")

    def test_enroll_ttl_still_inside_the_window_is_fine(self):
        self.hub.policy["enroll_ttl_s"] = 3600
        self.hub.store.set_meta("enroll_opened_at", repr(time.time() - 60))
        status, payload = self.enrol()
        self.assertEqual(status, 201, payload)

    def test_enroll_ttl_of_zero_never_expires(self):
        self.hub.policy["enroll_ttl_s"] = 0
        self.hub.store.set_meta("enroll_opened_at", repr(time.time() - 10 ** 7))
        status, payload = self.enrol()
        self.assertEqual(status, 201, payload)

    def test_enroll_max_uses_is_enforced(self):
        self.hub.policy["enroll_max_uses"] = 2
        self.assertEqual(self.enrol("One")[0], 201)
        self.assertEqual(self.enrol("Two")[0], 201)
        status, payload = self.enrol("Three")
        self.assert_error(status, payload, 403, "enroll_closed")

    def test_enroll_max_uses_of_zero_is_unlimited(self):
        self.hub.policy["enroll_max_uses"] = 0
        for index in range(5):
            self.assertEqual(self.enrol("A{0}".format(index))[0], 201)

    def test_max_agents_is_a_hard_cap(self):
        self.hub.policy["max_agents"] = 3
        for index in range(3):
            self.assertEqual(self.enrol("A{0}".format(index))[0], 201)
        status, payload = self.enrol("TooMany")
        self.assert_error(status, payload, 403, "enroll_closed")

    def test_the_lan_defaults_match_the_spec_table(self):
        self.assertEqual(self.hub.policy["enroll_ttl_s"], 0)
        self.assertEqual(self.hub.policy["enroll_max_uses"], 0)
        self.assertFalse(self.hub.policy["require_approval"])
        self.assertEqual(self.hub.policy["max_agents"], 16)


class TestPublicDefaults(HubAPITestCase):
    PUBLIC = True

    def test_the_public_defaults_match_the_spec_table(self):
        self.assertEqual(self.hub.policy["enroll_ttl_s"], 3600)
        self.assertEqual(self.hub.policy["enroll_max_uses"], 8)
        self.assertTrue(self.hub.policy["require_approval"])


class TestPendingApproval(HubAPITestCase):
    REQUIRE_APPROVAL = True

    def test_a_new_agent_lands_in_pending(self):
        agent_id, _, _ = self.enrolled_agent()
        self.assertEqual(self.hub.store.get_agent(agent_id)["status"], "pending")

    def test_a_pending_agent_may_not_write(self):
        agent_id, key, _ = self.enrolled_agent()
        status, _, payload = self.post_event(agent_id, key)
        self.assert_error(status, payload, 403, "pending_approval")

    def test_a_pending_agent_may_read_nothing_but_its_own_status(self):
        agent_id, key, _ = self.enrolled_agent()
        status, _, payload = self.call("GET", "/v1/state", agent=agent_id, key=key)
        self.assert_error(status, payload, 403, "pending_approval")

    def test_the_host_can_approve_and_then_writes_succeed(self):
        agent_id, key, _ = self.enrolled_agent()
        status, _, payload = self.call(
            "POST", "/v1/admin/approve", {"agent_id": agent_id}, sign=False,
            headers={"Authorization": "Parley-Host " + self.host_token},
        )
        self.assertIn(status, (200, 201, 204), payload)
        self.assertEqual(self.hub.store.get_agent(agent_id)["status"], "active")
        status, _, payload = self.post_event(agent_id, key)
        self.assertIn(status, (200, 201), payload)


class TestRequestAuthentication(HubAPITestCase):
    """SPEC §3.3."""

    def setUp(self):
        super().setUp()
        self.agent_id, self.key, _ = self.enrolled_agent()

    def test_a_correctly_signed_write_is_accepted(self):
        status, _, payload = self.post_event(self.agent_id, self.key)
        self.assertIn(status, (200, 201), payload)

    def test_a_bad_signature_is_refused(self):
        status, _, payload = self.post_event(self.agent_id, b"\x00" * 32)
        self.assert_error(status, payload, 401, "bad_signature")

    def test_a_missing_authorization_header_is_refused(self):
        status, _, payload = self.call("POST", "/v1/events", self.event(self.agent_id),
                                       sign=False)
        self.assertEqual(status, 401, payload)

    def test_a_signature_over_a_different_body_is_refused(self):
        event = self.event(self.agent_id)
        raw = json.dumps(event).encode("utf-8")
        stamp = str(int(time.time()))
        nonce = crypto.new_nonce_hex()
        sts = crypto.string_to_sign("POST", "/v1/events", raw, stamp, nonce,
                                    self.session, self.agent_id)
        headers = {
            "X-Parley-Version": "PARLEY/1", "X-Parley-Session": self.session,
            "X-Parley-Agent": self.agent_id, "X-Parley-Timestamp": stamp,
            "X-Parley-Nonce": nonce,
            "Authorization": "Parley-HMAC-SHA256 " + crypto.sign(self.key, sts),
        }
        tampered = json.dumps(self.event(self.agent_id, body={"text": "tampered"}))
        status, _, payload = self.call("POST", "/v1/events", tampered.encode("utf-8"),
                                       sign=False, headers=headers)
        self.assert_error(status, payload, 401, "bad_signature")

    def test_a_signature_over_a_different_path_is_refused(self):
        raw = b""
        stamp = str(int(time.time()))
        nonce = crypto.new_nonce_hex()
        sts = crypto.string_to_sign("GET", "/v1/state", raw, stamp, nonce,
                                    self.session, self.agent_id)
        headers = {
            "X-Parley-Version": "PARLEY/1", "X-Parley-Session": self.session,
            "X-Parley-Agent": self.agent_id, "X-Parley-Timestamp": stamp,
            "X-Parley-Nonce": nonce,
            "Authorization": "Parley-HMAC-SHA256 " + crypto.sign(self.key, sts),
        }
        status, _, payload = self.call("GET", "/v1/index", sign=False, headers=headers)
        self.assert_error(status, payload, 401, "bad_signature")

    def test_a_timestamp_beyond_the_skew_window_is_refused(self):
        for offset in (-301, -3600, 301, 86400):
            with self.subTest(offset=offset):
                status, _, payload = self.post_event(
                    self.agent_id, self.key, timestamp=time.time() + offset
                )
                self.assert_error(status, payload, 401, "stale_timestamp")

    def test_a_timestamp_inside_the_skew_window_is_accepted(self):
        for offset in (-299, -60, 0, 60, 299):
            with self.subTest(offset=offset):
                status, _, payload = self.post_event(
                    self.agent_id, self.key, timestamp=time.time() + offset,
                    event=self.event(self.agent_id, body={"text": str(offset)}),
                )
                self.assertIn(status, (200, 201), payload)

    def test_a_non_numeric_timestamp_is_refused(self):
        status, _, payload = self.call(
            "POST", "/v1/events", self.event(self.agent_id), sign=False,
            headers={"X-Parley-Version": "PARLEY/1", "X-Parley-Session": self.session,
                     "X-Parley-Agent": self.agent_id, "X-Parley-Timestamp": "soon",
                     "X-Parley-Nonce": crypto.new_nonce_hex(),
                     "Authorization": "Parley-HMAC-SHA256 " + "0" * 64},
        )
        self.assertIn(status, (400, 401), payload)
        self.assertIn(payload["error"]["code"],
                      ("bad_request", "stale_timestamp", "bad_signature"), payload)

    def test_a_replayed_nonce_is_refused(self):
        nonce = crypto.new_nonce_hex()
        stamp = int(time.time())
        first = self.post_event(self.agent_id, self.key, nonce=nonce, timestamp=stamp,
                                event=self.event(self.agent_id, event_id=ids.new_event_id()))
        self.assertIn(first[0], (200, 201), first[2])
        status, _, payload = self.post_event(
            self.agent_id, self.key, nonce=nonce, timestamp=stamp,
            event=self.event(self.agent_id, event_id=ids.new_event_id()),
        )
        self.assert_error(status, payload, 401, "replayed_nonce")

    def test_a_fresh_nonce_each_time_is_fine(self):
        for index in range(5):
            status, _, payload = self.post_event(
                self.agent_id, self.key,
                event=self.event(self.agent_id, body={"text": str(index)}),
            )
            self.assertIn(status, (200, 201), payload)

    def test_an_unknown_agent_is_refused(self):
        status, _, payload = self.call("GET", "/v1/state",
                                       agent="agt_ffffffffffffffff", key=self.key)
        self.assert_error(status, payload, 401, "unknown_agent")

    def test_a_revoked_agent_is_refused(self):
        self.hub.store.set_agent_status(self.agent_id, "revoked")
        status, _, payload = self.post_event(self.agent_id, self.key)
        self.assert_error(status, payload, 403, "revoked")

    def test_authenticate_reports_the_caller_kind(self):
        raw = b""
        stamp = str(int(time.time()))
        nonce = crypto.new_nonce_hex()
        sts = crypto.string_to_sign("GET", "/v1/state", raw, stamp, nonce,
                                    self.session, self.agent_id)
        auth = api.authenticate(
            self.hub.store, self.hub.config, "GET", "/v1/state",
            {"X-Parley-Version": "PARLEY/1", "X-Parley-Session": self.session,
             "X-Parley-Agent": self.agent_id, "X-Parley-Timestamp": stamp,
             "X-Parley-Nonce": nonce,
             "Authorization": "Parley-HMAC-SHA256 " + crypto.sign(self.key, sts)},
            raw,
        )
        self.assertEqual(auth["kind"], "agent")
        self.assertEqual(auth["agent"]["agent_id"], self.agent_id)


class TestEventIngestion(HubAPITestCase):
    def setUp(self):
        super().setUp()
        self.agent_id, self.key, _ = self.enrolled_agent()

    def test_an_event_gets_a_seq(self):
        status, _, payload = self.post_event(self.agent_id, self.key)
        self.assertIn(status, (200, 201), payload)
        self.assertTrue(json.dumps(payload).find("seq") >= 0, payload)

    def test_an_actor_that_is_not_the_authenticated_agent_is_refused(self):
        """SPEC §2: the Hub MUST reject an event whose actor is not the caller."""
        foreign = self.event("agt_ffffffffffffffff")
        status, _, payload = self.call("POST", "/v1/events", foreign,
                                       agent=self.agent_id, key=self.key)
        self.assertGreaterEqual(status, 400, payload)

    def test_an_agent_cannot_forge_a_hub_authored_event(self):
        forged = self.event("hub", etype="hub.notice", body={"text": "trust me"})
        status, _, payload = self.call("POST", "/v1/events", forged,
                                       agent=self.agent_id, key=self.key)
        self.assertGreaterEqual(status, 400, payload)

    def test_an_unknown_type_outside_the_extension_space_is_422(self):
        event = self.event(self.agent_id, etype="wibble.thing", body={})
        status, _, payload = self.call("POST", "/v1/events", event,
                                       agent=self.agent_id, key=self.key)
        self.assert_error(status, payload, 422, "unknown_type", "bad_event")

    def test_an_extension_type_is_accepted_and_stored(self):
        """SPEC §2.1: x.* MUST be accepted, stored and relayed."""
        event = self.event(self.agent_id, etype="x.vendor.thing",
                           body={"anything": [1, 2], "x_nested": {"ok": True}})
        status, _, payload = self.call("POST", "/v1/events", event,
                                       agent=self.agent_id, key=self.key)
        self.assertIn(status, (200, 201), payload)
        stored = [e for e in self.hub.store.read(since=0, limit=1000)
                  if e["type"] == "x.vendor.thing"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["body"]["x_nested"], {"ok": True})

    def test_an_unknown_field_in_a_known_body_is_preserved(self):
        event = self.event(self.agent_id, body={"text": "hi", "x_future": {"deep": [1]}})
        self.call("POST", "/v1/events", event, agent=self.agent_id, key=self.key)
        stored = [e for e in self.hub.store.read(since=0, limit=1000)
                  if e["type"] == "chat.message"][-1]
        self.assertEqual(stored["body"]["x_future"], {"deep": [1]})

    def test_a_body_over_256_kib_is_rejected(self):
        event = self.event(self.agent_id, etype="x.bulk",
                           body={"blob": "x" * (256 * 1024 + 100)})
        status, _, payload = self.call("POST", "/v1/events", event,
                                       agent=self.agent_id, key=self.key)
        self.assertIn(status, (413, 422), payload)

    def test_a_bad_path_in_a_file_event_is_422_bad_path(self):
        event = self.event(self.agent_id, etype="file.put",
                           body={"path": "../escape.py", "hash": "sha256:" + "ab" * 32,
                                 "size": 1})
        status, _, payload = self.call("POST", "/v1/events", event,
                                       agent=self.agent_id, key=self.key)
        self.assert_error(status, payload, 422, "bad_path", "bad_event")

    def test_a_batch_is_accepted(self):
        batch = {"events": [self.event(self.agent_id, body={"text": str(i)})
                            for i in range(10)]}
        status, _, payload = self.call("POST", "/v1/events", batch,
                                       agent=self.agent_id, key=self.key)
        self.assertIn(status, (200, 201), payload)
        self.assertEqual(len([e for e in self.hub.store.read(since=0, limit=1000)
                              if e["type"] == "chat.message"]), 10)

    def test_a_batch_over_sixty_four_is_refused(self):
        batch = {"events": [self.event(self.agent_id, body={"text": str(i)})
                            for i in range(65)]}
        status, _, payload = self.call("POST", "/v1/events", batch,
                                       agent=self.agent_id, key=self.key)
        self.assertGreaterEqual(status, 400, payload)

    def test_malformed_json_is_a_clean_400(self):
        status, _, payload = self.call("POST", "/v1/events", b"{not json",
                                       agent=self.agent_id, key=self.key)
        self.assert_error(status, payload, 400, "bad_json", "bad_request")

    def test_a_resend_of_the_same_event_id_does_not_duplicate(self):
        """SPEC §5.2: deduplicate on (actor, id), returning the original seq."""
        event = self.event(self.agent_id, event_id="evt_0123456789abcdef")
        first_status, _, first = self.call("POST", "/v1/events", dict(event),
                                           agent=self.agent_id, key=self.key)
        self.assertIn(first_status, (200, 201), first)
        head_after_first = self.hub.store.head_seq()
        status, _, payload = self.call("POST", "/v1/events", dict(event),
                                       agent=self.agent_id, key=self.key)
        self.assertEqual(self.hub.store.head_seq(), head_after_first,
                         "the resend must not append a second event")
        self.assertIn(status, (200, 201, 409), payload)
        self.assertIn(str(head_after_first), json.dumps(payload),
                      "the original seq must come back to the caller")

    def test_a_clock_skew_correction_is_flagged_in_the_body(self):
        """SPEC §2: a ts more than 300 s out is rewritten and flagged."""
        event = self.event(self.agent_id)
        event["ts"] = "2001-01-01T00:00:00.000Z"
        status, _, payload = self.call("POST", "/v1/events", event,
                                       agent=self.agent_id, key=self.key)
        self.assertIn(status, (200, 201), payload)
        stored = [e for e in self.hub.store.read(since=0, limit=1000)
                  if e["type"] == "chat.message"][-1]
        self.assertTrue(stored["body"].get("_clock_skew_corrected"))
        self.assertNotEqual(stored["ts"], "2001-01-01T00:00:00.000Z")


class TestBlobs(HubAPITestCase):
    def setUp(self):
        super().setUp()
        self.agent_id, self.key, _ = self.enrolled_agent()

    def test_a_blob_round_trips(self):
        data = b"hello blob" * 100
        digest = hashlib.sha256(data).hexdigest()
        status, _, _ = self.call("POST", "/v1/blobs", data, agent=self.agent_id,
                                 key=self.key,
                                 headers={"X-Parley-Blob-SHA256": digest,
                                          "Content-Type": "application/octet-stream"})
        self.assertIn(status, (200, 201), status)
        self.assertTrue(self.hub.store.has_blob("sha256:" + digest))

    def test_a_blob_whose_body_does_not_match_its_hash_is_refused(self):
        digest = hashlib.sha256(b"honest").hexdigest()
        status, _, payload = self.call("POST", "/v1/blobs", b"tampered",
                                       agent=self.agent_id, key=self.key,
                                       headers={"X-Parley-Blob-SHA256": digest,
                                                "Content-Type": "application/octet-stream"})
        self.assertGreaterEqual(status, 400, payload)
        self.assertFalse(self.hub.store.has_blob("sha256:" + digest))

    def test_an_absent_blob_is_404(self):
        status, _, payload = self.call("GET", "/v1/blobs/sha256:" + "00" * 32,
                                       agent=self.agent_id, key=self.key)
        self.assertEqual(status, 404, payload)

    def test_a_blob_over_the_policy_limit_is_413(self):
        self.hub.policy["max_blob_bytes"] = 1024
        data = b"x" * 4096
        digest = hashlib.sha256(data).hexdigest()
        status, _, payload = self.call("POST", "/v1/blobs", data, agent=self.agent_id,
                                       key=self.key,
                                       headers={"X-Parley-Blob-SHA256": digest,
                                                "Content-Type": "application/octet-stream"})
        self.assertEqual(status, 413, payload)


class TestViewerTokens(HubAPITestCase):
    """SPEC §3.7 — read-only, no blobs, no writes, no watchword."""

    def setUp(self):
        super().setUp()
        self.agent_id, self.key, _ = self.enrolled_agent()
        self.viewer = self.hub.mint_viewer_token(label="test")
        SECRETS.add_secret(self.viewer, "viewer_token")

    def test_a_viewer_token_can_read_the_state_snapshot(self):
        status, _, payload = self.call("GET", "/v1/state?vt=" + self.viewer, sign=False)
        self.assertEqual(status, 200, payload)
        self.assertIn("agents", payload)
        self.assertIn("ledger", payload)

    def test_a_viewer_token_cannot_write(self):
        status, _, payload = self.call("POST", "/v1/events?vt=" + self.viewer,
                                       self.event(self.agent_id), sign=False)
        self.assert_error(status, payload, 403, "read_only_token")

    def test_a_viewer_token_cannot_fetch_blob_content(self):
        data = b"secret file contents"
        self.hub.store.put_blob("sha256:" + hashlib.sha256(data).hexdigest(), data)
        status, _, payload = self.call(
            "GET", "/v1/blobs/sha256:{0}?vt={1}".format(
                hashlib.sha256(data).hexdigest(), self.viewer),
            sign=False,
        )
        self.assertIn(status, (401, 403), payload)

    def test_a_viewer_token_cannot_use_the_admin_endpoints(self):
        status, _, payload = self.call("POST", "/v1/admin/revoke?vt=" + self.viewer,
                                       {"agent_id": self.agent_id}, sign=False)
        self.assertIn(status, (401, 403), payload)

    def test_an_unknown_viewer_token_is_refused(self):
        status, _, payload = self.call("GET", "/v1/state?vt=vwr_" + "0" * 32, sign=False)
        self.assertIn(status, (401, 403), payload)

    def test_a_revoked_viewer_token_stops_working_immediately(self):
        self.hub.store.revoke_viewer_token(self.viewer)
        status, _, payload = self.call("GET", "/v1/state?vt=" + self.viewer, sign=False)
        self.assertIn(status, (401, 403), payload)

    def test_the_viewer_state_snapshot_never_contains_the_watchword_or_keys(self):
        _, _, payload = self.call("GET", "/v1/state?vt=" + self.viewer, sign=False)
        blob = json.dumps(payload)
        SECRETS.record("GET /v1/state (viewer)", blob)
        self.assertNotIn(self.watchword, blob)
        self.assertNotIn(self.host_token, blob)
        self.assertNotIn("key_hex", blob)


class TestHostTokenEndpoints(HubAPITestCase):
    def setUp(self):
        super().setUp()
        self.agent_id, self.key, _ = self.enrolled_agent()

    def host(self, path, body=None):
        return self.call("POST", path, body or {}, sign=False,
                         headers={"Authorization": "Parley-Host " + self.host_token})

    ADMIN_PATHS = ("/v1/admin/approve", "/v1/admin/revoke",
                   "/v1/admin/rotate-watchword", "/v1/admin/viewer-token")

    def test_admin_with_no_credentials_at_all_is_refused(self):
        for path in self.ADMIN_PATHS:
            with self.subTest(path=path):
                status, _, payload = self.call("POST", path, {"agent_id": self.agent_id},
                                               sign=False)
                self.assertIn(status, (401, 403), payload)
                self.assertIn(payload["error"]["code"],
                              ("host_token_required", "unknown_agent", "bad_signature"),
                              payload)

    def test_an_agent_key_does_not_unlock_the_admin_endpoints(self):
        """SPEC §12: an authenticated non-host caller gets 403 host_token_required."""
        for path in self.ADMIN_PATHS:
            with self.subTest(path=path):
                status, _, payload = self.call("POST", path, {"agent_id": self.agent_id},
                                               agent=self.agent_id, key=self.key)
                self.assert_error(status, payload, 403, "host_token_required")

    def test_a_wrong_host_token_is_refused(self):
        status, _, payload = self.call("POST", "/v1/admin/viewer-token", {}, sign=False,
                                       headers={"Authorization": "Parley-Host hst_" + "0" * 32})
        self.assertIn(status, (401, 403), payload)

    def test_the_host_can_revoke_an_agent(self):
        status, _, payload = self.host("/v1/admin/revoke", {"agent_id": self.agent_id})
        self.assertIn(status, (200, 201, 204), payload)
        self.assertEqual(self.hub.store.get_agent(self.agent_id)["status"], "revoked")
        status, _, payload = self.post_event(self.agent_id, self.key)
        self.assert_error(status, payload, 403, "revoked")

    def test_revocation_emits_an_agent_revoked_event(self):
        self.host("/v1/admin/revoke", {"agent_id": self.agent_id})
        revoked = [e for e in self.hub.store.read(since=0, limit=1000)
                   if e["type"] == "agent.revoked"]
        self.assertTrue(revoked)
        self.assertEqual(revoked[-1]["body"]["agent_id"], self.agent_id)
        self.assertTrue(protocol.is_hub_authored(revoked[-1]))

    def test_rotating_the_watchword_leaves_existing_agent_keys_working(self):
        """SPEC §3.8: this is exactly why the two-tier key design exists."""
        status, _, payload = self.host("/v1/admin/rotate-watchword", {})
        self.assertIn(status, (200, 201), payload)
        status, _, payload = self.post_event(self.agent_id, self.key)
        self.assertIn(status, (200, 201), payload)

    def test_rotating_the_watchword_invalidates_the_old_enrol_key(self):
        self.host("/v1/admin/rotate-watchword", {})
        status, payload = self.enrol("Latecomer")
        self.assertGreaterEqual(status, 400, payload)

    def test_the_host_can_mint_a_viewer_token(self):
        status, _, payload = self.host("/v1/admin/viewer-token", {})
        self.assertIn(status, (200, 201), payload)
        token = json.dumps(payload)
        self.assertIn("vwr_", token)


class TestRateLimiting(HubAPITestCase):
    """SPEC §12.1 — 60 events/minute, burst 120, 429 with Retry-After."""

    def setUp(self):
        super().setUp()
        self.agent_id, self.key, _ = self.enrolled_agent()

    def test_a_flood_of_events_is_eventually_rate_limited(self):
        limited = None
        for index in range(400):
            status, headers, payload = self.post_event(
                self.agent_id, self.key,
                event=self.event(self.agent_id, body={"text": str(index)}),
            )
            if status == 429:
                limited = (status, headers, payload, index)
                break
        self.assertIsNotNone(limited, "400 events in a burst must trip the limiter")
        status, headers, payload, index = limited
        self.assert_error(status, payload, 429, "rate_limited")
        self.assertTrue(payload["error"].get("retryable"))
        lowered = {k.lower(): v for k, v in headers.items()}
        self.assertIn("retry-after", lowered,
                      "SPEC §12.1: 429 MUST carry Retry-After")
        self.assertGreater(float(lowered["retry-after"]), 0)
        self.assertGreaterEqual(index, 60,
                                "the burst allowance must let honest traffic through")

    def test_the_burst_allowance_lets_a_normal_session_through(self):
        for index in range(50):
            status, _, payload = self.post_event(
                self.agent_id, self.key,
                event=self.event(self.agent_id, body={"text": str(index)}),
            )
            self.assertIn(status, (200, 201), payload)

    def test_the_token_bucket_refills_over_time(self):
        from parley.hub.ratelimit import TokenBucket

        bucket = TokenBucket(60.0, 120.0)
        now = 1000.0
        for _ in range(120):
            self.assertEqual(bucket.take(now), 0.0)
        self.assertGreater(bucket.take(now), 0.0, "the burst must run out")
        self.assertEqual(bucket.take(now + 120.0), 0.0, "and refill with time")

    def test_the_token_bucket_reports_a_usable_retry_after(self):
        from parley.hub.ratelimit import TokenBucket

        bucket = TokenBucket(60.0, 1.0)
        self.assertEqual(bucket.take(0.0), 0.0)
        wait = bucket.take(0.0)
        self.assertGreater(wait, 0.0)
        self.assertEqual(bucket.take(wait + 0.001), 0.0,
                         "waiting the advertised time must actually work")


class TestErrorShape(HubAPITestCase):
    """SPEC §12."""

    def test_every_error_body_has_the_documented_shape(self):
        agent_id, key, _ = self.enrolled_agent()
        cases = [
            ("POST", "/v1/events", self.event(agent_id), {"key": b"\x00" * 32}),
            ("GET", "/v1/state", None, {"agent": "agt_ffffffffffffffff", "key": key}),
            ("GET", "/v1/nope", None, {"key": key}),
        ]
        for method, path, body, kwargs in cases:
            with self.subTest(path=path):
                kwargs.setdefault("agent", agent_id)
                status, _, payload = self.call(method, path, body, **kwargs)
                self.assertGreaterEqual(status, 400)
                self.assertIn("error", payload, payload)
                error = payload["error"]
                self.assertIsInstance(error.get("code"), str)
                self.assertIsInstance(error.get("message"), str)
                self.assertIsInstance(error.get("retryable"), bool)

    def test_the_hint_field_tells_a_caller_what_to_do(self):
        status, _, payload = self.call("GET", "/v1/state", agent="agt_ffffffffffffffff",
                                       key=b"\x00" * 32)
        self.assertGreaterEqual(status, 400)
        self.assertIn("hint", payload["error"])


class TestNoSecretEverLeaks(unittest.TestCase):
    """SPEC §3.7 and §12: the watchword, a key or a token must never appear in output.

    This runs over everything every other case in this module recorded, which is why it is
    worth having as its own test rather than an assertion sprinkled through each one.
    """

    def test_nothing_the_hub_emitted_contained_secret_material(self):
        if not SECRETS.secrets:
            self.skipTest("no secrets were registered; the Hub suite did not run")
        violations = SECRETS.violations()
        self.assertEqual(
            violations, [],
            "secret material leaked into Hub output:\n" + "\n".join(
                "  {0} leaked {1}: {2}".format(where, label, sample)
                for where, label, sample in violations
            ),
        )

    def test_the_watchword_is_never_persisted_in_plain_form(self):
        """SPEC §3.7/§11: the Hub stores a hash and the derived root key, never the words."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            hub, watchword = create_parley(workspace, name="leak-test", port=0)
            try:
                for path in workspace.rglob("*"):
                    if not path.is_file():
                        continue
                    blob = path.read_bytes()
                    self.assertNotIn(watchword.encode("utf-8"), blob,
                                     "{0} contains the watchword".format(path))
                    self.assertNotIn(
                        crypto.normalise_watchword(watchword).encode("utf-8"), blob,
                        "{0} contains the normalised watchword".format(path),
                    )
            finally:
                hub.stop()


if __name__ == "__main__":
    unittest.main()
