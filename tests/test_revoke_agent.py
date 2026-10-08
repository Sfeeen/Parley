"""SPEC §3.8 / §11 ``parley revoke --agent`` -- evicting a participant.

``revoke`` carries two different operations because they are the same verb in
English: ``--name`` withdraws a capability you announced (§15.1), ``--agent``
evicts a participant entirely (§3.8).  Keeping them apart is not pedantry.
``docs/SECURITY.md`` names agent revocation as *the* response to a leaked agent
key, and an operator reaching for it mid-incident must not silently withdraw a
capability instead and believe they have contained the breach.

So the tests that matter here are the two that would actually bite:

* a revoked agent's key stops working **immediately**, not at the next heartbeat;
* the two senses of ``revoke`` cannot be confused for one another at the CLI.

The second one also pins the thing that made this worth building: before this
existed, ``SECURITY.md`` documented a command that revoked the wrong thing.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from parley.client.client import ParleyClient
from parley.config import Credentials
from parley.errors import ParleyError
from parley.hub import server as server_mod
from tests.helpers import free_port, wait_until


class RevokeAgentTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.host_ws = root / "host"
        self.agent_ws = root / "ada"
        for path in (self.host_ws, self.agent_ws):
            path.mkdir(parents=True)

        self.port = free_port()
        self.hub, self.watchword = server_mod.create_parley(
            self.host_ws, name="revoke-test", port=self.port, bind="127.0.0.1"
        )
        self.hub.start()
        self.addCleanup(self._stop)
        self.hub_url = "http://127.0.0.1:%d" % self.hub.port
        wait_until(self._is_up, timeout=15.0, message="the Hub never started listening")

    def _stop(self):
        try:
            self.hub.stop()
        except Exception:
            pass

    def _is_up(self):
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(self.hub_url + "/v1/hello", timeout=2.0) as response:
                return response.getcode() == 200
        except (urllib.error.URLError, OSError):
            return False

    def enrol(self, name="Ada") -> ParleyClient:
        client = ParleyClient.enroll(
            self.hub_url, self.watchword, self.agent_ws, name=name, kind="revoke-test"
        )
        self.addCleanup(client.close)
        return client

    # ------------------------------------------------------------------ #

    def test_a_revoked_agents_key_stops_working_immediately(self):
        client = self.enrol()
        self.assertTrue(client.say("before").get("seq"))

        self.hub.store.set_agent_status(client.creds.agent_id, "revoked")

        with self.assertRaises(ParleyError) as caught:
            client.say("after")
        self.assertEqual(caught.exception.code, "revoked")

    def test_revocation_survives_a_reopened_client(self):
        """The credentials file on disk must not be a way back in.

        A client that reloads from disk presents exactly the same key, so if
        revocation lived only in the live object's memory this would succeed.
        """
        client = self.enrol()
        agent_id = client.creds.agent_id
        self.hub.store.set_agent_status(agent_id, "revoked")

        reopened = ParleyClient(self.agent_ws, Credentials.load(self.agent_ws))
        self.addCleanup(reopened.close)
        with self.assertRaises(ParleyError) as caught:
            reopened.say("after")
        self.assertEqual(caught.exception.code, "revoked")

    def test_revoking_one_agent_does_not_disturb_another(self):
        """§3.8: revocation is surgical. No re-keying, no collateral eviction."""
        other_ws = self.agent_ws.parent / "bram"
        other_ws.mkdir()
        first = self.enrol()
        second = ParleyClient.enroll(
            self.hub_url, self.watchword, other_ws, name="Bram", kind="revoke-test"
        )
        self.addCleanup(second.close)

        self.hub.store.set_agent_status(first.creds.agent_id, "revoked")

        with self.assertRaises(ParleyError):
            first.say("gone")
        self.assertTrue(second.say("still here").get("seq"),
                        "revoking one agent must not disturb any other")


class RevokeCliGrammarTestCase(unittest.TestCase):
    """The two senses of ``revoke`` must not be confusable (SPEC §11)."""

    def parse(self, argv):
        from parley import cli

        parser = cli.build_parser()
        return parser.parse_args(argv)

    def test_both_senses_are_reachable(self):
        self.assertEqual(self.parse(["revoke", "--name", "kvm.relay"]).name, ["kvm.relay"])
        self.assertEqual(self.parse(["revoke", "--agent", "agt_dead"]).agent, "agt_dead")

    def test_asking_for_both_at_once_is_a_usage_error(self):
        from parley import cli

        args = self.parse(["revoke", "--name", "a.b", "--agent", "agt_x"])
        ctx = cli.Ctx(args)
        with self.assertRaises(cli.CliError) as caught:
            cli.cmd_revoke(ctx, args)
        self.assertEqual(caught.exception.exit_code, cli.EXIT_USAGE)

    def test_neither_is_a_usage_error_that_names_both(self):
        from parley import cli

        args = self.parse(["revoke"])
        ctx = cli.Ctx(args)
        with self.assertRaises(cli.CliError) as caught:
            cli.cmd_revoke(ctx, args)
        self.assertEqual(caught.exception.exit_code, cli.EXIT_USAGE)
        hint = (caught.exception.hint or "") + (str(caught.exception) or "")
        self.assertIn("--name", hint)
        self.assertIn("--agent", hint,
                      "the usage error must point at agent revocation too, or an operator "
                      "containing a leaked key will not find it")


if __name__ == "__main__":
    unittest.main()
