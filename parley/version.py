"""Version constants.

Kept in its own module with no imports so that anything -- including the
installer and ``parley doctor`` running on a Python too old to import the rest of
the package -- can read it safely.
"""
from __future__ import annotations

__version__ = "1.0.0"

#: The protocol identifier that appears in every event and every signature.
#: Bump this only for a wire-incompatible change; SPEC is versioned with it.
WIRE_VERSION = "PARLEY/1"

#: The oldest Python this implementation supports. See SPEC R2.
MIN_PYTHON = (3, 9)

__all__ = ["__version__", "WIRE_VERSION", "MIN_PYTHON"]
