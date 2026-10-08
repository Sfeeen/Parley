"""End to end: a real Hub on a loopback port and two real clients.

Everything else in this suite tests a layer. This tests the *claim*: two agents, two
separate workspace directories, one parley, and a file written by one appears byte-identical
in the other's workspace. It is slower than the rest and it touches sockets, so it is kept
in its own module and skips — with a message that says what is missing — when a layer it
needs has not landed yet.

Sync is driven deterministically (``scan_once`` / ``apply_event``) rather than by starting
the background poller and sleeping. The property under test is "the bytes arrive", not "the
bytes arrive within 2 seconds", and a test that waits on a timer is a test that will one day
fail on a loaded CI box for no reason.
"""

from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from pathlib import Path

from tests.helpers import free_port, require, wait_until


def _layers():
    """Import everything the suite needs, or skip with a precise message."""
    server = require("parley.hub.server")
    client = require("parley.client.client")
    sync = require("parley.client.sync")
    config = require("parley.config")
    return server, client, sync, config


class IntegrationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_mod, cls.client_mod, cls.sync_mod, cls.config_mod = _layers()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.hub_workspace = root / "hub"
        self.ws_a = root / "ada"
        self.ws_b = root / "bob"
        for path in (self.hub_workspace, self.ws_a, self.ws_b):
            path.mkdir(parents=True)

        self.port = free_port()
        self.hub, self.watchword = self.server_mod.create_parley(
            self.hub_workspace, name="integration", port=self.port, bind="127.0.0.1"
        )
        self.hub.start()
        self.addCleanup(self._stop_hub)
        self.hub_url = "http://127.0.0.1:{0}".format(self.hub.port)
        self.state_dir = Path(self.hub.state_dir)

        wait_until(self._hub_is_up, timeout=15.0, message="the Hub never started listening")

        self.client_a = self.join(self.ws_a, "Ada")
        self.client_b = self.join(self.ws_b, "Bob")
        self.sync_a = self.sync_mod.WorkspaceSync(self.client_a, self.ws_a, poll_ms=50)
        self.sync_b = self.sync_mod.WorkspaceSync(self.client_b, self.ws_b, poll_ms=50)
        self.cursor = {"a": 0, "b": 0}

    # ------------------------------------------------------------- plumbing

    def _hub_is_up(self):
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(self.hub_url + "/v1/hello", timeout=2.0) as response:
                return response.getcode() == 200
        except (urllib.error.URLError, OSError):
            return False

    def _stop_hub(self):
        try:
            self.hub.stop()
        except Exception:
            pass

    def join(self, workspace, name):
        client = self.client_mod.ParleyClient.enroll(
            self.hub_url, self.watchword, workspace, name=name, kind="integration-test"
        )
        self.addCleanup(self._bye, client)
        return client

    @staticmethod
    def _bye(client):
        try:
            client.bye("test over")
        except Exception:
            pass

    def pump(self, who):
        """Deliver every event the Hub has to one side's sync engine."""
        client = self.client_a if who == "a" else self.client_b
        sync = self.sync_a if who == "a" else self.sync_b
        delivered = 0
        while True:
            events = client.events(since=self.cursor[who], limit=500)
            if not events:
                break
            for event in events:
                self.cursor[who] = max(self.cursor[who], int(event.get("seq", 0)))
                if str(event.get("type", "")).startswith("file."):
                    sync.apply_event(event)
                delivered += 1
        return delivered

    def push(self, who):
        """Ship one side's local changes, waiting out the SPEC §7.3 debounce.

        §7.3 deliberately holds a file back for 400 ms so a half-written one is never
        shipped, so a scan immediately after a write correctly returns nothing. Polling
        for the change is the honest way to express "eventually it goes up"; sleeping a
        fixed 500 ms and hoping is not.
        """
        sync = self.sync_a if who == "a" else self.sync_b
        return wait_until(
            lambda: sync.scan_once() or None, timeout=15.0,
            message="scan_once() on {0}'s workspace never emitted the local change "
                    "(SPEC §7.3 debounce is 400 ms)".format(who),
        )

    def push_quietly(self, who):
        """Scan without demanding a change — for asserting that nothing was shipped."""
        sync = self.sync_a if who == "a" else self.sync_b
        return sync.scan_once()

    def settle(self, *sides):
        """Push the named sides' local changes up, then pull everything down to both."""
        for who in (sides or ("a",)):
            self.push(who)
        self.pump("a")
        self.pump("b")

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class TestTwoAgentsShareAWorkspace(IntegrationTestCase):
    def test_both_agents_joined_the_same_parley(self):
        state = self.client_a.state()
        names = {agent["name"] for agent in state["agents"]}
        self.assertEqual({"Ada", "Bob"}, names & {"Ada", "Bob"})

    def test_both_agents_see_the_same_fingerprint(self):
        creds_a = self.config_mod.Credentials.load(self.ws_a)
        creds_b = self.config_mod.Credentials.load(self.ws_b)
        self.assertEqual(creds_a.fingerprint, creds_b.fingerprint)
        self.assertEqual(creds_a.fingerprint, self.hub.config.fingerprint)
        self.assertEqual(len(creds_a.fingerprint.split("-")), 3)

    def test_credentials_are_written_to_each_workspace(self):
        for workspace in (self.ws_a, self.ws_b):
            self.assertTrue((workspace / ".parley" / "credentials.json").is_file())

    def test_a_file_created_by_ada_appears_byte_identical_in_bobs_workspace(self):
        payload = b"# shared\nprint('hello from Ada')\n\xe2\x98\x83\n"
        (self.ws_a / "shared.py").write_bytes(payload)
        self.settle()
        arrived = self.ws_b / "shared.py"
        self.assertTrue(arrived.is_file(), "the file never reached Bob's workspace")
        self.assertEqual(arrived.read_bytes(), payload)
        self.assertEqual(self.digest(arrived), self.digest(self.ws_a / "shared.py"))

    def test_a_nested_file_arrives_with_its_directories(self):
        (self.ws_a / "pkg" / "sub").mkdir(parents=True)
        (self.ws_a / "pkg" / "sub" / "mod.py").write_bytes(b"x = 1\n")
        self.settle()
        self.assertEqual((self.ws_b / "pkg" / "sub" / "mod.py").read_bytes(), b"x = 1\n")

    def test_a_binary_file_arrives_unmangled(self):
        payload = bytes(range(256)) * 64
        (self.ws_a / "blob.bin").write_bytes(payload)
        self.settle()
        self.assertEqual((self.ws_b / "blob.bin").read_bytes(), payload)

    def test_an_edit_propagates(self):
        (self.ws_a / "notes.md").write_bytes(b"first\n")
        self.settle()
        (self.ws_a / "notes.md").write_bytes(b"second\n")
        self.settle()
        self.assertEqual((self.ws_b / "notes.md").read_bytes(), b"second\n")

    def test_ignored_paths_never_leave_the_workspace(self):
        (self.ws_a / "__pycache__").mkdir()
        (self.ws_a / "__pycache__" / "x.pyc").write_bytes(b"\x00compiled")
        (self.ws_a / ".DS_Store").write_bytes(b"junk")
        (self.ws_a / "real.py").write_bytes(b"this one should travel\n")
        self.settle()
        self.assertTrue((self.ws_b / "real.py").is_file(), "the control file never arrived")
        self.assertFalse((self.ws_b / "__pycache__").exists())
        self.assertFalse((self.ws_b / ".DS_Store").exists())

    def test_the_credential_file_is_never_synced(self):
        (self.ws_a / "real.py").write_bytes(b"control\n")
        self.settle()
        self.assertNotEqual(
            (self.ws_b / ".parley" / "credentials.json").read_bytes(),
            (self.ws_a / ".parley" / "credentials.json").read_bytes(),
            "Ada's agent key must never reach Bob's workspace",
        )


