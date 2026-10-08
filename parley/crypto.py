"""Key derivation, request signing and the optional sealed-body AEAD.

This is the security core of Parley. Three things in here are load-bearing for
interoperability and must not drift:

* :func:`string_to_sign` -- the Hub and every client build these bytes
  independently and compare the resulting MAC. One extra newline and nobody can
  talk to anybody.
* :func:`seal_aad` -- the sealed-mode binding string of SPEC 3.6. Both ends
  build it independently and the Poly1305 tag is the only thing that notices a
  disagreement, so there is exactly one implementation of it, here.
* :func:`hkdf` and :func:`derive_root_key` -- the whole key hierarchy of
  SPEC 3.2 hangs off them.
* the ChaCha20-Poly1305 construction -- a sealed body produced by the pure
  Python path must open under ``cryptography`` and vice versa.

Nothing in this module keeps key material in an object with a ``__repr__``:
keys are passed as plain ``bytes`` and never stored, so a traceback cannot print
one. The nonce guard deliberately stores a digest of the key rather than the key.

**AEAD backend.** SPEC 3.6 requires trying a native accelerator first and
falling back to the bundled implementation, because pure-Python ChaCha20 is
roughly 1 MB/s and large blobs would crawl. Selection order is ``cryptography``,
then ``PyNaCl``, then ``pure``; set ``PARLEY_CRYPTO_BACKEND`` to one of
``cryptography`` / ``pynacl`` / ``pure`` to pin it (the test-suite pins ``pure``
to exercise the fallback on machines that have an accelerator installed). The
pure implementation is always importable as :func:`pure_aead_encrypt` /
:func:`pure_aead_decrypt` regardless of which backend is live.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import struct
import threading
import unicodedata
from collections import deque
from typing import Dict, Tuple

from .errors import BadSeal, CryptoFailure, NonceReuse
from .jsonutil import canonical
from .version import WIRE_VERSION
from .wordlist import WORDS

log = logging.getLogger("parley.crypto")

__all__ = [
    "BACKEND", "normalise_watchword", "generate_watchword", "derive_root_key",
    "hkdf", "hkdf_extract", "hkdf_expand", "enroll_key", "seal_key", "fingerprint",
    "string_to_sign", "sign", "verify", "sign_event", "verify_event",
    "seal_aad", "SEAL_AAD_PREFIX", "SEAL_AAD_RESPONSE_PREFIX",
    "seal", "unseal", "seal_frames", "unseal_frames", "new_nonce_hex",
    "pure_aead_encrypt", "pure_aead_decrypt", "reset_nonce_guard",
    "NONCE_BYTES", "TAG_BYTES", "BLOB_FRAME_BYTES",
]

NONCE_BYTES = 12            # RFC 8439 IETF variant
TAG_BYTES = 16
KEY_BYTES = 32
BLOB_FRAME_BYTES = 256 * 1024
BLOB_FRAME_AAD_PREFIX = b"parley/blob/v1"

#: Info strings for the three derived keys. These are part of the wire contract:
#: change one and existing sessions stop interoperating.
INFO_ENROLL = b"parley/v1/enroll"
INFO_SEAL = b"parley/v1/seal"
INFO_FINGERPRINT = b"parley/v1/fingerprint"

#: The filler word of SPEC 3.1. It carries no entropy; it is there so the
#: watchword reads as a sentence and is easier to say and remember.
FILLER_WORD = "the"


# ---------------------------------------------------------------------------
# HKDF (RFC 5869), SHA-256
# ---------------------------------------------------------------------------
# Validated against the RFC 5869 appendix A test vectors:
#
# A.1 (SHA-256, basic)
#   IKM  = 0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b  (22 bytes)
#   salt = 000102030405060708090a0b0c
#   info = f0f1f2f3f4f5f6f7f8f9
#   L    = 42
#   PRK  = 077709362c2e32df0ddc3f0dc47bba6390b6c73bb50f9c3122ec844ad7c2b3e5
#   OKM  = 3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf
#          34007208d5b887185865
#
# A.2 (SHA-256, longer inputs)
#   IKM  = 000102...4f   (80 bytes), salt = 606162...af (80), info = b0b1...ff (80)
#   L    = 82
#   PRK  = 06a6b88c5853361a06104c9ceb35b45cef760014904671014a193f40c15fc244
#   OKM  = b11e398dc80327a1c8e7f78c596a49344f012eda2d4efad8a050cc4c19afa97c
#          59045a99cac7827271cb41c65e590e09da3275600c2f09b8367793a9aca3db71
#          cc30c58179ec3e87c14c01d5c1f3434f1d87
#
# A.3 (SHA-256, zero-length salt and info)
#   IKM  = 0b * 22, salt = (empty), info = (empty), L = 42
#   PRK  = 19ef24a32c717b167f33a91d6f648bdf96596776afdb6377ac434c1c293ccb04
#   OKM  = 8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d
#          9d201395faa4b61a96c8
# ---------------------------------------------------------------------------

def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    """RFC 5869 step 1. An empty or absent salt means HashLen zero bytes."""
    if not salt:
        salt = b"\x00" * hashlib.sha256().digest_size
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 step 2."""
    hash_len = hashlib.sha256().digest_size
    if length < 0 or length > 255 * hash_len:
        raise ValueError("HKDF output length %d out of range" % length)
    okm = bytearray()
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return bytes(okm[:length])


