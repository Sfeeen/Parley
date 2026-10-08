"""Parley's test suite.

Run it from the repository root with::

    python3 -m unittest discover -s tests -v

Stdlib ``unittest`` only — no pytest, no third-party anything, in keeping with SPEC R1.
``tests/helpers.py`` holds the shared fixtures and the independent reference
implementations the crypto tests check the real code against; it deliberately imports
nothing from ``parley`` at module scope so that it stays importable while the package is
still being built.
"""