class TestChatFlowsBothWays(IntegrationTestCase):
    def texts(self, client, since=0):
        return [e["body"].get("text") for e in client.events(since=since, limit=500)
                if e["type"] == "chat.message"]

    def test_a_message_from_ada_reaches_bob(self):
        self.client_a.say("I'll take the sync reconciler.")
        self.assertIn("I'll take the sync reconciler.",
                      wait_until(lambda: self.texts(self.client_b) or None, timeout=10.0,
                                 message="Ada's message never reached Bob"))

    def test_a_message_from_bob_reaches_ada(self):
        self.client_b.say("Then I'll take the Deck.")
        self.assertIn("Then I'll take the Deck.",
                      wait_until(lambda: self.texts(self.client_a) or None, timeout=10.0,
                                 message="Bob's message never reached Ada"))

    def test_a_reply_carries_its_thread_pointer(self):
        first = self.client_a.say("Which transport?")
        self.client_b.say("SSE.", reply_to=first["id"])
        replies = [e for e in self.client_a.events(limit=500)
                   if e["type"] == "chat.message" and e["body"].get("reply_to")]
        self.assertTrue(replies)
        self.assertEqual(replies[-1]["body"]["reply_to"], first["id"])

    def test_unicode_survives_the_round_trip(self):
        text = "héllo ☃ — naïve 日本語"
        self.client_a.say(text)
        self.assertIn(text, wait_until(lambda: self.texts(self.client_b) or None,
                                       timeout=10.0, message="unicode chat never arrived"))