def hkdf(key: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256 with an empty salt, which is all SPEC 3.2 ever needs.

    The salt is empty because the input keying material is already a PBKDF2
    output with the session id as its salt -- adding a second salt would buy
    nothing and would be one more thing two implementations could disagree on.
    """
    return hkdf_expand(hkdf_extract(b"", key), info, length)


# ---------------------------------------------------------------------------
# The watchword
# ---------------------------------------------------------------------------
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


def normalise_watchword(s: str) -> str:
    """SPEC 3.1 normalisation, so humans can retype the invite however they like.

    ``"Copper Otter Climbs the Quiet Hill"``, ``"copper otter climbs the quiet
    hill"`` and ``"COPPER-OTTER-CLIMBS-THE-QUIET-HILL"`` must all derive the same
    root key, or "it worked on my machine" becomes a support burden. Accents are
    stripped because a phone-dictated word may well arrive with one.
    """
    if not isinstance(s, str):
        raise TypeError("watchword must be a string")
    text = unicodedata.normalize("NFKD", s.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # NFKD can re-introduce uppercase (U+216B ROMAN NUMERAL TWELVE -> "XII"),
    # so fold once more before slugifying.
    text = _NON_SLUG_RE.sub("-", text.lower()).strip("-")
    return text


def generate_watchword(words: int = 5) -> str:
    """Draw a readable invite sentence.

    ``words`` is the number of *entropy-bearing* words; the literal ``the`` is
    inserted near the end so the result parses as a sentence out loud
    (``copper-otter-climbs-the-quiet-hill``) without inflating the entropy claim.
    Five words from a 2048-word list is 55 bits.
    """
    if not isinstance(words, int) or isinstance(words, bool):
        raise TypeError("words must be an int")
    if not 1 <= words <= 32:
        raise ValueError("words must be between 1 and 32, got %d" % words)
    drawn = [secrets.choice(WORDS) for _ in range(words)]
    if words >= 4:
        drawn.insert(words - 2, FILLER_WORD)
    elif words >= 2:
        drawn.insert(words - 1, FILLER_WORD)
    return "-".join(drawn)


def derive_root_key(watchword: str, session_id: str, *, iterations: int = 200_000) -> bytes:
    """PBKDF2-HMAC-SHA256 over the normalised watchword, salted by the session id.

    The session id is the salt (SPEC 3.2) so that the same watchword typed into
    two different parleys yields two unrelated root keys, and so that a
    precomputed table has to be built per session. ``iterations`` is recorded in
    the Hub's descriptor so it can be raised later without invalidating sessions
    created under the old count.
    """
    normalised = normalise_watchword(watchword)
    if not normalised:
        raise ValueError("watchword is empty after normalisation")
    if not session_id:
        raise ValueError("session id is required: it is the PBKDF2 salt")
    if iterations < 1000:
        raise ValueError("refusing to derive a root key with %d iterations" % iterations)
    return hashlib.pbkdf2_hmac("sha256", normalised.encode("utf-8"),
                               session_id.encode("utf-8"), iterations, dklen=KEY_BYTES)


def enroll_key(root_key: bytes) -> bytes:
    """The key that proves you know the watchword. Used only by ``POST /v1/enroll``."""
    return hkdf(root_key, INFO_ENROLL, KEY_BYTES)


def seal_key(root_key: bytes) -> bytes:
    """The body-encryption key for sealed mode (SPEC 3.6)."""
    return hkdf(root_key, INFO_SEAL, KEY_BYTES)


def fingerprint(root_key: bytes) -> str:
    """Three words two humans can read to each other to confirm one parley.

    Six bytes of HKDF output, taken 16 bits at a time and reduced into the
    wordlist. 2048 divides 65536 exactly, so the reduction is unbiased. This is a
    relay/MITM check, not a key: 33 bits of the 48 survive the mapping, which is
    ample for "are we both looking at the same Hub?" and is not claimed to be
    more (see ``docs/SECURITY.md``).
    """
    raw = hkdf(root_key, INFO_FINGERPRINT, 6)
    parts = struct.unpack(">3H", raw)
    return "-".join(WORDS[value % len(WORDS)] for value in parts)


# ---------------------------------------------------------------------------
# Request signing (SPEC 3.3)
# ---------------------------------------------------------------------------
def string_to_sign(method: str, path: str, body: bytes, ts: str,
                   nonce: str, session: str, agent: str) -> bytes:
    """Build the exact bytes SPEC 3.3 signs.

    Every component is pinned so that an attacker who captures a request cannot
    replay it against a different verb, path, session or agent, and cannot touch
    the body without invalidating the MAC. ``path`` must be the path *with* its
    query string, exactly as it will appear on the request line -- re-encoding it
    first is the classic way to make two implementations disagree.
    """
    if body is None:
        body = b""
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("body must be bytes; hash it yourself if you have a stream")
    parts = [
        WIRE_VERSION,
        method.upper(),
        path,
        hashlib.sha256(bytes(body)).hexdigest(),
        str(ts),
        nonce,
        session,
        agent,
    ]
    for part in parts:
        if "\n" in part:
            # A newline in a component would let an attacker shift the meaning of
            # every later line. Nothing legitimate contains one.
            raise ValueError("newline in a string-to-sign component")
    return "\n".join(parts).encode("utf-8")


def sign(key: bytes, sts: bytes) -> str:
    """Hex HMAC-SHA256. Hex rather than base64 because it survives every URL,
    header and log line without escaping."""
    return hmac.new(key, sts, hashlib.sha256).hexdigest()


def verify(key: bytes, sts: bytes, signature: str) -> bool:
    """Constant-time signature check. A malformed signature is simply false."""
    if not isinstance(signature, str):
        return False
    expected = sign(key, sts)
    candidate = signature.strip().lower()
    try:
        return hmac.compare_digest(expected, candidate)
    except TypeError:
        # compare_digest rejects non-ASCII str; that is a failed signature.
        return False


def _signable_event(event: dict) -> dict:
    """The event as it is signed: no ``seq`` (the Hub assigns it afterwards), no
    ``sig`` (that is the output), and no ``None`` values (SPEC 1.3 forbids them,
    and dropping them means an author and a verifier cannot disagree about
    whether an absent field was present-but-null)."""
    return {k: v for k, v in event.items() if k not in ("seq", "sig") and v is not None}


def sign_event(key: bytes, event: dict) -> str:
    """Author signature over an event (SPEC 2).

    Lets any participant verify who wrote an event without trusting the Hub to
    tell them -- which is the whole point of signing events separately from
    signing requests.
    """
    return sign(key, canonical(_signable_event(event)))


def verify_event(key: bytes, event: dict) -> bool:
    sig = event.get("sig")
    if not isinstance(sig, str):
        return False
    return verify(key, canonical(_signable_event(event)), sig)


def new_nonce_hex() -> str:
    """A fresh 64-bit request nonce (SPEC 3.3), rendered as 16 hex characters."""
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# ChaCha20-Poly1305, pure Python (RFC 8439)
# ---------------------------------------------------------------------------
_MASK32 = 0xFFFFFFFF
_CHACHA_CONSTANTS = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)

# The eight quarter-rounds of one double round: four columns then four diagonals.
_QUARTER_ROUNDS = (
    (0, 4, 8, 12), (1, 5, 9, 13), (2, 6, 10, 14), (3, 7, 11, 15),
    (0, 5, 10, 15), (1, 6, 11, 12), (2, 7, 8, 13), (3, 4, 9, 14),
)

_POLY1305_P = (1 << 130) - 5
_POLY1305_CLAMP = 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF


def _chacha20_block(key_words: Tuple[int, ...], counter: int,
                    nonce_words: Tuple[int, ...]) -> bytes:
    """One 64-byte ChaCha20 keystream block (RFC 8439 section 2.3).

    Fully unrolled into locals rather than indexing a 16-element list. That is
    not premature optimisation: this function is called once per 64 bytes of
    every sealed body, and the list-indexed version runs at roughly a third of
    the speed. The structure is exactly the reference double round -- four
    column quarter-rounds then four diagonal ones, ten times.
    """
    M = _MASK32
    x0, x1, x2, x3 = _CHACHA_CONSTANTS
    x4, x5, x6, x7, x8, x9, x10, x11 = key_words
    x13, x14, x15 = nonce_words
    x12 = counter & M
    s0, s1, s2, s3 = x0, x1, x2, x3
    s4, s5, s6, s7, s8, s9, s10, s11 = key_words
    s12, s13, s14, s15 = x12, x13, x14, x15

    for _ in range(10):                      # 10 double rounds == 20 rounds
        # column round
        x0 = (x0 + x4) & M; x12 ^= x0; x12 = ((x12 << 16) | (x12 >> 16)) & M
        x8 = (x8 + x12) & M; x4 ^= x8; x4 = ((x4 << 12) | (x4 >> 20)) & M
        x0 = (x0 + x4) & M; x12 ^= x0; x12 = ((x12 << 8) | (x12 >> 24)) & M
        x8 = (x8 + x12) & M; x4 ^= x8; x4 = ((x4 << 7) | (x4 >> 25)) & M
        x1 = (x1 + x5) & M; x13 ^= x1; x13 = ((x13 << 16) | (x13 >> 16)) & M
        x9 = (x9 + x13) & M; x5 ^= x9; x5 = ((x5 << 12) | (x5 >> 20)) & M
        x1 = (x1 + x5) & M; x13 ^= x1; x13 = ((x13 << 8) | (x13 >> 24)) & M
        x9 = (x9 + x13) & M; x5 ^= x9; x5 = ((x5 << 7) | (x5 >> 25)) & M
        x2 = (x2 + x6) & M; x14 ^= x2; x14 = ((x14 << 16) | (x14 >> 16)) & M
        x10 = (x10 + x14) & M; x6 ^= x10; x6 = ((x6 << 12) | (x6 >> 20)) & M
        x2 = (x2 + x6) & M; x14 ^= x2; x14 = ((x14 << 8) | (x14 >> 24)) & M
        x10 = (x10 + x14) & M; x6 ^= x10; x6 = ((x6 << 7) | (x6 >> 25)) & M
        x3 = (x3 + x7) & M; x15 ^= x3; x15 = ((x15 << 16) | (x15 >> 16)) & M
        x11 = (x11 + x15) & M; x7 ^= x11; x7 = ((x7 << 12) | (x7 >> 20)) & M
        x3 = (x3 + x7) & M; x15 ^= x3; x15 = ((x15 << 8) | (x15 >> 24)) & M
        x11 = (x11 + x15) & M; x7 ^= x11; x7 = ((x7 << 7) | (x7 >> 25)) & M
        # diagonal round
        x0 = (x0 + x5) & M; x15 ^= x0; x15 = ((x15 << 16) | (x15 >> 16)) & M
        x10 = (x10 + x15) & M; x5 ^= x10; x5 = ((x5 << 12) | (x5 >> 20)) & M
        x0 = (x0 + x5) & M; x15 ^= x0; x15 = ((x15 << 8) | (x15 >> 24)) & M
        x10 = (x10 + x15) & M; x5 ^= x10; x5 = ((x5 << 7) | (x5 >> 25)) & M
        x1 = (x1 + x6) & M; x12 ^= x1; x12 = ((x12 << 16) | (x12 >> 16)) & M
        x11 = (x11 + x12) & M; x6 ^= x11; x6 = ((x6 << 12) | (x6 >> 20)) & M
        x1 = (x1 + x6) & M; x12 ^= x1; x12 = ((x12 << 8) | (x12 >> 24)) & M
        x11 = (x11 + x12) & M; x6 ^= x11; x6 = ((x6 << 7) | (x6 >> 25)) & M
        x2 = (x2 + x7) & M; x13 ^= x2; x13 = ((x13 << 16) | (x13 >> 16)) & M
        x8 = (x8 + x13) & M; x7 ^= x8; x7 = ((x7 << 12) | (x7 >> 20)) & M
        x2 = (x2 + x7) & M; x13 ^= x2; x13 = ((x13 << 8) | (x13 >> 24)) & M
        x8 = (x8 + x13) & M; x7 ^= x8; x7 = ((x7 << 7) | (x7 >> 25)) & M
        x3 = (x3 + x4) & M; x14 ^= x3; x14 = ((x14 << 16) | (x14 >> 16)) & M
        x9 = (x9 + x14) & M; x4 ^= x9; x4 = ((x4 << 12) | (x4 >> 20)) & M
        x3 = (x3 + x4) & M; x14 ^= x3; x14 = ((x14 << 8) | (x14 >> 24)) & M
        x9 = (x9 + x14) & M; x4 ^= x9; x4 = ((x4 << 7) | (x4 >> 25)) & M
    return struct.pack("<16I", (x0 + s0) & M, (x1 + s1) & M, (x2 + s2) & M, (x3 + s3) & M, (x4 + s4) & M, (x5 + s5) & M, (x6 + s6) & M, (x7 + s7) & M, (x8 + s8) & M, (x9 + s9) & M, (x10 + s10) & M, (x11 + s11) & M, (x12 + s12) & M, (x13 + s13) & M, (x14 + s14) & M, (x15 + s15) & M)


def _chacha20_xor(key: bytes, counter: int, nonce: bytes, data: bytes) -> bytes:
    """XOR ``data`` with the ChaCha20 keystream. Encryption and decryption both."""
    if len(key) != KEY_BYTES:
        raise CryptoFailure("ChaCha20 key must be 32 bytes, got %d" % len(key))
    if len(nonce) != NONCE_BYTES:
        raise CryptoFailure("ChaCha20 nonce must be 12 bytes, got %d" % len(nonce))
    key_words = struct.unpack("<8I", key)
    nonce_words = struct.unpack("<3I", nonce)
    # XOR a whole 64-byte block at a time as one big integer. Byte-by-byte is the
    # obvious way to write this and is about four times slower, which matters:
    # this loop is the entire cost of sealed mode.
    pieces = []
    for index, offset in enumerate(range(0, len(data), 64)):
        block = _chacha20_block(key_words, counter + index, nonce_words)
        chunk = data[offset:offset + 64]
        size = len(chunk)
        mixed = int.from_bytes(chunk, "big") ^ int.from_bytes(block[:size], "big")
        pieces.append(mixed.to_bytes(size, "big"))
    return b"".join(pieces)


def _poly1305_mac(message: bytes, one_time_key: bytes) -> bytes:
    """Poly1305 (RFC 8439 section 2.5).

    The clamp on ``r`` is not optional decoration: it is what keeps the
    multiplications inside the field and the security proof intact. Each block is
    read little-endian with an extra 0x01 byte appended *after* the block's own
    bytes -- for a short final block that byte lands immediately after the data,
    not at offset 16.
    """
    if len(one_time_key) != 32:
        raise CryptoFailure("Poly1305 key must be 32 bytes, got %d" % len(one_time_key))
    r = int.from_bytes(one_time_key[:16], "little") & _POLY1305_CLAMP
    s = int.from_bytes(one_time_key[16:], "little")
    acc = 0
    for offset in range(0, len(message), 16):
        block = message[offset:offset + 16]
        acc = ((acc + int.from_bytes(block + b"\x01", "little")) * r) % _POLY1305_P
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _pad16(data: bytes) -> bytes:
    """RFC 8439's pad16(): zeros up to the next 16-byte boundary, nothing if already aligned."""
    remainder = len(data) % 16
    return b"" if remainder == 0 else b"\x00" * (16 - remainder)


def _aead_mac_data(aad: bytes, ciphertext: bytes) -> bytes:
    """The exact byte string Poly1305 covers in the AEAD construction.

    Both the AAD and the ciphertext are zero-padded to a 16-byte boundary and
    then their *true* lengths are appended as little-endian 64-bit integers.
    Without those trailing lengths an attacker could shift bytes between the AAD
    and the ciphertext and keep the tag valid.
    """
    return (aad + _pad16(aad) + ciphertext + _pad16(ciphertext)
            + struct.pack("<Q", len(aad)) + struct.pack("<Q", len(ciphertext)))


def pure_aead_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """Pure-Python ChaCha20-Poly1305 AEAD. Returns ``ciphertext || tag``.

    Validated against the RFC 8439 section 2.8.2 test vector:
        key   = 808182...9f (32 bytes, 0x80..0x9f)
        nonce = 070000004041424344454647
        aad   = 50515253c0c1c2c3c4c5c6c7
        plaintext  = "Ladies and Gentlemen of the class of '99: If I could offer
                      you only one tip for the future, sunscreen would be it."
        ciphertext = d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736
                     ee62d63dbea45e8ca9671282fafb69da92728b1a71de0a9e060b2905d6
                     a5b67ecd3b3692ddbd7f2d778b8c9803aee328091b58fab324e4fad675
                     945585808b4831d7bc3ff4def08e4b7a9de576d26586cec64b6116
        tag        = 1ae10b594f09e26a7e902ecbd0600691
    """
    _check_key(key)
    _check_nonce(nonce)
    one_time_key = _chacha20_block(struct.unpack("<8I", key), 0,
                                   struct.unpack("<3I", nonce))[:32]
    ciphertext = _chacha20_xor(key, 1, nonce, plaintext)
    tag = _poly1305_mac(_aead_mac_data(aad, ciphertext), one_time_key)
    return ciphertext + tag


def pure_aead_decrypt(key: bytes, nonce: bytes, data: bytes, aad: bytes) -> bytes:
    """Inverse of :func:`pure_aead_encrypt`; raises :class:`BadSeal` on any mismatch."""
    _check_key(key)
    _check_nonce(nonce)
    if len(data) < TAG_BYTES:
        raise BadSeal("sealed body is shorter than its authentication tag")
    ciphertext, tag = data[:-TAG_BYTES], data[-TAG_BYTES:]
    one_time_key = _chacha20_block(struct.unpack("<8I", key), 0,
                                   struct.unpack("<3I", nonce))[:32]
    expected = _poly1305_mac(_aead_mac_data(aad, ciphertext), one_time_key)
    if not hmac.compare_digest(expected, tag):
        raise BadSeal("sealed body failed authentication",
                      hint="Wrong watchword, wrong seal mode, or the body was altered.")
    return _chacha20_xor(key, 1, nonce, ciphertext)


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------
def _load_cryptography():
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    def encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
        return ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)

    def decrypt(key: bytes, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        try:
            return ChaCha20Poly1305(key).decrypt(nonce, data, aad)
        except Exception as exc:                 # InvalidTag and friends
            raise BadSeal("sealed body failed authentication",
                          hint="Wrong watchword, wrong seal mode, or the body was "
                               "altered.") from exc

    return encrypt, decrypt


def _load_pynacl():
    from nacl.bindings import (                              # type: ignore
        crypto_aead_chacha20poly1305_ietf_decrypt as _dec,
        crypto_aead_chacha20poly1305_ietf_encrypt as _enc,
    )

    def encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
        return _enc(plaintext, aad, nonce, key)

    def decrypt(key: bytes, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        try:
            return _dec(data, aad, nonce, key)
        except Exception as exc:
            raise BadSeal("sealed body failed authentication",
                          hint="Wrong watchword, wrong seal mode, or the body was "
                               "altered.") from exc

    return encrypt, decrypt


def _select_backend() -> Tuple[str, object, object]:
    """Pick the AEAD implementation, honouring an explicit pin.

    A pin that cannot be satisfied is an error rather than a silent downgrade:
    if an operator asked for the accelerator, quietly running 100x slower is not
    a kindness.
    """
    requested = (os.environ.get("PARLEY_CRYPTO_BACKEND") or "auto").strip().lower()
    loaders = (("cryptography", _load_cryptography), ("pynacl", _load_pynacl))

    if requested == "pure":
        return "pure", pure_aead_encrypt, pure_aead_decrypt
    for name, loader in loaders:
        if requested not in ("auto", name):
            continue
        try:
            encrypt, decrypt = loader()
            return name, encrypt, decrypt
        except Exception:                        # not installed, or broken install
            if requested == name:
                raise CryptoFailure(
                    "PARLEY_CRYPTO_BACKEND=%s was requested but could not be loaded" % name,
                    hint="Install it, or unset PARLEY_CRYPTO_BACKEND to fall back.")
    if requested not in ("auto", "cryptography", "pynacl", "pure"):
        raise CryptoFailure("unknown PARLEY_CRYPTO_BACKEND %r" % requested,
                            hint="Use cryptography, pynacl, pure, or leave it unset.")
    return "pure", pure_aead_encrypt, pure_aead_decrypt


BACKEND, _aead_encrypt, _aead_decrypt = _select_backend()
log.debug("AEAD backend selected: %s", BACKEND)


# ---------------------------------------------------------------------------
# Nonce reuse guard
# ---------------------------------------------------------------------------
class _NonceGuard:
    """Refuses to seal twice with the same (key, nonce) pair.

    With 96 random bits a collision is not something that happens by chance; it
    happens when a process forks, a VM is restored from a snapshot, or someone
    "helpfully" makes the nonce deterministic. Any of those silently destroys
    ChaCha20-Poly1305, so the check is cheap insurance.

    Memory is bounded per key: a FIFO of the most recent nonces. Evicting old
    entries weakens the guarantee only for sessions that have sealed more than
    ``capacity`` messages, where a birthday collision is still astronomically
    unlikely. Keys are identified by a digest so no key material is retained.
    """

    def __init__(self, capacity: int = 1 << 17) -> None:
        self._capacity = capacity
        self._lock = threading.Lock()
        self._seen: Dict[bytes, set] = {}
        self._order: Dict[bytes, deque] = {}

    @staticmethod
    def _key_id(key: bytes) -> bytes:
        return hashlib.sha256(b"parley/nonce-guard/v1" + key).digest()[:16]

    def claim(self, key: bytes, nonce: bytes) -> None:
        key_id = self._key_id(key)
        with self._lock:
            seen = self._seen.setdefault(key_id, set())
            order = self._order.setdefault(key_id, deque())
            if nonce in seen:
                raise NonceReuse(
                    "refusing to seal: this nonce was already used with this key",
                    hint="A duplicate nonce breaks ChaCha20-Poly1305 outright. "
                         "Restart the process; do not retry.")
            seen.add(nonce)
            order.append(nonce)
            while len(order) > self._capacity:
                seen.discard(order.popleft())

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()
            self._order.clear()


_NONCE_GUARD = _NonceGuard()


def reset_nonce_guard() -> None:
    """Forget every observed nonce. For tests only; never call this in a session."""
    _NONCE_GUARD.reset()


# ---------------------------------------------------------------------------
# Sealed bodies (SPEC 3.6)
# ---------------------------------------------------------------------------
#: The two AAD domain separators of SPEC 3.6, named once here so that no call
#: site ever spells one out: "PARLEY/1-SEAL" and "PARLEY/1-SEAL-RESPONSE".
SEAL_AAD_PREFIX = WIRE_VERSION + "-SEAL"
SEAL_AAD_RESPONSE_PREFIX = WIRE_VERSION + "-SEAL-RESPONSE"


def seal_aad(method: str, path: str, ts: str, nonce: str, session: str, agent: str,
             *, response: bool = False) -> bytes:
    """Build the exact AAD bytes SPEC 3.6 binds a sealed body to.

    This is deliberately *not* :func:`string_to_sign`. The §3.3 string embeds
    ``sha256(raw_request_body)``, and in sealed mode the raw body is the output
    of the very AEAD call this AAD feeds -- so "the AAD is the string-to-sign"
    would be circular and unimplementable. §3.6 therefore defines the same
    identity binding with the body hash removed: version-and-direction, method,
    path-with-query, timestamp, nonce, session, agent, newline-joined, UTF-8.

    ``ts`` and ``nonce`` are always the **request's** values, in both
    directions. A response uses the ``-RESPONSE`` prefix over those same values,
    which binds each response to the exact request that produced it and makes a
    response body unusable as a request body (and vice versa).

    ``agent`` is the literal ``"enroll"`` during enrolment -- the one case where
    the sealing key (``seal_key``) and the signing key (``enroll_key``) differ,
    and historically the first place two implementations drifted.

    Both ends MUST call this one function. SPEC 3.6 forbids trying several AADs
    and accepting whichever authenticates: a wrong AAD does fail the Poly1305
    tag, but probing turns a loud interoperability failure into a silent one and
    lets two implementations drift apart permanently.
    """
    parts = [
        SEAL_AAD_RESPONSE_PREFIX if response else SEAL_AAD_PREFIX,
        method.upper(),
        path,
        str(ts),
        nonce,
        session,
        agent,
    ]
    for part in parts:
        if "\n" in part:
            # Same reasoning as string_to_sign: a newline inside a component
            # would let an attacker re-interpret every line after it.
            raise ValueError("newline in a seal-AAD component")
    return "\n".join(parts).encode("utf-8")


def seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """Encrypt a body. Returns ``nonce(12) || ciphertext || tag(16)``.

    The caller supplies the AAD, which for a request or response body is
    :func:`seal_aad`: that binds the ciphertext to the method, path, timestamp,
    nonce, session and agent, so a sealed body cannot be lifted out of one
    request and dropped into another.
    """
    _check_key(key)
    nonce = secrets.token_bytes(NONCE_BYTES)
    _NONCE_GUARD.claim(key, nonce)
    return nonce + _aead_encrypt(key, nonce, plaintext, aad)


def unseal(key: bytes, sealed: bytes, aad: bytes) -> bytes:
    """Decrypt and authenticate a body produced by :func:`seal`."""
    _check_key(key)
    if not isinstance(sealed, (bytes, bytearray)):
        raise BadSeal("sealed body must be bytes")
    sealed = bytes(sealed)
    if len(sealed) < NONCE_BYTES + TAG_BYTES:
        raise BadSeal("sealed body is too short to contain a nonce and a tag",
                      hint="Is X-Parley-Seal set on a body that was never sealed?")
    return _aead_decrypt(key, sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], aad)


def _blob_frame_aad(blob_hash: str, index: int) -> bytes:
    """AAD for one blob frame: prefix, the blob's canonical id, and the frame number.

    Including the index is what stops an attacker from reordering, duplicating or
    dropping frames while every individual frame still authenticates.
    """
    digest = blob_hash.strip().lower()
    if not digest.startswith("sha256:"):
        digest = "sha256:" + digest
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise CryptoFailure("blob hash is not a sha256 digest: %r" % blob_hash)
    return BLOB_FRAME_AAD_PREFIX + digest.encode("ascii") + struct.pack(">I", index)


def seal_frames(key: bytes, data: bytes, blob_hash: str) -> bytes:
    """Seal a blob as independent 256 KiB frames (SPEC 3.6).

    Framing exists so a large blob can be streamed and verified incrementally
    instead of being held in memory twice. An empty blob still produces one
    frame, so even "nothing" is authenticated.
    """
    _check_key(key)
    out = bytearray()
    index = 0
    offset = 0
    while True:
        chunk = data[offset:offset + BLOB_FRAME_BYTES]
        nonce = secrets.token_bytes(NONCE_BYTES)
        _NONCE_GUARD.claim(key, nonce)
        out += nonce + _aead_encrypt(key, nonce, chunk, _blob_frame_aad(blob_hash, index))
        offset += BLOB_FRAME_BYTES
        index += 1
        if offset >= len(data):
            break
    return bytes(out)


def unseal_frames(key: bytes, data: bytes, blob_hash: str) -> bytes:
    """Inverse of :func:`seal_frames`.

    Frame boundaries are implicit: every frame but the last is exactly
    ``12 + 262144 + 16`` bytes, so the split is unambiguous without a length
    prefix an attacker could lie about.
    """
    _check_key(key)
    full_frame = NONCE_BYTES + BLOB_FRAME_BYTES + TAG_BYTES
    out = bytearray()
    offset = 0
    index = 0
    if len(data) < NONCE_BYTES + TAG_BYTES:
        raise BadSeal("sealed blob is too short to contain even one frame")
    while offset < len(data):
        frame = data[offset:offset + full_frame]
        if len(frame) < NONCE_BYTES + TAG_BYTES:
            raise BadSeal("truncated frame %d in sealed blob" % index)
        out += _aead_decrypt(key, frame[:NONCE_BYTES], frame[NONCE_BYTES:],
                             _blob_frame_aad(blob_hash, index))
        offset += full_frame
        index += 1
    return bytes(out)


def _check_key(key: bytes) -> None:
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
        raise CryptoFailure("seal key must be exactly %d bytes" % KEY_BYTES)


def _check_nonce(nonce: bytes) -> None:
    if not isinstance(nonce, (bytes, bytearray)) or len(nonce) != NONCE_BYTES:
        raise CryptoFailure("nonce must be exactly %d bytes" % NONCE_BYTES)
