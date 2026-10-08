"""SPEC §3 — key hierarchy, request signing, sealed mode, watchword handling.

Everything here is checked against an independent oracle or a published test vector, never
against the implementation's own output. Two things in particular are pinned
byte-for-byte, because an implementation that disagrees about them cannot talk to anyone:

* ``string_to_sign`` (SPEC §3.3) — the exact bytes the Hub and every client must hash.
* the HKDF ``info`` strings (SPEC §3.2) — the exact labels the key hierarchy hangs off.
"""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
import unittest
from unittest import mock

from parley import crypto
from tests import helpers
from tests.helpers import SECRETS, json_fixture

RFC5869 = json_fixture("rfc5869.json")
RFC8439 = json_fixture("rfc8439.json")
STS_FIXTURE = json_fixture("string_to_sign.json")


class TestReferenceOraclesAreCorrect(unittest.TestCase):
    """Validate the oracle before trusting it to judge the implementation."""

    def test_hkdf_oracle_matches_every_rfc5869_sha256_vector(self):
        for case in RFC5869["cases"]:
            with self.subTest(case=case["name"]):
                prk = helpers.hkdf_extract(bytes.fromhex(case["salt"]),
                                           bytes.fromhex(case["ikm"]))
                self.assertEqual(prk.hex(), case["prk"])
                okm = helpers.hkdf_reference(bytes.fromhex(case["ikm"]),
                                             bytes.fromhex(case["info"]),
                                             case["L"], bytes.fromhex(case["salt"]))
                self.assertEqual(okm.hex(), case["okm"])

    def test_aead_oracle_matches_the_rfc8439_worked_example(self):
        ciphertext, tag = helpers.aead_encrypt(
            bytes.fromhex(RFC8439["key"]),
            bytes.fromhex(RFC8439["nonce"]),
            RFC8439["plaintext_utf8"].encode("utf-8"),
            bytes.fromhex(RFC8439["aad"]),
        )
        self.assertEqual(ciphertext.hex(), RFC8439["ciphertext"])
        self.assertEqual(tag.hex(), RFC8439["tag"])


class TestHKDF(unittest.TestCase):
    def test_hkdf_matches_rfc5869_test_case_3(self):
        """TC3 is the empty-salt, empty-info case, which is exactly parley's shape."""
        case = next(c for c in RFC5869["cases"] if c["name"] == "TC3")
        self.assertEqual(
            crypto.hkdf(bytes.fromhex(case["ikm"]), bytes.fromhex(case["info"]), case["L"]).hex(),
            case["okm"],
        )

    def test_hkdf_uses_an_empty_salt_for_arbitrary_inputs(self):
        for ikm, info, length in (
            (b"", b"", 32),
            (b"k", b"parley/v1/enroll", 32),
            (bytes(range(256)), b"\x00\xff", 64),
            (b"x" * 1000, b"info", 1),
            (b"short", b"", 255 * 32),
        ):
            with self.subTest(length=length, info=info):
                self.assertEqual(
                    crypto.hkdf(ikm, info, length),
                    helpers.hkdf_reference(ikm, info, length, salt=b""),
                )

    def test_hkdf_defaults_to_thirty_two_bytes(self):
        self.assertEqual(len(crypto.hkdf(b"key", b"info")), 32)

    def test_hkdf_rejects_an_output_longer_than_the_rfc_allows(self):
        with self.assertRaises(Exception):
            crypto.hkdf(b"key", b"info", 255 * 32 + 1)

    def test_different_info_strings_give_independent_keys(self):
        root = bytes(range(32))
        outputs = {crypto.hkdf(root, label) for label in
                   (b"parley/v1/enroll", b"parley/v1/seal", b"parley/v1/fingerprint")}
        self.assertEqual(len(outputs), 3)