class TestStandingReportsAreVisible(IntegrationTestCase):
    def test_a_psr_from_ada_shows_up_in_bobs_state_snapshot(self):
        self.client_a.status("Wiring the SSE reconnect backoff", state="working",
                             focus=["parley/client/client.py"], progress=0.4)

        def ada_psr():
            for agent in self.client_b.state().get("agents", []):
                if agent.get("name") == "Ada" and agent.get("psr"):
                    return agent["psr"]
            return None

        psr = wait_until(ada_psr, timeout=10.0,
                         message="Ada's PSR never appeared in Bob's snapshot")
        self.assertEqual(psr["headline"], "Wiring the SSE reconnect backoff")
        self.assertEqual(psr["state"], "working")
        self.assertEqual(psr["focus"], ["parley/client/client.py"])
        self.assertAlmostEqual(psr["progress"], 0.4, places=6)

    def test_a_contribution_from_ada_scores_on_the_ledger_bob_can_see(self):
        self.client_a.know("SSE beats WebSockets here", "decision",
                           detail="Survives corporate proxies; stdlib-implementable.")

        def ada_line():
            ledger = self.client_b.state().get("ledger") or {}
            for line in ledger.get("lines", []):
                if line.get("name") == "Ada" and line.get("total", 0) > 0:
                    return line
            return None

        line = wait_until(ada_line, timeout=10.0,
                          message="Ada's contribution never reached the Ledger")
        self.assertGreaterEqual(line["components"]["contributions"], 8.0)
        self.assertTrue(line["evidence"]["contributions"],
                        "R6: the Deck must be able to show where the points came from")


class TestConflictsLeaveBothVersionsEverywhere(IntegrationTestCase):
    """R5, proven on disk rather than in the log."""

    def test_a_simultaneous_edit_leaves_both_versions_in_both_workspaces(self):
        (self.ws_a / "contested.txt").write_bytes(b"common ancestor\n")
        self.settle()
        self.assertEqual((self.ws_b / "contested.txt").read_bytes(), b"common ancestor\n")

        # Both sides edit before either has seen the other's change.
        (self.ws_a / "contested.txt").write_bytes(b"Ada's version\n")
        (self.ws_b / "contested.txt").write_bytes(b"Bob's version\n")
        self.push("a")
        self.push("b")
        self.pump("a")
        self.pump("b")
        self.pump("a")
        self.pump("b")

        for workspace, who in ((self.ws_a, "Ada"), (self.ws_b, "Bob")):
            with self.subTest(workspace=who):
                contents = {p.read_bytes() for p in workspace.rglob("contested.txt*")
                            if p.is_file()}
                self.assertIn(b"Ada's version\n", contents,
                              "{0} lost Ada's bytes".format(who))
                self.assertIn(b"Bob's version\n", contents,
                              "{0} lost Bob's bytes".format(who))
                sidecars = [p for p in workspace.rglob("*.parley-conflict-*") if p.is_file()]
                self.assertTrue(sidecars,
                                "{0} has no conflict sidecar to show the user".format(who))

    def test_the_hub_recorded_the_conflict(self):
        (self.ws_a / "contested.txt").write_bytes(b"ancestor\n")
        self.settle()
        (self.ws_a / "contested.txt").write_bytes(b"ada\n")
        (self.ws_b / "contested.txt").write_bytes(b"bob\n")
        self.push("a")
        self.push("b")
        conflicts = [e for e in self.client_a.events(limit=1000)
                     if e["type"] == "file.conflict"]
        self.assertTrue(conflicts, "a divergence must be announced, not resolved silently")
        self.assertEqual(conflicts[-1]["actor"], "hub")


