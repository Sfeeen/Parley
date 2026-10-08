"""SPEC §3.6 — sealed mode, pinned byte for byte.

Sealed mode failed in the field for a reason worth remembering: §3.6 used to say
the AEAD's AAD was "the string_to_sign of §3.3", and §3.3's string contains
``sha256(raw_request_body)`` — which in sealed mode *is the AEAD's own output*.
The definition was circular, so the Hub and the client each invented a different
non-circular reading, each "fixed" it by trying several AADs and taking whichever
authenticated, and the result was a 400 that no amount of reading either side's
code could explain.

The spec now defines the AAD outright, and this module is where that definition
is nailed down:

* the AAD bytes are compared against **hand-written literals**, not against a
  second call into the implementation — a test that asks the code what the code
  does proves nothing;
* a wrong AAD, a wrong key and a wrong direction must each be *rejected*, never
  probed around — §3.6 forbids trial decryption, because it turns a loud
  interoperability failure into a silent one;
* the enrolment request gets its own tests, because it is the one request sealed
  under one key (``seal_key``) and signed under another (``enroll_key``) with the
  literal agent id ``"enroll"``, and it is where the old bug bit first.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from parley import crypto, protocol
from parley.client.transport import Transport
from parley.errors import BadSeal, CryptoFailure
from parley.hub import api
from parley.hub.server import create_parley

KEY_A = bytes(range(32))
KEY_B = bytes((b + 7) % 256 for b in range(32))

#: One request's identity, reused by every AAD test so the literals below can be
#: read against it.
TS = "1791456896"
NONCE = "3f8a1c04bb9e7d62"
SESSION = "ses_9f2c41ab77e0d315"
AGENT = "agt_0c5518aa91be7742"
PATH = "/v1/events?since=10"

#: Hand-written, not generated. SPEC §3.6: prefix, METHOD, path-with-query,
#: timestamp, nonce, session, agent — newline-joined, UTF-8, no trailing newline.
REQUEST_AAD = (
    b"PARLEY/1-SEAL\n"
    b"POST\n"
    b"/v1/events?since=10\n"
    b"1791456896\n"
    b"3f8a1c04bb9e7d62\n"
    b"ses_9f2c41ab77e0d315\n"
    b"agt_0c5518aa91be7742"
)

#: The response form: the *same* timestamp and nonce — they belong to the
#: request — under the ``-RESPONSE`` prefix.
RESPONSE_AAD = (
    b"PARLEY/1-SEAL-RESPONSE\n"
    b"POST\n"
    b"/v1/events?since=10\n"
    b"1791456896\n"
    b"3f8a1c04bb9e7d62\n"
    b"ses_9f2c41ab77e0d315\n"
    b"agt_0c5518aa91be7742"
)

#: The enrolment request: agent id is the literal "enroll" on both sides.
ENROL_AAD = (
    b"PARLEY/1-SEAL\n"
    b"POST\n"
    b"/v1/enroll\n"
    b"1791456896\n"
    b"3f8a1c04bb9e7d62\n"
    b"ses_9f2c41ab77e0d315\n"
    b"enroll"
)


# --------------------------------------------------------------------------- #
# The AAD itself
# --------------------------------------------------------------------------- #


class TestSealAADBytes(unittest.TestCase):
    """The one thing both implementations must agree on to the byte."""

    def test_the_request_aad_is_exactly_these_bytes(self):
        self.assertEqual(
            crypto.seal_aad("POST", PATH, TS, NONCE, SESSION, AGENT),
            REQUEST_AAD,
        )

    def test_the_response_aad_is_exactly_these_bytes(self):
        self.assertEqual(
            crypto.seal_aad("POST", PATH, TS, NONCE, SESSION, AGENT, response=True),
            RESPONSE_AAD,
        )

    def test_the_enrolment_aad_uses_the_literal_enroll_agent_id(self):
        """The hard case: sealed under seal_key, signed under enroll_key."""
        self.assertEqual(
            crypto.seal_aad("POST", "/v1/enroll", TS, NONCE, SESSION, "enroll"),
            ENROL_AAD,
        )

    def test_the_two_directions_differ_only_in_the_prefix(self):
        self.assertEqual(REQUEST_AAD.split(b"\n", 1)[1], RESPONSE_AAD.split(b"\n", 1)[1])
        self.assertNotEqual(REQUEST_AAD, RESPONSE_AAD)

    def test_the_aad_has_seven_fields_and_no_trailing_newline(self):
        aad = crypto.seal_aad("POST", PATH, TS, NONCE, SESSION, AGENT)
        self.assertEqual(aad.count(b"\n"), 6)
        self.assertFalse(aad.endswith(b"\n"))

    def test_the_aad_contains_no_body_hash(self):
        """The whole point of the rewrite: nothing here depends on the body.

        If a body hash crept back in, the definition would be circular again —
        the sealed body is the output of the AEAD this AAD feeds.
        """
        aad = crypto.seal_aad("POST", PATH, TS, NONCE, SESSION, AGENT)
        empty_hash = b"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        self.assertNotIn(empty_hash, aad)
        sts = crypto.string_to_sign("POST", PATH, b"", TS, NONCE, SESSION, AGENT)
        self.assertNotEqual(aad, sts)
        self.assertEqual(sts.count(b"\n"), 7)  # §3.3 has one line more: the hash

    def test_the_method_is_upper_cased(self):
        self.assertEqual(
            crypto.seal_aad("post", PATH, TS, NONCE, SESSION, AGENT),
            REQUEST_AAD,
        )

    def test_the_path_keeps_its_query_string_verbatim(self):
        aad = crypto.seal_aad("GET", "/v1/events?since=10&wait=25", TS, NONCE,
                              SESSION, AGENT)
        self.assertIn(b"\n/v1/events?since=10&wait=25\n", aad)

    def test_a_newline_in_a_component_is_refused(self):
        with self.assertRaises(ValueError):
            crypto.seal_aad("POST", "/v1/events\nPOST", TS, NONCE, SESSION, AGENT)

    def test_every_field_changes_the_aad(self):
        base = crypto.seal_aad("POST", PATH, TS, NONCE, SESSION, AGENT)
        variants = [
            crypto.seal_aad("GET", PATH, TS, NONCE, SESSION, AGENT),
            crypto.seal_aad("POST", "/v1/events", TS, NONCE, SESSION, AGENT),
            crypto.seal_aad("POST", PATH, "1791456897", NONCE, SESSION, AGENT),
            crypto.seal_aad("POST", PATH, TS, "0" * 16, SESSION, AGENT),
            crypto.seal_aad("POST", PATH, TS, NONCE, "ses_0", AGENT),
            crypto.seal_aad("POST", PATH, TS, NONCE, SESSION, "enroll"),
            crypto.seal_aad("POST", PATH, TS, NONCE, SESSION, AGENT, response=True),
        ]
        self.assertEqual(len(set(variants + [base])), len(variants) + 1)


# --------------------------------------------------------------------------- #
# seal / unseal
# --------------------------------------------------------------------------- #


class TestSealRoundTrip(unittest.TestCase):
    def setUp(self):
        crypto.reset_nonce_guard()
        self.addCleanup(crypto.reset_nonce_guard)
        self.plain = json.dumps({"type": "chat.message", "body": {"text": "hello"}}).encode()

    def test_a_sealed_body_round_trips(self):
        sealed = crypto.seal(KEY_A, self.plain, REQUEST_AAD)
        self.assertNotIn(b"chat.message", sealed)
        self.assertEqual(len(sealed), len(self.plain) + crypto.NONCE_BYTES + crypto.TAG_BYTES)
        self.assertEqual(crypto.unseal(KEY_A, sealed, REQUEST_AAD), self.plain)

    def test_an_empty_body_round_trips(self):
        sealed = crypto.seal(KEY_A, b"", REQUEST_AAD)
        self.assertEqual(crypto.unseal(KEY_A, sealed, REQUEST_AAD), b"")

    def test_a_wrong_aad_is_rejected(self):
        sealed = crypto.seal(KEY_A, self.plain, REQUEST_AAD)
        wrong = crypto.seal_aad("POST", PATH, TS, "0" * 16, SESSION, AGENT)
        with self.assertRaises(BadSeal):
            crypto.unseal(KEY_A, sealed, wrong)

    def test_a_wrong_key_is_rejected(self):
        sealed = crypto.seal(KEY_A, self.plain, REQUEST_AAD)
        with self.assertRaises(BadSeal):
            crypto.unseal(KEY_B, sealed, REQUEST_AAD)

    def test_a_response_body_cannot_be_opened_as_a_request_body(self):
        """What the ``-RESPONSE`` prefix buys: the directions are not interchangeable."""
        sealed = crypto.seal(KEY_A, self.plain, RESPONSE_AAD)
        with self.assertRaises(BadSeal):
            crypto.unseal(KEY_A, sealed, REQUEST_AAD)
        self.assertEqual(crypto.unseal(KEY_A, sealed, RESPONSE_AAD), self.plain)

    def test_a_body_sealed_under_the_old_circular_reading_is_rejected(self):
        """The §3.3 string-to-sign is *not* the AAD, and must not open anything."""
        sts = crypto.string_to_sign("POST", PATH, self.plain, TS, NONCE, SESSION, AGENT)
        sealed = crypto.seal(KEY_A, self.plain, sts)
        with self.assertRaises(BadSeal):
            crypto.unseal(KEY_A, sealed, REQUEST_AAD)

    def test_an_altered_ciphertext_is_rejected(self):
        sealed = bytearray(crypto.seal(KEY_A, self.plain, REQUEST_AAD))
        sealed[crypto.NONCE_BYTES] ^= 0x01
        with self.assertRaises(BadSeal):
            crypto.unseal(KEY_A, bytes(sealed), REQUEST_AAD)

    def test_a_truncated_body_is_rejected_with_a_useful_message(self):
        with self.assertRaises(BadSeal):
            crypto.unseal(KEY_A, b"short", REQUEST_AAD)

    def test_sealing_twice_with_one_nonce_aborts(self):
        """SPEC §3.6: a repeated nonce under the same key MUST abort."""
        first = crypto.seal(KEY_A, self.plain, REQUEST_AAD)
        self.assertEqual(len(first[:crypto.NONCE_BYTES]), crypto.NONCE_BYTES)
        from parley.errors import NonceReuse

        with self.assertRaises(NonceReuse):
            crypto._NONCE_GUARD.claim(KEY_A, first[:crypto.NONCE_BYTES])

    def test_the_key_length_is_enforced(self):
        with self.assertRaises(CryptoFailure):
            crypto.seal(b"too short", self.plain, REQUEST_AAD)


class TestCrossBackendInterop(unittest.TestCase):
    """A body sealed by the accelerator must open under the pure fallback, and back.

    SPEC §3.6 lets an implementation pick ``cryptography``, ``PyNaCl`` or the
    bundled pure-Python code at will, so two Parley processes on one LAN can
    easily be running different ones. If the two ever disagreed, sealed mode
    would work on the developer's machine and fail on the user's.
    """

    def setUp(self):
        crypto.reset_nonce_guard()
        self.addCleanup(crypto.reset_nonce_guard)
        self.nonce = bytes(range(12))
        self.plain = b"sealed across two AEAD implementations"

    def _accelerated(self):
        """The native backend, loaded through ``parley.crypto``'s own loaders.

        Imported via the package rather than with an ``import cryptography`` here
        because the suite itself must stay stdlib-only (``test_portability``), and
        because this way the test exercises whichever accelerator the host has.
        """
        for loader in (crypto._load_cryptography, crypto._load_pynacl):
            try:
                return loader()
            except Exception:  # noqa: BLE001 - not installed on this host
                continue
        self.skipTest("no native AEAD accelerator is installed")

    def test_pure_output_opens_under_the_accelerator(self):
        _encrypt, decrypt = self._accelerated()
        sealed = crypto.pure_aead_encrypt(KEY_A, self.nonce, self.plain, REQUEST_AAD)
        self.assertEqual(decrypt(KEY_A, self.nonce, sealed, REQUEST_AAD), self.plain)

    def test_accelerator_output_opens_under_pure(self):
        encrypt, _decrypt = self._accelerated()
        sealed = encrypt(KEY_A, self.nonce, self.plain, REQUEST_AAD)
        self.assertEqual(
            crypto.pure_aead_decrypt(KEY_A, self.nonce, sealed, REQUEST_AAD),
            self.plain,
        )

    def test_the_accelerator_rejects_a_pure_sealed_body_with_the_wrong_aad(self):
        """Interop must not be achieved by one side being lax about the AAD."""
        _encrypt, decrypt = self._accelerated()
        sealed = crypto.pure_aead_encrypt(KEY_A, self.nonce, self.plain, REQUEST_AAD)
        with self.assertRaises(BadSeal):
            decrypt(KEY_A, self.nonce, sealed, RESPONSE_AAD)

    def test_the_live_backend_opens_what_pure_sealed(self):
        """Whatever ``PARLEY_CRYPTO_BACKEND`` selected, it must agree with pure."""
        sealed = self.nonce + crypto.pure_aead_encrypt(KEY_A, self.nonce, self.plain,
                                                       REQUEST_AAD)
        self.assertEqual(crypto.unseal(KEY_A, sealed, REQUEST_AAD), self.plain)

    def test_pure_opens_what_the_live_backend_sealed(self):
        sealed = crypto.seal(KEY_A, self.plain, REQUEST_AAD)
        self.assertEqual(
            crypto.pure_aead_decrypt(KEY_A, sealed[:12], sealed[12:], REQUEST_AAD),
            self.plain,
        )


# --------------------------------------------------------------------------- #
# Blob frames — specified non-circularly all along; verified, not rewritten
# --------------------------------------------------------------------------- #


class TestBlobFrames(unittest.TestCase):
    def setUp(self):
        crypto.reset_nonce_guard()
        self.addCleanup(crypto.reset_nonce_guard)
        self.data = os.urandom(crypto.BLOB_FRAME_BYTES + 1234)
        import hashlib

        self.digest = "sha256:" + hashlib.sha256(self.data).hexdigest()

    def test_a_multi_frame_blob_round_trips(self):
        sealed = crypto.seal_frames(KEY_A, self.data, self.digest)
        overhead = crypto.NONCE_BYTES + crypto.TAG_BYTES
        self.assertEqual(len(sealed), len(self.data) + 2 * overhead)  # exactly two frames
        self.assertEqual(crypto.unseal_frames(KEY_A, sealed, self.digest), self.data)

    def test_an_empty_blob_still_produces_one_authenticated_frame(self):
        sealed = crypto.seal_frames(KEY_A, b"", "sha256:" + "0" * 64)
        self.assertEqual(len(sealed), crypto.NONCE_BYTES + crypto.TAG_BYTES)
        self.assertEqual(crypto.unseal_frames(KEY_A, sealed, "sha256:" + "0" * 64), b"")

    def test_the_blob_hash_is_canonicalised_to_the_sha256_prefixed_form(self):
        """``crypto`` accepts a bare hex digest and seals under the prefixed one."""
        bare = self.digest.split(":", 1)[1]
        sealed = crypto.seal_frames(KEY_A, self.data, bare)
        self.assertEqual(crypto.unseal_frames(KEY_A, sealed, self.digest), self.data)
        self.assertEqual(crypto.unseal_frames(KEY_A, sealed, bare.upper()), self.data)

    def test_a_different_blob_hash_does_not_open_the_frames(self):
        sealed = crypto.seal_frames(KEY_A, self.data, self.digest)
        with self.assertRaises(BadSeal):
            crypto.unseal_frames(KEY_A, sealed, "sha256:" + "1" * 64)

    def test_swapping_two_frames_is_detected(self):
        """What the frame index in the AAD is for."""
        sealed = crypto.seal_frames(KEY_A, self.data, self.digest)
        full = crypto.NONCE_BYTES + crypto.BLOB_FRAME_BYTES + crypto.TAG_BYTES
        swapped = sealed[full:] + sealed[:full]
        with self.assertRaises(BadSeal):
            crypto.unseal_frames(KEY_A, swapped, self.digest)

    def test_a_wrong_key_does_not_open_the_frames(self):
        sealed = crypto.seal_frames(KEY_A, self.data, self.digest)
        with self.assertRaises(BadSeal):
            crypto.unseal_frames(KEY_B, sealed, self.digest)

    def test_a_malformed_blob_hash_is_refused(self):
        with self.assertRaises(CryptoFailure):
            crypto.seal_frames(KEY_A, b"x", "not-a-digest")


# --------------------------------------------------------------------------- #
# The Hub, driven in process
# --------------------------------------------------------------------------- #


class SealedHubTestCase(unittest.TestCase):
    """A real sealed Hub, called through ``api.handle`` — no socket needed."""

    def setUp(self):
        crypto.reset_nonce_guard()
        self.addCleanup(crypto.reset_nonce_guard)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hub, self.watchword = create_parley(
            Path(self._tmp.name), name="sealed-test", port=0, sealed=True,
        )
        self.addCleanup(self._shutdown)
        self.session = self.hub.config.session
        self.root_key = crypto.derive_root_key(
            self.watchword, self.session,
            iterations=getattr(self.hub.config, "pbkdf2_iterations", 200000),
        )
        self.enroll_key = crypto.enroll_key(self.root_key)
        self.seal_key = crypto.seal_key(self.root_key)

    def _shutdown(self):
        try:
            self.hub.stop()
        except Exception:
            pass

    # -- plumbing ----------------------------------------------------------
    def sealed_call(self, method, path, body=None, *, agent, key, seal_aad=None,
                    sign_over=None, extra_headers=None, seal=True):
        """One sealed request, verified the way SPEC §3.6 requires.

        ``seal_aad`` overrides the AAD (to prove a wrong one is refused) and
        ``sign_over`` overrides the bytes the §3.3 signature covers (to prove the
        signature is computed over the *sealed* body).
        """
        plain = b"" if body is None else (
            body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        )
        ts = str(int(time.time()))
        nonce = crypto.new_nonce_hex()
        if seal and plain:
            aad = seal_aad if seal_aad is not None else crypto.seal_aad(
                method, path, ts, nonce, self.session, agent)
            outer = crypto.seal(self.seal_key, plain, aad)
        else:
            outer = plain
        signed_bytes = outer if sign_over is None else sign_over
        sts = crypto.string_to_sign(method, path, signed_bytes, ts, nonce,
                                    self.session, agent)
        headers = {
            "X-Parley-Version": "PARLEY/1",
            "X-Parley-Session": self.session,
            "X-Parley-Agent": agent,
            "X-Parley-Timestamp": ts,
            "X-Parley-Nonce": nonce,
            "Authorization": "Parley-HMAC-SHA256 " + crypto.sign(key, sts),
            "Content-Type": "application/json; charset=utf-8",
        }
        if seal:
            headers["X-Parley-Seal"] = "v1"
        headers.update(extra_headers or {})
        status, resp_headers, resp_body = api.handle(self.hub, method, path, headers, outer)
        return status, resp_headers, resp_body, (method, path, ts, nonce, agent)

    def open_response(self, resp_headers, resp_body, binding):
        """Decrypt a response the one way §3.6 allows, then parse it."""
        method, path, ts, nonce, agent = binding
        self.assertEqual(resp_headers.get("X-Parley-Seal"), "v1",
                         "a sealed request must get a sealed response")
        aad = crypto.seal_aad(method, path, ts, nonce, self.session, agent, response=True)
        plain = crypto.unseal(self.seal_key, resp_body, aad)
        return json.loads(plain.decode("utf-8"))

    def enrol(self, name="Ada", **kwargs):
        body = {"session": self.session, "agent": {
            "name": name, "kind": "sealed-test", "os": "linux", "host": "bench",
            "client_version": "1.0.0", "capabilities": ["chat"],
        }}
        return self.sealed_call("POST", "/v1/enroll", body,
                                agent="enroll", key=self.enroll_key, **kwargs)

    def enrolled_agent(self, name="Ada"):
        status, headers, raw, binding = self.enrol(name=name)
        self.assertEqual(status, 201, raw[:200])
        payload = self.open_response(headers, raw, binding)
        return payload["agent_id"], bytes.fromhex(payload["agent_key"])

    @staticmethod
    def error_code(raw):
        try:
            return json.loads(raw.decode("utf-8"))["error"]["code"]
        except Exception:
            return raw[:200]


class TestSealedEnrolment(SealedHubTestCase):
    """The hard case: sealed under ``seal_key``, signed under ``enroll_key``."""

    def test_a_correctly_sealed_enrolment_succeeds(self):
        status, headers, raw, binding = self.enrol()
        self.assertEqual(status, 201, self.error_code(raw))
        payload = self.open_response(headers, raw, binding)
        self.assertTrue(payload["agent_id"].startswith("agt_"))
        self.assertEqual(len(payload["agent_key"]), 64)

    def test_the_enrolment_body_never_appears_in_the_clear(self):
        plain = json.dumps({"session": self.session, "agent": {
            "name": "Ada", "kind": "sealed-test", "os": "linux", "host": "bench",
            "client_version": "1.0.0", "capabilities": ["chat"]}}).encode("utf-8")
        ts = str(int(time.time()))
        nonce = crypto.new_nonce_hex()
        aad = crypto.seal_aad("POST", "/v1/enroll", ts, nonce, self.session, "enroll")
        outer = crypto.seal(self.seal_key, plain, aad)
        self.assertNotIn(b"workbench", outer)
        self.assertNotIn(b"sealed-test", outer)

    def test_an_aad_built_with_the_wrong_agent_id_is_refused(self):
        """``"enroll"`` is normative; ``""`` or an agent id must not be accepted."""
        ts = str(int(time.time()))
        nonce = crypto.new_nonce_hex()
        for wrong_agent in ("", "agt_0000000000000000", "ENROLL"):
            with self.subTest(agent=wrong_agent):
                aad = crypto.seal_aad("POST", "/v1/enroll", ts, nonce,
                                      self.session, wrong_agent)
                status, _h, raw, _b = self.enrol(seal_aad=aad)
                self.assertEqual(status, 400, raw[:200])
                self.assertEqual(self.error_code(raw), "bad_request")

    def test_an_unsealed_enrolment_is_refused_by_a_sealed_hub(self):
        status, _h, raw, _b = self.enrol(seal=False)
        self.assertEqual(status, 400, raw[:200])
        self.assertEqual(self.error_code(raw), "bad_request")


class TestSealedRequests(SealedHubTestCase):
    def setUp(self):
        super().setUp()
        self.agent_id, self.agent_key = self.enrolled_agent()

    def post_event(self, **kwargs):
        event = protocol.make_event(self.agent_id, self.session, "chat.message",
                                    {"text": "sealed hello"})
        return self.sealed_call("POST", "/v1/events", event,
                                agent=self.agent_id, key=self.agent_key, **kwargs)

    def test_a_sealed_event_round_trips_through_the_hub(self):
        status, headers, raw, binding = self.post_event()
        self.assertEqual(status, 201, self.error_code(raw))
        self.assertGreaterEqual(int(self.open_response(headers, raw, binding)["seq"]), 1)

        status, headers, raw, binding = self.sealed_call(
            "GET", "/v1/events?since=0&limit=50", agent=self.agent_id, key=self.agent_key)
        self.assertEqual(status, 200, self.error_code(raw))
        payload = self.open_response(headers, raw, binding)
        texts = [e["body"].get("text") for e in payload["events"]
                 if e["type"] == "chat.message"]
        self.assertIn("sealed hello", texts)

    def test_a_bodyless_sealed_read_still_gets_a_sealed_response(self):
        """§3.6 routes a sealed client's long-poll reads through the sealed path.

        If the Hub answered a sealed GET in the clear, every event body a sealed
        client ever read would be on the wire in plaintext — which is the one
        thing sealed mode exists to prevent.
        """
        self.post_event()
        status, headers, raw, _binding = self.sealed_call(
            "GET", "/v1/events?since=0&limit=50", agent=self.agent_id, key=self.agent_key)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Parley-Seal"), "v1")
        self.assertNotIn(b"sealed hello", raw)

    def test_the_response_opens_only_under_the_response_aad(self):
        status, headers, raw, binding = self.post_event()
        self.assertEqual(status, 201)
        method, path, ts, nonce, agent = binding
        request_aad = crypto.seal_aad(method, path, ts, nonce, self.session, agent)
        with self.assertRaises(BadSeal):
            crypto.unseal(self.seal_key, raw, request_aad)
        self.assertIn("seq", self.open_response(headers, raw, binding))

    def test_an_aad_using_a_different_timestamp_is_refused(self):
        ts = str(int(time.time()) - 5)
        nonce = crypto.new_nonce_hex()
        aad = crypto.seal_aad("POST", "/v1/events", ts, nonce, self.session, self.agent_id)
        status, _h, raw, _b = self.post_event(seal_aad=aad)
        self.assertEqual(status, 400, raw[:200])

    def test_the_old_circular_aad_is_refused_rather_than_silently_accepted(self):
        """SPEC §3.6 forbids "try several AADs and take whichever authenticates".

        Each of these is a reading some implementation did invent. Every one of
        them must now fail loudly — that is the entire point of pinning the AAD.
        """
        plain = json.dumps(protocol.make_event(self.agent_id, self.session,
                                               "chat.message", {"text": "x"})).encode()
        ts = str(int(time.time()))
        nonce = crypto.new_nonce_hex()
        wrong_aads = {
            "string_to_sign over the plaintext body":
                crypto.string_to_sign("POST", "/v1/events", plain, ts, nonce,
                                      self.session, self.agent_id),
            "string_to_sign over an empty body":
                crypto.string_to_sign("POST", "/v1/events", b"", ts, nonce,
                                      self.session, self.agent_id),
            "empty AAD": b"",
        }
        for label, aad in wrong_aads.items():
            with self.subTest(aad=label):
                status, _h, raw, _b = self.post_event(seal_aad=aad)
                self.assertEqual(status, 400, raw[:200])
                self.assertEqual(self.error_code(raw), "bad_request")

    def test_the_signature_covers_the_sealed_body_not_the_plaintext(self):
        """§3.6: verify first, then unseal. Never decrypt an unauthenticated body."""
        event = protocol.make_event(self.agent_id, self.session, "chat.message",
                                    {"text": "sealed hello"})
        plain = json.dumps(event).encode("utf-8")
        status, _h, raw, _b = self.post_event(sign_over=plain)
        self.assertEqual(status, 401, raw[:200])
        self.assertEqual(self.error_code(raw), "bad_signature")

    def test_a_tampered_ciphertext_fails_the_signature_before_any_decryption(self):
        event = protocol.make_event(self.agent_id, self.session, "chat.message",
                                    {"text": "sealed hello"})
        plain = json.dumps(event).encode("utf-8")
        ts = str(int(time.time()))
        nonce = crypto.new_nonce_hex()
        aad = crypto.seal_aad("POST", "/v1/events", ts, nonce, self.session, self.agent_id)
        outer = bytearray(crypto.seal(self.seal_key, plain, aad))
        outer[-1] ^= 0xFF
        sts = crypto.string_to_sign("POST", "/v1/events", crypto.seal(
            self.seal_key, plain, aad), ts, nonce, self.session, self.agent_id)
        headers = {
            "X-Parley-Version": "PARLEY/1",
            "X-Parley-Session": self.session,
            "X-Parley-Agent": self.agent_id,
            "X-Parley-Timestamp": ts,
            "X-Parley-Nonce": nonce,
            "X-Parley-Seal": "v1",
            "Authorization": "Parley-HMAC-SHA256 " + crypto.sign(self.agent_key, sts),
        }
        status, _rh, raw = api.handle(self.hub, "POST", "/v1/events", headers, bytes(outer))
        self.assertEqual(status, 401, raw[:200])
        self.assertEqual(self.error_code(raw), "bad_signature")

    def test_a_blob_round_trips_in_frames(self):
        data = os.urandom(crypto.BLOB_FRAME_BYTES + 777)
        import hashlib

        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        payload = crypto.seal_frames(self.seal_key, data, digest)
        ts = str(int(time.time()))
        nonce = crypto.new_nonce_hex()
        sts = crypto.string_to_sign("POST", "/v1/blobs", payload, ts, nonce,
                                    self.session, self.agent_id)
        headers = {
            "X-Parley-Version": "PARLEY/1",
            "X-Parley-Session": self.session,
            "X-Parley-Agent": self.agent_id,
            "X-Parley-Timestamp": ts,
            "X-Parley-Nonce": nonce,
            "X-Parley-Seal": "v1",
            "X-Parley-Blob-SHA256": digest,
            "Authorization": "Parley-HMAC-SHA256 " + crypto.sign(self.agent_key, sts),
        }
        status, _rh, raw = api.handle(self.hub, "POST", "/v1/blobs", headers, payload)
        self.assertIn(status, (200, 201), raw[:200])

        ts = str(int(time.time()))
        nonce = crypto.new_nonce_hex()
        path = "/v1/blobs/" + digest
        sts = crypto.string_to_sign("GET", path, b"", ts, nonce, self.session, self.agent_id)
        headers = {
            "X-Parley-Version": "PARLEY/1",
            "X-Parley-Session": self.session,
            "X-Parley-Agent": self.agent_id,
            "X-Parley-Timestamp": ts,
            "X-Parley-Nonce": nonce,
            "X-Parley-Seal": "v1",
            "Authorization": "Parley-HMAC-SHA256 " + crypto.sign(self.agent_key, sts),
        }
        status, resp_headers, raw = api.handle(self.hub, "GET", path, headers, b"")
        self.assertEqual(status, 200, raw[:200])
        self.assertEqual(resp_headers.get("X-Parley-Seal"), "v1")
        self.assertEqual(crypto.unseal_frames(self.seal_key, raw, digest), data)


class TestSealedStreamIsRefused(SealedHubTestCase):
    """SPEC §3.6: there is no sealed framing for ``text/event-stream``."""

    def setUp(self):
        super().setUp()
        self.agent_id, self.agent_key = self.enrolled_agent()

    def _stream(self, headers):
        return api.handle(self.hub, "GET", "/v1/stream?since=0", headers, b"")

    def _signed_headers(self, path="/v1/stream?since=0", **extra):
        ts = str(int(time.time()))
        nonce = crypto.new_nonce_hex()
        sts = crypto.string_to_sign("GET", path, b"", ts, nonce, self.session, self.agent_id)
        headers = {
            "X-Parley-Version": "PARLEY/1",
            "X-Parley-Session": self.session,
            "X-Parley-Agent": self.agent_id,
            "X-Parley-Timestamp": ts,
            "X-Parley-Nonce": nonce,
            "Authorization": "Parley-HMAC-SHA256 " + crypto.sign(self.agent_key, sts),
        }
        headers.update(extra)
        return headers

    def test_a_sealed_stream_request_is_422_bad_request(self):
        status, _h, raw = self._stream(self._signed_headers(**{"X-Parley-Seal": "v1"}))
        self.assertEqual(status, 422, raw[:200])
        self.assertEqual(self.error_code(raw), "bad_request")

    def test_the_refusal_names_the_long_poll_alternative(self):
        _s, _h, raw = self._stream(self._signed_headers(**{"X-Parley-Seal": "v1"}))
        hint = json.loads(raw.decode("utf-8"))["error"].get("hint", "")
        self.assertIn("/v1/events", hint)

    def test_the_refusal_does_not_depend_on_the_credential(self):
        """Refused before authentication: a 401 would send the client off fixing
        its signing instead of its transport choice."""
        status, _h, raw = self._stream({"X-Parley-Seal": "v1"})
        self.assertEqual(status, 422, raw[:200])

    def test_an_unsealed_stream_request_is_not_refused_here(self):
        """The guard must be narrow: only the sealed case is a 422."""
        status, _h, raw = self._stream(self._signed_headers())
        self.assertNotEqual(status, 422, raw[:200])

    def test_other_endpoints_are_unaffected_by_the_seal_header(self):
        status, _h, raw, _b = self.sealed_call(
            "GET", "/v1/me", agent=self.agent_id, key=self.agent_key)
        self.assertEqual(status, 200, raw[:200])


class TestHostTokenCarrier(SealedHubTestCase):
    """SPEC §3.7: the host token has exactly one carrier."""

    def admin(self, headers, path="/v1/admin/viewer-token"):
        body = b"{}"
        headers = dict(headers)
        headers.setdefault("Content-Type", "application/json")
        return api.handle(self.hub, "POST", path, headers, body)

    def test_the_authorization_header_is_accepted(self):
        status, _h, raw = self.admin(
            {"Authorization": "Parley-Host " + self.hub.config.host_token})
        self.assertIn(status, (200, 201), raw[:200])

    def test_the_bespoke_header_is_ignored(self):
        status, _h, raw = self.admin(
            {"X-Parley-Host-Token": self.hub.config.host_token})
        self.assertEqual(status, 403, raw[:200])
        self.assertEqual(self.error_code(raw), "host_token_required")

    def test_the_query_parameter_is_ignored(self):
        status, _h, raw = self.admin(
            {}, path="/v1/admin/viewer-token?ht=" + self.hub.config.host_token)
        self.assertEqual(status, 403, raw[:200])
        self.assertEqual(self.error_code(raw), "host_token_required")

    def test_a_wrong_token_in_the_right_carrier_is_refused(self):
        status, _h, raw = self.admin({"Authorization": "Parley-Host hst_" + "0" * 32})
        self.assertIn(status, (401, 403), raw[:200])


# --------------------------------------------------------------------------- #
# The client side of the same contract
# --------------------------------------------------------------------------- #


class TestTransportSealing(unittest.TestCase):
    """The client must seal under the AAD its own headers describe.

    The original bug was exactly here: the AAD was built with one freshly drawn
    timestamp and nonce and the headers with another, so the Hub rebuilt an AAD
    the client had never used and every sealed body failed its tag.
    """

    def setUp(self):
        crypto.reset_nonce_guard()
        self.addCleanup(crypto.reset_nonce_guard)
        self.seal_key = KEY_A
        self.transport = Transport(
            "http://127.0.0.1:1", "ses_" + "a" * 16, "agt_" + "b" * 16, KEY_B,
            sealed=True, seal_key=self.seal_key,
        )
        self.sent = []

        def fake_raw(method, url, body, headers, timeout):
            self.sent.append({"method": method, "url": url, "body": body,
                              "headers": headers})
            return 200, {}, b""

        self.transport._raw = fake_raw  # type: ignore[assignment]

    def test_the_body_is_sealed_under_the_aad_the_headers_describe(self):
        self.transport.request("POST", "/v1/events", json_body={"hello": "world"})
        sent = self.sent[-1]
        aad = crypto.seal_aad(
            "POST", "/v1/events", sent["headers"]["X-Parley-Timestamp"],
            sent["headers"]["X-Parley-Nonce"], self.transport.session,
            self.transport.agent_id,
        )
        self.assertEqual(
            json.loads(crypto.unseal(self.seal_key, sent["body"], aad).decode()),
            {"hello": "world"},
        )

    def test_the_signature_is_computed_over_the_sealed_bytes(self):
        self.transport.request("POST", "/v1/events", json_body={"hello": "world"})
        sent = self.sent[-1]
        sts = crypto.string_to_sign(
            "POST", "/v1/events", sent["body"], sent["headers"]["X-Parley-Timestamp"],
            sent["headers"]["X-Parley-Nonce"], self.transport.session,
            self.transport.agent_id,
        )
        signature = sent["headers"]["Authorization"].split(" ", 1)[1]
        self.assertTrue(crypto.verify(KEY_B, sts, signature))

    def test_the_seal_header_is_set(self):
        self.transport.request("POST", "/v1/events", json_body={"a": 1})
        self.assertEqual(self.sent[-1]["headers"]["X-Parley-Seal"], "v1")

    def test_a_response_sealed_under_the_response_aad_is_opened(self):
        captured = {}

        def fake_raw(method, url, body, headers, timeout):
            aad = crypto.seal_aad(method, "/v1/events", headers["X-Parley-Timestamp"],
                                  headers["X-Parley-Nonce"], self.transport.session,
                                  self.transport.agent_id, response=True)
            captured["ok"] = True
            return 200, {"x-parley-seal": "v1"}, crypto.seal(
                self.seal_key, b'{"seq": 7}', aad)

        self.transport._raw = fake_raw  # type: ignore[assignment]
        status, _h, body = self.transport.request("POST", "/v1/events", json_body={"a": 1})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode()), {"seq": 7})
        self.assertTrue(captured["ok"])

    def test_a_response_sealed_under_the_request_aad_is_rejected(self):
        """No probing: a response that used the wrong direction must fail loudly."""
        def fake_raw(method, url, body, headers, timeout):
            aad = crypto.seal_aad(method, "/v1/events", headers["X-Parley-Timestamp"],
                                  headers["X-Parley-Nonce"], self.transport.session,
                                  self.transport.agent_id)
            return 200, {"x-parley-seal": "v1"}, crypto.seal(
                self.seal_key, b'{"seq": 7}', aad)

        self.transport._raw = fake_raw  # type: ignore[assignment]
        from parley.errors import TransportError

        with self.assertRaises(TransportError):
            self.transport.request("POST", "/v1/events", json_body={"a": 1},
                                   max_attempts=1)

    def test_a_sealed_client_refuses_to_open_an_sse_stream(self):
        from parley.errors import TransportError

        with self.assertRaises(TransportError):
            next(iter(self.transport._iter_sse(0, None)))

    def test_a_sealed_client_streams_by_long_poll(self):
        self.assertEqual(self.transport.mode, "poll")

    def test_an_enrol_transport_seals_under_the_literal_enroll_agent_id(self):
        enrol = Transport("http://127.0.0.1:1", "ses_" + "a" * 16, "enroll", KEY_B,
                          sealed=True, seal_key=self.seal_key)
        sent = []
        enrol._raw = lambda method, url, body, headers, timeout: (  # type: ignore
            sent.append({"body": body, "headers": headers}) or (200, {}, b"")
        )
        enrol.request("POST", "/v1/enroll", json_body={"session": "ses_x"})
        headers = sent[-1]["headers"]
        self.assertEqual(headers["X-Parley-Agent"], "enroll")
        aad = crypto.seal_aad("POST", "/v1/enroll", headers["X-Parley-Timestamp"],
                              headers["X-Parley-Nonce"], enrol.session, "enroll")
        self.assertEqual(
            json.loads(crypto.unseal(self.seal_key, sent[-1]["body"], aad).decode()),
            {"session": "ses_x"},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
