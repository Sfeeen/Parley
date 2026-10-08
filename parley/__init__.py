"""Parley -- a protocol for autonomous agents to work on one project together.

The package layout mirrors the specification:

==========================  =============================================
``parley.version``          wire version and the Python floor
``parley.errors``           the one exception hierarchy (SPEC 12)
``parley.jsonutil``         canonical JSON, RFC 3339 time, atomic writes
``parley.ids``              identifier minting and checking (SPEC 1.1)
``parley.wordlist``         the 2048 spoken words
``parley.crypto``           key hierarchy, request signing, sealed bodies
``parley.protocol``         events, the PSR, and the workspace path boundary
``parley.config``           on-disk Hub descriptor and client credentials
``parley.hub``              the server
``parley.client``           the participant side
``parley.ledger``           contribution scoring (SPEC 9)
``parley.cli``              the ``parley`` command
==========================  =============================================

Only the modules above the line are imported here: they are the dependency root
and are cheap. The Hub, the client and the CLI are imported on demand so that a
one-line ``import parley`` does not drag in an HTTP server.

See ``docs/SPEC.md`` for the protocol and ``docs/SECURITY.md`` for the threat
model. The full reference implementation uses the standard library only; an
installed ``cryptography`` or ``PyNaCl`` is used as an AEAD accelerator if it
happens to be there, and never required.
"""
from __future__ import annotations

import sys

from .version import MIN_PYTHON, WIRE_VERSION, __version__

if sys.version_info < MIN_PYTHON:        # pragma: no cover - the whole point is to not run
    raise RuntimeError(
        "Parley needs Python %d.%d or newer; this is %d.%d. "
        "SPEC R2 sets the floor at 3.9."
        % (MIN_PYTHON[0], MIN_PYTHON[1], sys.version_info[0], sys.version_info[1])
    )

from . import config, crypto, errors, ids, jsonutil, protocol, wordlist  # noqa: E402
from .errors import ParleyError  # noqa: E402

__all__ = [
    "__version__", "WIRE_VERSION", "MIN_PYTHON", "ParleyError",
    "config", "crypto", "errors", "ids", "jsonutil", "protocol", "wordlist",
]