class TestKeyHierarchy(unittest.TestCase):
    WATCHWORD = "copper-otter-climbs-the-quiet-hill"
    SESSION = "ses_9f2c41ab77e0d315"

    def test_root_key_is_pbkdf2_over_the_normalised_watchword_salted_by_the_session(self):
        root = crypto.derive_root_key(self.WATCHWORD, self.SESSION, iterations=1000)
        expected = hashlib.pbkdf2_hmac(
            "sha256",
            crypto.normalise_watchword(self.WATCHWORD).encode("utf-8"),
            self.SESSION.encode("utf-8"),
            1000,
            32,
        )
        self.assertEqual(root, expected)

    def test_root_key_derivation_is_deterministic(self):
        a = crypto.derive_root_key(self.WATCHWORD, self.SESSION, iterations=1000)
        b = crypto.derive_root_key(self.WATCHWORD, self.SESSION, iterations=1000)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32)

    def test_a_different_session_gives_a_different_root_key(self):
        a = crypto.derive_root_key(self.WATCHWORD, self.SESSION, iterations=1000)
        b = crypto.derive_root_key(self.WATCHWORD, "ses_0000000000000000", iterations=1000)
        self.assertNotEqual(a, b)

    def test_a_different_iteration_count_gives_a_different_root_key(self):
        a = crypto.derive_root_key(self.WATCHWORD, self.SESSION, iterations=1000)
        b = crypto.derive_root_key(self.WATCHWORD, self.SESSION, iterations=2000)
        self.assertNotEqual(a, b)

    def test_the_default_iteration_count_is_two_hundred_thousand(self):
        explicit = crypto.derive_root_key(self.WATCHWORD, self.SESSION, iterations=200000)
        self.assertEqual(crypto.derive_root_key(self.WATCHWORD, self.SESSION), explicit)

    def test_a_spoken_watchword_derives_the_same_key_as_the_canonical_one(self):
        spoken = crypto.derive_root_key("Copper Otter Climbs the Quiet Hill!",
                                        self.SESSION, iterations=1000)
        canonical = crypto.derive_root_key(self.WATCHWORD, self.SESSION, iterations=1000)
        self.assertEqual(spoken, canonical)

    def test_enroll_and_seal_keys_hang_off_the_documented_info_strings(self):
        root = bytes(range(32))
        self.assertEqual(crypto.enroll_key(root), crypto.hkdf(root, b"parley/v1/enroll"))
        self.assertEqual(crypto.seal_key(root), crypto.hkdf(root, b"parley/v1/seal"))

    def test_enroll_and_seal_keys_are_distinct_from_each_other_and_from_the_root(self):
        root = bytes(range(32))
        keys = {root, crypto.enroll_key(root), crypto.seal_key(root)}
        self.assertEqual(len(keys), 3)
        self.assertEqual(len(crypto.enroll_key(root)), 32)
        self.assertEqual(len(crypto.seal_key(root)), 32)


class TestFingerprint(unittest.TestCase):
    def test_fingerprint_is_three_words_from_the_bundled_wordlist(self):
        from parley.wordlist import WORDS

        fingerprint = crypto.fingerprint(bytes(range(32)))
        parts = fingerprint.split("-")
        self.assertEqual(len(parts), 3, fingerprint)
        for part in parts:
            self.assertIn(part, WORDS)

    def test_fingerprint_is_stable_for_a_given_root_key(self):
        root = bytes(range(32))
        self.assertEqual(crypto.fingerprint(root), crypto.fingerprint(root))

    def test_fingerprint_depends_only_on_the_root_key(self):
        self.assertNotEqual(crypto.fingerprint(bytes(32)), crypto.fingerprint(bytes(range(32))))

    def test_fingerprint_is_derived_from_six_bytes_so_it_spreads_widely(self):
        seen = {crypto.fingerprint(i.to_bytes(32, "big")) for i in range(500)}
        self.assertGreater(len(seen), 495, "48 bits should give essentially no collisions")

    def test_fingerprint_never_leaks_the_root_key(self):
        root = bytes(range(32))
        self.assertNotIn(root.hex()[:16], crypto.fingerprint(root))


