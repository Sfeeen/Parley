"""LAN discovery responder: ``parley join --discover`` with nobody typing an IP.

A client broadcasts ``b"PARLEY/1 DISCOVER"`` to UDP 7778; every Hub on the
segment answers with a small JSON descriptor.  The reply carries the *verbal
fingerprint* but never the watchword, so discovering a Hub tells you it exists
and lets you confirm you joined the right one -- it does not let you join.

Defensive by design: UDP 7778 is a fixed port, so a second Hub on the same
machine will fail to bind.  That is a degraded feature, not a fatal error, so a
bind failure logs a warning and the responder simply stays off (R7).
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from typing import Callable, Optional

log = logging.getLogger("parley.hub.discovery")

DISCOVERY_PORT = 7778
PROBE = b"PARLEY/1 DISCOVER"
#: Longest reply we will ever send; keeps us inside one datagram on any MTU.
MAX_REPLY = 1200


def local_ip_towards(peer: str) -> str:
    """The address of the interface this host would use to reach ``peer``.

    Done with a connected UDP socket, which performs a route lookup without
    sending a packet.  This is the portable way to answer "what is my IP on the
    network this request came from" -- ``gethostbyname(gethostname())`` famously
    returns 127.0.1.1 on Debian.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((peer or "192.0.2.1", 9))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


class DiscoveryResponder:
    """A daemon thread answering discovery probes on UDP 7778."""

    def __init__(
        self,
        describe: Callable[[str], dict],
        *,
        port: int = DISCOVERY_PORT,
        bind: str = "",
    ) -> None:
        #: ``describe(peer_ip) -> {"session","name","url","fingerprint"}``.  Taking
        #: the peer lets the Hub advertise the URL that is reachable *from there*.
        self._describe = describe
        self._port = port
        self._bind = bind
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def active(self) -> bool:
        return self._sock is not None and not self._stop.is_set()

    def start(self) -> bool:
        """Bind and serve.  Returns False (with a warning logged) on a port clash."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(0.5)
            sock.bind((self._bind, self._port))
        except OSError as exc:
            log.warning(
                "LAN discovery disabled: cannot bind UDP %s:%d (%s). "
                "Another Parley Hub on this machine probably owns it; "
                "joiners will need the Hub URL typed in.",
                self._bind or "0.0.0.0",
                self._port,
                exc,
            )
            return False

        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve, name="parley-discovery", daemon=True
        )
        self._thread.start()
        log.info("LAN discovery responder listening on UDP %s:%d", self._bind or "0.0.0.0", self._port)
        return True

    def stop(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _serve(self) -> None:
        sock = self._sock
        assert sock is not None
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                log.debug("discovery socket error", exc_info=True)
                continue
            if data.strip() != PROBE:
                continue
            try:
                desc = self._describe(addr[0])
                payload = json.dumps(desc, separators=(",", ":")).encode("utf-8")
                if len(payload) > MAX_REPLY:
                    log.debug("discovery reply too large (%d bytes); skipped", len(payload))
                    continue
                sock.sendto(payload, addr)
            except Exception:
                log.debug("failed to answer discovery probe from %s", addr, exc_info=True)


def discover(timeout: float = 1.5, port: int = DISCOVERY_PORT) -> list:
    """Broadcast a probe and collect replies.  Here so the CLI and tests share it."""
    out = []
    seen = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.3)
        for target in ("255.255.255.255", "127.0.0.1"):
            try:
                sock.sendto(PROBE, (target, port))
            except OSError:
                continue
        import time as _time

        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                data, _addr = sock.recvfrom(MAX_REPLY + 64)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                desc = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            key = (desc.get("session"), desc.get("url"))
            if key in seen:
                continue
            seen.add(key)
            out.append(desc)
    finally:
        sock.close()
    return out