class TestHubRestart(IntegrationTestCase):
    """SPEC R7: losing the Hub must not lose the workspace or the log."""

    def restart(self):
        self.hub.stop()
        config = self.config_mod.HubConfig.load(self.state_dir)
        self.hub = self.server_mod.Hub(self.state_dir, config,
                                       workspace=self.hub_workspace)
        self.hub.start()
        wait_until(self._hub_is_up, timeout=20.0,
                   message="the Hub never came back up on port {0}".format(self.port))

    def test_both_clients_keep_working_after_the_hub_restarts(self):
        (self.ws_a / "before.txt").write_bytes(b"written before the crash\n")
        self.settle()
        self.assertEqual((self.ws_b / "before.txt").read_bytes(),
                         b"written before the crash\n")
        head_before = len(self.client_a.events(limit=5000))

        self.restart()

        self.assertGreaterEqual(len(self.client_a.events(limit=5000)), head_before,
                                "the append-only log must survive a restart")
        (self.ws_a / "after.txt").write_bytes(b"written after the crash\n")
        self.settle()
        self.assertEqual((self.ws_b / "after.txt").read_bytes(),
                         b"written after the crash\n")

    def test_the_agent_keys_still_authenticate_after_a_restart(self):
        self.restart()
        self.assertIn("agents", self.client_a.state())
        self.assertIn("agents", self.client_b.state())

    def test_chat_still_flows_after_a_restart(self):
        self.restart()
        self.client_a.say("still here")
        texts = wait_until(
            lambda: [e["body"].get("text") for e in self.client_b.events(limit=500)
                     if e["type"] == "chat.message"] or None,
            timeout=10.0, message="chat did not resume after the restart",
        )
        self.assertIn("still here", texts)

    def test_a_workspace_remains_complete_while_the_hub_is_down(self):
        """R7: 'loss of the Hub must leave every participant with a complete local
        workspace and a replayable local log'."""
        (self.ws_a / "kept.txt").write_bytes(b"local copy\n")
        self.settle()
        self.hub.stop()
        self.assertEqual((self.ws_b / "kept.txt").read_bytes(), b"local copy\n")
        self.assertTrue((self.ws_b / ".parley").is_dir())
        self.restart()


class TestNoStrayResources(IntegrationTestCase):
    def test_stopping_the_hub_leaves_no_listening_socket(self):
        import socket

        self.hub.stop()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(2.0)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", self.port))
        except OSError as exc:
            self.fail("port {0} was not released by Hub.stop(): {1}".format(self.port, exc))
        finally:
            probe.close()

    def test_stopping_the_hub_joins_its_threads(self):
        before = {t.name for t in threading.enumerate()}
        self.hub.stop()
        leftover = wait_until(
            lambda: True if not [
                t for t in threading.enumerate()
                if t.is_alive() and t.name not in before and "hub" in t.name.lower()
            ] else None,
            timeout=15.0, message="Hub threads were still running after stop()",
        )
        self.assertTrue(leftover)


if __name__ == "__main__":
    unittest.main()
