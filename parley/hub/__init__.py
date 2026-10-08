"""The Parley Hub: the server side of a parley.

One Hub per parley.  It is the single ordering authority (it assigns ``seq``),
the conflict arbiter (SPEC §7.6), the blob store, and the origin that serves the
Deck.  Any participant can host one; nothing about it is privileged beyond being
the thing everyone happens to point at.

Typical use::

    from parley.hub import create_parley

    hub, watchword = create_parley(Path("."), name="Reconciler sprint")
    hub.start()
    print("invite:", watchword)          # shown exactly once
    print("deck:  ", hub.deck_url())

The submodules are deliberately separable: ``store`` is pure persistence,
``state`` is the materialised view, ``api`` is the router, ``server`` owns the
threads and the sockets, ``ratelimit`` and ``discovery`` are self-contained.
"""

from __future__ import annotations

from .api import authenticate, handle
from .discovery import DISCOVERY_PORT, DiscoveryResponder, discover
from .ratelimit import Limiter, TokenBucket
from .server import Hub, conflict_sidecar_path, create_parley, load_parley
from .state import StateView, color_hue
from .store import Store, normalise_blob_hash, wire_blob_hash

__all__ = [
    "Hub",
    "create_parley",
    "load_parley",
    "conflict_sidecar_path",
    "Store",
    "StateView",
    "color_hue",
    "normalise_blob_hash",
    "wire_blob_hash",
    "authenticate",
    "handle",
    "Limiter",
    "TokenBucket",
    "DiscoveryResponder",
    "discover",
    "DISCOVERY_PORT",
]