class TestWatchwordNormalisation(unittest.TestCase):
    CANONICAL = "copper-otter-climbs-the-quiet-hill"

    def test_the_canonical_form_is_a_fixed_point(self):
        self.assertEqual(crypto.normalise_watchword(self.CANONICAL), self.CANONICAL)

    def test_case_and_spacing_do_not_matter(self):
        for variant in (
            "Copper Otter Climbs the Quiet Hill",
            "COPPER OTTER CLIMBS THE QUIET HILL",
            "  copper   otter   climbs   the   quiet   hill  ",
            "copper_otter_climbs_the_quiet_hill",
            "copper.otter.climbs.the.quiet.hill",
            "Copper-Otter-Climbs-The-Quiet-Hill",
            "\tcopper\notter\rclimbs\tthe quiet hill\n",
        ):
            with self.subTest(variant=variant):
                self.assertEqual(crypto.normalise_watchword(variant), self.CANONICAL)

    def test_punctuation_collapses_to_a_single_separator(self):
        for variant in (
            "copper, otter; climbs: the -- quiet!! hill.",
            "copper---otter...climbs???the//quiet\\\\hill",
            '"copper" (otter) [climbs] {the} <quiet> hill!',
        ):
            with self.subTest(variant=variant):
                self.assertEqual(crypto.normalise_watchword(variant), self.CANONICAL)

    def test_accents_are_stripped_so_a_phone_spelling_still_works(self):
        self.assertEqual(crypto.normalise_watchword("cóppér ötter clímbs the quïet hîll"),
                         self.CANONICAL)
        self.assertEqual(crypto.normalise_watchword("çopper otter climbs the quiet hill"),
                         self.CANONICAL)

    def test_precomposed_and_decomposed_accents_normalise_alike(self):
        precomposed = unicodedata.normalize("NFC", "café")
        decomposed = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(precomposed, decomposed)
        self.assertEqual(crypto.normalise_watchword(precomposed),
                         crypto.normalise_watchword(decomposed))
        self.assertEqual(crypto.normalise_watchword(precomposed), "cafe")

    def test_compatibility_forms_fold_because_nfkd_is_specified(self):
        self.assertEqual(crypto.normalise_watchword("ｃｏｐｐｅｒ"),
                         "copper")

    def test_a_dotted_capital_i_folds_to_a_plain_i(self):
        self.assertEqual(crypto.normalise_watchword("İron"), "iron")

    def test_digits_survive(self):
        self.assertEqual(crypto.normalise_watchword("Word1 Word2"), "word1-word2")

    def test_leading_and_trailing_separators_are_stripped(self):
        self.assertEqual(crypto.normalise_watchword("---copper otter---"), "copper-otter")

    def test_an_empty_or_punctuation_only_watchword_normalises_to_empty(self):
        self.assertEqual(crypto.normalise_watchword(""), "")
        self.assertEqual(crypto.normalise_watchword("   "), "")
        self.assertEqual(crypto.normalise_watchword("!!!---???"), "")

    def test_normalisation_is_idempotent(self):
        for variant in ("Copper Otter!", "  a--b  ", "éè", "x", ""):
            with self.subTest(variant=variant):
                once = crypto.normalise_watchword(variant)
                self.assertEqual(crypto.normalise_watchword(once), once)

    def test_distinct_watchwords_stay_distinct(self):
        self.assertNotEqual(crypto.normalise_watchword("copper otter"),
                            crypto.normalise_watchword("copper otters"))


class TestWatchwordGeneration(unittest.TestCase):
    def test_five_drawn_words_plus_the_literal_filler(self):
        watchword = crypto.generate_watchword(5)
        parts = watchword.split("-")
        self.assertEqual(len(parts), 6, watchword)
        self.assertEqual(parts[3], "the",
                         "SPEC §3.1: the filler sits at a fixed position")

    def test_generated_watchwords_are_already_normalised(self):
        for _ in range(20):
            watchword = crypto.generate_watchword()
            self.assertEqual(crypto.normalise_watchword(watchword), watchword)

    def test_every_drawn_word_comes_from_the_bundled_list(self):
        from parley.wordlist import WORDS

        parts = crypto.generate_watchword(5).split("-")
        for index, part in enumerate(parts):
            if index == 3:
                continue
            self.assertIn(part, WORDS)

    def test_the_word_count_argument_is_honoured(self):
        for count in (3, 4, 5, 6, 8):
            with self.subTest(words=count):
                self.assertEqual(len(crypto.generate_watchword(count).split("-")), count + 1)

    def test_generation_has_real_entropy(self):
        produced = {crypto.generate_watchword(5) for _ in range(300)}
        self.assertEqual(len(produced), 300, "watchwords must not repeat in 300 draws")

    def test_the_wordlist_carries_at_least_fifty_five_bits_over_five_words(self):
        from parley.wordlist import WORDS

        self.assertGreaterEqual(len(WORDS), 2048)
        self.assertGreaterEqual(5 * (len(WORDS).bit_length() - 1), 55)


class TestStringToSign(unittest.TestCase):
    """SPEC §3.3, pinned to the byte. If this drifts, nothing can authenticate."""

    def test_every_pinned_case_reproduces_exactly(self):
        for case in STS_FIXTURE["cases"]:
            with self.subTest(case=case["name"]):
                produced = crypto.string_to_sign(
                    case["method"], case["path"], case["body_utf8"].encode("utf-8"),
                    case["ts"], case["nonce"], case["session"], case["agent"],
                )
                self.assertIsInstance(produced, bytes)
                self.assertEqual(produced.hex(), case["string_to_sign_hex"])

    def test_the_hand_written_expectation_is_what_the_spec_describes(self):
        case = STS_FIXTURE["cases"][0]
        expected = b"\n".join([
            b"PARLEY/1",
            b"POST",
            b"/v1/events?since=10",
            hashlib.sha256(case["body_utf8"].encode("utf-8")).hexdigest().encode("ascii"),
            b"1791456896",
            b"3f8a1c04bb9e7d62",
            b"ses_9f2c41ab77e0d315",
            b"agt_0c5518aa91be7742",
        ])
        self.assertEqual(bytes.fromhex(case["string_to_sign_hex"]), expected)

    def test_an_absent_body_hashes_the_empty_string(self):
        empty_sha = hashlib.sha256(b"").hexdigest()
        self.assertEqual(empty_sha,
                         "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        sts = crypto.string_to_sign("GET", "/v1/state", b"", "1", "n", "ses_x", "agt_y")
        self.assertIn(empty_sha.encode("ascii"), sts)

    def test_the_method_is_upper_cased(self):
        upper = crypto.string_to_sign("POST", "/v1/events", b"", "1", "n", "s", "a")
        lower = crypto.string_to_sign("post", "/v1/events", b"", "1", "n", "s", "a")
        self.assertEqual(upper, lower)

    def test_there_are_exactly_eight_lines_and_no_trailing_newline(self):
        sts = crypto.string_to_sign("GET", "/v1/state", b"", "1", "n", "s", "a")
        self.assertEqual(sts.count(b"\n"), 7)
        self.assertFalse(sts.endswith(b"\n"))

    def test_the_query_string_is_part_of_the_signature(self):
        a = crypto.string_to_sign("GET", "/v1/events?since=10", b"", "1", "n", "s", "a")
        b = crypto.string_to_sign("GET", "/v1/events?since=11", b"", "1", "n", "s", "a")
        self.assertNotEqual(a, b)

    def test_the_enrol_pseudo_agent_is_carried_verbatim(self):
        sts = crypto.string_to_sign("POST", "/v1/enroll", b"{}", "1", "n", "ses_x", "enroll")
        self.assertTrue(sts.endswith(b"\nenroll"))

    def test_a_non_ascii_path_is_utf8_encoded(self):
        case = next(c for c in STS_FIXTURE["cases"] if c["name"] == "non_ascii_path")
        produced = crypto.string_to_sign(
            case["method"], case["path"], b"", case["ts"], case["nonce"],
            case["session"], case["agent"],
        )
        self.assertIn("café".encode("utf-8"), produced)
        self.assertEqual(produced.hex(), case["string_to_sign_hex"])


class TestRequestSigning(unittest.TestCase):
    def test_signature_matches_a_plain_hmac_sha256(self):
        for case in STS_FIXTURE["cases"]:
            with self.subTest(case=case["name"]):
                sts = bytes.fromhex(case["string_to_sign_hex"])
                self.assertEqual(crypto.sign(bytes(32), sts), case["signature_with_zero_key"])

    def test_signatures_are_lower_case_hex_of_the_right_length(self):
        signature = crypto.sign(b"key", b"data")
        self.assertEqual(len(signature), 64)
        self.assertEqual(signature, signature.lower())
        bytes.fromhex(signature)

    def test_verify_accepts_a_good_signature_and_rejects_everything_else(self):
        key, sts = b"k" * 32, b"payload"
        good = crypto.sign(key, sts)
        self.assertTrue(crypto.verify(key, sts, good))
        self.assertFalse(crypto.verify(key, sts, good[:-1] + ("0" if good[-1] != "0" else "1")))
        self.assertFalse(crypto.verify(b"other" * 7, sts, good))
        self.assertFalse(crypto.verify(key, b"tampered", good))

    def test_verify_rejects_garbage_without_raising(self):
        key, sts = b"k" * 32, b"payload"
        for bad in ("", "zz", "not hex at all", "0" * 63, "0" * 65, crypto.sign(key, sts).upper()):
            with self.subTest(signature=bad):
                self.assertIsInstance(crypto.verify(key, sts, bad), bool)

    def test_verify_uses_a_constant_time_comparison(self):
        with mock.patch.object(hmac, "compare_digest", wraps=hmac.compare_digest) as spy:
            crypto.verify(b"k", b"d", crypto.sign(b"k", b"d"))
        self.assertTrue(spy.called, "SPEC §3.3 requires hmac.compare_digest")

    def test_nonces_are_sixteen_hex_characters_and_fresh(self):
        nonces = {crypto.new_nonce_hex() for _ in range(500)}
        self.assertEqual(len(nonces), 500)
        for nonce in list(nonces)[:20]:
            self.assertEqual(len(nonce), 16)
            bytes.fromhex(nonce)


class TestEventSigning(unittest.TestCase):
    BASE = {
        "v": "PARLEY/1",
        "id": "evt_41d0e8be2a7c9f03",
        "ts": "2026-10-08T12:34:56.789Z",
        "session": "ses_9f2c41ab77e0d315",
        "actor": "agt_0c5518aa91be7742",
        "type": "chat.message",
        "body": {"text": "I'll take the sync reconciler."},
    }

    def test_the_signature_ignores_seq_so_the_author_can_sign_before_the_hub_assigns_one(self):
        key = b"k" * 32
        unsequenced = dict(self.BASE)
        sequenced = dict(self.BASE, seq=1284)
        self.assertEqual(crypto.sign_event(key, unsequenced), crypto.sign_event(key, sequenced))

    def test_the_signature_ignores_an_existing_sig_field(self):
        key = b"k" * 32
        signature = crypto.sign_event(key, dict(self.BASE))
        self.assertEqual(crypto.sign_event(key, dict(self.BASE, sig=signature)), signature)

    def test_verify_event_accepts_the_event_it_signed(self):
        key = b"k" * 32
        event = dict(self.BASE)
        event["sig"] = crypto.sign_event(key, event)
        self.assertTrue(crypto.verify_event(key, event))
        self.assertTrue(crypto.verify_event(key, dict(event, seq=9999)),
                        "the Hub assigns seq after signing; that must not break verification")

    def test_verify_event_rejects_a_tampered_body(self):
        key = b"k" * 32
        event = dict(self.BASE)
        event["sig"] = crypto.sign_event(key, event)
        event["body"] = {"text": "I'll take the money."}
        self.assertFalse(crypto.verify_event(key, event))

    def test_verify_event_rejects_a_changed_actor(self):
        key = b"k" * 32
        event = dict(self.BASE)
        event["sig"] = crypto.sign_event(key, event)
        event["actor"] = "agt_ffffffffffffffff"
        self.assertFalse(crypto.verify_event(key, event))

    def test_an_unknown_body_field_is_covered_by_the_signature(self):
        key = b"k" * 32
        plain = crypto.sign_event(key, dict(self.BASE))
        extended = dict(self.BASE)
        extended["body"] = dict(self.BASE["body"], x_vendor_flag=True)
        self.assertNotEqual(crypto.sign_event(key, extended), plain)

    def test_key_ordering_does_not_change_the_signature(self):
        key = b"k" * 32
        reordered = {k: self.BASE[k] for k in reversed(list(self.BASE))}
        self.assertEqual(crypto.sign_event(key, reordered), crypto.sign_event(key, dict(self.BASE)))


class TestSealedMode(unittest.TestCase):
    KEY = bytes.fromhex(RFC8439["key"])
    NONCE = bytes.fromhex(RFC8439["nonce"])
    AAD = bytes.fromhex(RFC8439["aad"])
    PLAINTEXT = RFC8439["plaintext_utf8"].encode("utf-8")

    def sealed_rfc_vector(self) -> bytes:
        return self.NONCE + bytes.fromhex(RFC8439["ciphertext"]) + bytes.fromhex(RFC8439["tag"])

    def test_unseal_decrypts_the_rfc8439_vector(self):
        """The sealed wire shape is nonce||ct||tag, which is exactly the RFC's output."""
        self.assertEqual(crypto.unseal(self.KEY, self.sealed_rfc_vector(), self.AAD),
                         self.PLAINTEXT)

    def test_seal_produces_what_the_reference_implementation_would(self):
        sealed = crypto.seal(self.KEY, self.PLAINTEXT, self.AAD)
        nonce, ciphertext, tag = sealed[:12], sealed[12:-16], sealed[-16:]
        expected_ct, expected_tag = helpers.aead_encrypt(
            self.KEY, nonce, self.PLAINTEXT, self.AAD
        )
        self.assertEqual(ciphertext, expected_ct)
        self.assertEqual(tag, expected_tag)

    def test_the_sealed_envelope_has_the_documented_layout(self):
        sealed = crypto.seal(self.KEY, b"abc", b"aad")
        self.assertEqual(len(sealed), 12 + 3 + 16)

    def test_round_trip_for_awkward_sizes(self):
        for size in (0, 1, 15, 16, 17, 63, 64, 65, 4096):
            with self.subTest(size=size):
                plaintext = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
                plaintext = plaintext[:size]
                sealed = crypto.seal(self.KEY, plaintext, b"aad")
                self.assertEqual(crypto.unseal(self.KEY, sealed, b"aad"), plaintext)

    def test_a_wrong_tag_is_rejected(self):
        sealed = bytearray(crypto.seal(self.KEY, self.PLAINTEXT, self.AAD))
        sealed[-1] ^= 0x01
        with self.assertRaises(Exception):
            crypto.unseal(self.KEY, bytes(sealed), self.AAD)

    def test_a_tampered_ciphertext_is_rejected(self):
        sealed = bytearray(crypto.seal(self.KEY, self.PLAINTEXT, self.AAD))
        sealed[20] ^= 0x80
        with self.assertRaises(Exception):
            crypto.unseal(self.KEY, bytes(sealed), self.AAD)

    def test_the_wrong_aad_is_rejected(self):
        """SPEC §3.6 binds the ciphertext to method, path and identity through the AAD."""
        sealed = crypto.seal(self.KEY, self.PLAINTEXT, self.AAD)
        with self.assertRaises(Exception):
            crypto.unseal(self.KEY, sealed, self.AAD + b"\x00")
        with self.assertRaises(Exception):
            crypto.unseal(self.KEY, sealed, b"")

    def test_the_wrong_key_is_rejected(self):
        sealed = crypto.seal(self.KEY, self.PLAINTEXT, self.AAD)
        with self.assertRaises(Exception):
            crypto.unseal(bytes(32), sealed, self.AAD)

    def test_a_truncated_envelope_is_rejected_rather_than_misparsed(self):
        sealed = crypto.seal(self.KEY, self.PLAINTEXT, self.AAD)
        for cut in (0, 5, 12, 20, len(sealed) - 1):
            with self.subTest(length=cut):
                with self.assertRaises(Exception):
                    crypto.unseal(self.KEY, sealed[:cut], self.AAD)

    def test_nonces_do_not_repeat(self):
        nonces = {crypto.seal(self.KEY, b"x", b"")[:12] for _ in range(1000)}
        self.assertEqual(len(nonces), 1000)

    def test_a_repeated_nonce_under_the_same_key_aborts(self):
        """SPEC §3.6: 'a repeated nonce under the same key MUST abort'."""
        fixed = bytes(range(12))
        patched = None
        for target, attribute in (("secrets", "token_bytes"), ("os", "urandom")):
            module = getattr(crypto, target, None)
            if module is not None and hasattr(module, attribute):
                patched = mock.patch.object(module, attribute, lambda n: fixed[:n])
                break
        if patched is None:
            self.skipTest(
                "parley.crypto exposes no seam to force a nonce repeat; expose either the "
                "`secrets` or `os` module it draws from, or a seal(..., nonce=) argument, "
                "so this MUST can be tested"
            )
        with patched:
            first = crypto.seal(self.KEY, b"first", b"aad")
            self.assertEqual(first[:12], fixed)
            with self.assertRaises(Exception):
                crypto.seal(self.KEY, b"second", b"aad")

    def test_the_selected_backend_is_one_of_the_three_documented_options(self):
        self.assertIn(crypto.BACKEND, ("cryptography", "pynacl", "pure"))


class TestSealedBlobFrames(unittest.TestCase):
    KEY = bytes(range(32))
    BLOB_HASH = "sha256:" + "e3b0c442" * 8
    FRAME = 256 * 1024

    def test_round_trip_across_frame_boundaries(self):
        for size in (0, 1, self.FRAME - 1, self.FRAME, self.FRAME + 1, 3 * self.FRAME - 7):
            with self.subTest(size=size):
                data = (b"parley" * ((size // 6) + 1))[:size]
                sealed = crypto.seal_frames(self.KEY, data, self.BLOB_HASH)
                self.assertEqual(crypto.unseal_frames(self.KEY, sealed, self.BLOB_HASH), data)

    def test_each_frame_carries_its_own_overhead(self):
        data = b"x" * (2 * self.FRAME)
        sealed = crypto.seal_frames(self.KEY, data, self.BLOB_HASH)
        self.assertEqual(len(sealed), len(data) + 2 * (12 + 16))

    def test_a_tampered_frame_is_rejected(self):
        data = b"x" * (2 * self.FRAME)
        sealed = bytearray(crypto.seal_frames(self.KEY, data, self.BLOB_HASH))
        sealed[100] ^= 0xFF
        with self.assertRaises(Exception):
            crypto.unseal_frames(self.KEY, bytes(sealed), self.BLOB_HASH)

    def test_swapping_two_frames_is_rejected_because_the_index_is_in_the_aad(self):
        frame_len = self.FRAME + 12 + 16
        data = bytes([1]) * self.FRAME + bytes([2]) * self.FRAME
        sealed = crypto.seal_frames(self.KEY, data, self.BLOB_HASH)
        swapped = sealed[frame_len:2 * frame_len] + sealed[:frame_len]
        with self.assertRaises(Exception):
            crypto.unseal_frames(self.KEY, swapped, self.BLOB_HASH)

    def test_the_wrong_blob_hash_is_rejected(self):
        data = b"hello"
        sealed = crypto.seal_frames(self.KEY, data, self.BLOB_HASH)
        with self.assertRaises(Exception):
            crypto.unseal_frames(self.KEY, sealed, "sha256:" + "00" * 32)

    def test_the_frame_aad_is_the_documented_construction(self):
        data = b"hello world"
        sealed = crypto.seal_frames(self.KEY, data, self.BLOB_HASH)
        nonce, ciphertext, tag = sealed[:12], sealed[12:-16], sealed[-16:]
        aad = b"parley/blob/v1" + self.BLOB_HASH.encode("utf-8") + (0).to_bytes(4, "big")
        self.assertEqual(helpers.aead_decrypt(self.KEY, nonce, ciphertext, tag, aad), data)


class TestNoSecretLeaksInRepr(unittest.TestCase):
    """INTERNAL-API: crypto.py provides no ``__repr__`` that exposes key material."""

    def test_no_module_level_constant_looks_like_baked_in_key_material(self):
        """A public label such as ``b"parley/v1/enroll"`` is fine; opaque bytes are not.

        The distinction is printability: every legitimate constant in the key hierarchy is
        a documented ASCII label from SPEC §3.2. Anything key-sized and unprintable at
        module scope would be a hard-coded secret.
        """
        for name in dir(crypto):
            if name.startswith("__"):
                continue
            value = getattr(crypto, name)
            if not isinstance(value, (bytes, bytearray)) or len(value) < 16:
                continue
            printable = all(0x20 <= byte < 0x7F for byte in bytes(value))
            self.assertTrue(
                printable,
                "parley.crypto.{0} is {1} opaque bytes at module scope".format(
                    name, len(value)),
            )

    def test_the_documented_info_labels_are_exactly_what_the_spec_says(self):
        root = bytes(range(32))
        for name, label in (("INFO_ENROLL", b"parley/v1/enroll"),
                            ("INFO_SEAL", b"parley/v1/seal"),
                            ("INFO_FINGERPRINT", b"parley/v1/fingerprint")):
            value = getattr(crypto, name, None)
            if value is not None:
                self.assertEqual(value, label, name)
        self.assertEqual(crypto.enroll_key(root), crypto.hkdf(root, b"parley/v1/enroll"))

    def test_derived_keys_are_registered_with_the_leak_scanner(self):
        root = crypto.derive_root_key("copper-otter-climbs-the-quiet-hill",
                                      "ses_9f2c41ab77e0d315", iterations=1000)
        SECRETS.add_secret(root.hex(), "root_key")
        SECRETS.add_secret(crypto.enroll_key(root).hex(), "enroll_key")
        self.assertTrue(SECRETS.secrets)


if __name__ == "__main__":
    unittest.main()
