"""SPEC §11 ``parley resume`` -- restarting a Hub without ending the parley.

The defect this suite exists for: ``parley init`` mints a *new* session id and a
new root key every single run, so a supervisor configured with
``Restart=on-failure`` -- or anyone who simply re-runs the command after a reboot
-- silently starts a different parley, and every enrolled client then fails with
``fingerprint_mismatch``.  All the data is still on disk; what was missing was a
way to invoke the Hub against it.

So the central test here is not "``resume`` returns the right JSON".  It is: stop
a Hub that has a real enrolled agent and a real log, start it again with
``resume``, and watch that agent keep working **without re-enrolling**.  Anything
less would pass against a ``resume`` that quietly minted fresh credentials.

Ports: each case binds a port obtained a moment earlier and reuses that exact
port across the restart, because an enrolled client's stored ``hub_url`` names
it.  Changing the port on resume is covered by its own test, which re-reads the
credentials rather than assuming.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from parley.client.client import ParleyClient
from parley.config import Credentials, HubConfig
from parley.hub import server as server_mod
from tests.helpers import free_port, wait_until


class ResumeTestCase(unittest.TestCase):
    """A hosted parley, one enrolled agent, and the plumbing to restart it."""

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
            self.host_ws, name="resume-test", port=self.port, bind="127.0.0.1"
        )
        self.hub.start()
        self.addCleanup(self._stop)
        self.hub_url = "http://127.0.0.1:%d" % self.hub.port
        wait_until(self._is_up, timeout=15.0, message="the Hub never started listening")

    # ----------------------------------------------------------- plumbing

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
            self.hub_url, self.watchword, self.agent_ws, name=name, kind="resume-test"
        )
        self.addCleanup(client.close)
        return client

    def restart(self, **kwargs):
        """Stop the Hub and bring it back with ``resume``, as a reboot would."""
        self.hub.stop()
        self.hub = server_mod.resume_parley(self.host_ws, **kwargs)
        self.hub.start()
        self.hub_url = "http://127.0.0.1:%d" % self.hub.port
        wait_until(self._is_up, timeout=15.0, message="the resumed Hub never started listening")
        return self.hub

    def reopen(self, client: ParleyClient) -> ParleyClient:
        """A *fresh* client built only from the credentials already on disk.

        Reusing the live object would prove nothing about restarts: the point is
        that the file written before the Hub died still authenticates after it.
        """
        reopened = ParleyClient(self.agent_ws, Credentials.load(self.agent_ws))
        self.addCleanup(reopened.close)
        return reopened

    # ------------------------------------------------------------- identity

    def test_resume_keeps_the_session_id_and_the_fingerprint(self):
        session = self.hub.config.session
        fingerprint = self.hub.config.fingerprint
        self.assertTrue(session and fingerprint)

        self.restart()

        self.assertEqual(self.hub.config.session, session,
                         "resume must not mint a new session id")
        self.assertEqual(self.hub.config.fingerprint, fingerprint,
                         "resume must not mint a new root key: SPEC §3.5 derives the "
                         "fingerprint from it and SPEC §3.5 calls a change a hard error")

    def test_resume_keeps_the_root_key_itself_not_just_its_fingerprint(self):
        root_key = self.hub.config.root_key_hex
        watchword_hash = self.hub.config.watchword_hash
        self.restart()
        self.assertEqual(self.hub.config.root_key_hex, root_key)
        self.assertEqual(self.hub.config.watchword_hash, watchword_hash,
                         "the old watchword must keep working for new enrolments")

    def test_the_hub_reports_the_same_session_over_the_wire_after_a_resume(self):
        import urllib.request

        with urllib.request.urlopen(self.hub_url + "/v1/hello", timeout=5.0) as response:
            before = json.loads(response.read().decode("utf-8"))
        self.restart()
        with urllib.request.urlopen(self.hub_url + "/v1/hello", timeout=5.0) as response:
            after = json.loads(response.read().decode("utf-8"))
        self.assertEqual(after.get("session"), before.get("session"))
        self.assertEqual(after.get("fingerprint"), before.get("fingerprint"))

    # ---------------------------------------------------- the restart trap

    def test_an_agent_enrolled_before_the_restart_still_authenticates(self):
        """The whole point. No re-enrolment, no new credentials, no re-join."""
        client = self.enrol()
        agent_id = client.agent_id
        before = client.say("before the restart")
        credentials_before = (self.agent_ws / ".parley" / "credentials.json").read_bytes()

        self.restart()

        same_agent = self.reopen(client)
        self.assertEqual(same_agent.agent_id, agent_id)
        after = same_agent.say("after the restart")
        self.assertGreater(int(after["seq"]), int(before["seq"]),
                           "the agent's write must be accepted and sequenced after the old one")
        self.assertEqual((self.agent_ws / ".parley" / "credentials.json").read_bytes(),
                         credentials_before,
                         "nothing may have had to rewrite the agent's credentials")

    def test_the_resumed_hub_still_knows_the_agent_in_its_roster(self):
        client = self.enrol()
        client.announce()
        self.restart()
        roster = {row["agent_id"] for row in self.hub.store.list_agents()}
        self.assertIn(client.agent_id, roster)

    def test_the_event_log_and_head_seq_survive(self):
        client = self.enrol()
        client.say("one")
        client.say("two")
        head_before = self.hub.store.head_seq()
        log_before = [e["id"] for e in self.hub.store.read(since=0, limit=10_000)]
        self.assertGreaterEqual(head_before, 2)

        self.restart()

        self.assertEqual(self.hub.store.head_seq(), head_before + 2,
                         "resume appends hub.started and hub.policy and nothing else")
        log_after = [e["id"] for e in self.hub.store.read(since=0, limit=10_000)]
        self.assertEqual(log_after[:len(log_before)], log_before,
                         "the log is append-only across a restart; nothing may be rewritten")

        self.assertEqual(self.hub.view.head_seq(), self.hub.store.head_seq(),
                         "the materialised view must be rebuilt from the surviving log")

    def test_events_written_before_the_restart_are_still_readable_afterwards(self):
        client = self.enrol()
        client.say("remember me")
        self.restart()
        texts = [e.get("body", {}).get("text")
                 for e in self.reopen(client).events(since=0, limit=10_000)]
        self.assertIn("remember me", texts)

    def test_blobs_survive_a_resume(self):
        client = self.enrol()
        blob_hash = client.put_blob(b"the bytes must outlive the process")
        self.restart()
        self.assertEqual(self.reopen(client).get_blob(blob_hash),
                         b"the bytes must outlive the process")

    def test_the_host_token_survives_so_approve_and_rotate_still_work(self):
        host_token = self.hub.config.host_token
        self.assertTrue(host_token.startswith("hst_"))
        client = self.enrol()

        self.restart()

        self.assertEqual(self.hub.config.host_token, host_token)
        self.assertEqual(HubConfig.load(self.hub.state_dir).host_token, host_token)
        status, _headers, body = self._admin("/v1/admin/approve",
                                             {"agent_id": client.agent_id}, host_token)
        self.assertEqual(status, 200, body)

    def _admin(self, path, payload, host_token):
        from tests.helpers import http_call

        return http_call(
            self.hub_url + path,
            method="POST",
            body=json.dumps(payload).encode("utf-8"),
            # SPEC §3.7 pins the host token to exactly one carrier.
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Authorization": "Parley-Host " + host_token},
        )

    # --------------------------------------------------------- moving ports

    def test_resume_can_move_the_port_and_persists_the_move(self):
        new_port = free_port()
        self.restart(port=new_port)
        self.assertEqual(self.hub.port, new_port)
        self.assertEqual(HubConfig.load(self.hub.state_dir).port, new_port,
                         "`parley approve` reads the address out of hub.json, so a move "
                         "that is not persisted breaks it")

    def test_resume_without_an_override_keeps_the_stored_address(self):
        self.restart()
        self.assertEqual(self.hub.port, self.port)
        self.assertEqual(self.hub.config.bind, "127.0.0.1")

    # ------------------------------------------------------ init refuses

    def test_init_over_an_existing_state_directory_refuses(self):
        with self.assertRaises(server_mod.HubStateExists) as caught:
            server_mod.create_parley(self.host_ws, name="second", port=free_port(),
                                     bind="127.0.0.1")
        self.assertIn("resume", caught.exception.hint,
                      "the refusal must point at the command that does what was meant")

    def test_a_refused_init_changes_nothing(self):
        session = self.hub.config.session
        head = self.hub.store.head_seq()
        with self.assertRaises(server_mod.HubStateExists):
            server_mod.create_parley(self.host_ws, name="second", port=free_port(),
                                     bind="127.0.0.1")
        self.assertEqual(HubConfig.load(self.hub.state_dir).session, session)
        self.assertEqual(self.hub.store.head_seq(), head)

    def test_init_force_starts_a_genuinely_new_parley(self):
        old_session = self.hub.config.session
        old_fingerprint = self.hub.config.fingerprint
        client = self.enrol()
        client.say("this log is about to be destroyed")
        self.hub.stop()

        self.hub, watchword = server_mod.create_parley(
            self.host_ws, name="replacement", port=self.port, bind="127.0.0.1", force=True
        )
        self.hub.start()
        self.hub_url = "http://127.0.0.1:%d" % self.hub.port
        wait_until(self._is_up, timeout=15.0, message="the forced Hub never started listening")

        self.assertNotEqual(self.hub.config.session, old_session)
        self.assertNotEqual(self.hub.config.fingerprint, old_fingerprint)
        self.assertNotEqual(watchword, self.watchword)

    def test_init_force_is_destructive_as_advertised(self):
        """--force locks every enrolled agent out. That is the warning, so prove it."""
        client = self.enrol()
        self.hub.stop()

        # Same port, so the stale client actually reaches the new Hub: the point
        # is that it is *rejected*, not that it cannot connect.
        self.hub, _watchword = server_mod.create_parley(
            self.host_ws, name="replacement", port=self.port, bind="127.0.0.1", force=True
        )
        self.hub.start()
        self.hub_url = "http://127.0.0.1:%d" % self.hub.port
        wait_until(self._is_up, timeout=15.0, message="the forced Hub never started listening")

        self.assertEqual(self.hub.store.head_seq(), 2,
                         "the old log is gone; only the new Hub's own two events remain")
        self.assertEqual(self.hub.store.list_agents(), [],
                         "the old roster is gone")

        stale = ParleyClient(self.agent_ws, Credentials.load(self.agent_ws))
        self.addCleanup(stale.close)
        # The agent's key was minted under a session that no longer exists; which
        # of the §12 codes comes back depends on how far the request gets, but it
        # must not succeed.
        with self.assertRaises(Exception) as caught:
            stale.say("am I still in?")
        self.assertIn(getattr(caught.exception, "code", ""),
                      ("no_such_session", "unknown_agent", "bad_signature"),
                      "a locked-out agent must be told so, not silently accepted")
        self.assertNotEqual(client.session, self.hub.config.session)

    # ------------------------------------------------- resume refuses

    def test_resume_on_a_directory_with_no_parley_fails_usefully(self):
        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        with self.assertRaises(server_mod.NoHubState) as caught:
            server_mod.resume_parley(empty)
        message = caught.exception.message + " " + caught.exception.hint
        self.assertIn("init", message,
                      "someone who ran resume by mistake must be told to run init")
        self.assertIn(str(empty), message)

    def test_resume_on_a_corrupt_hub_json_fails_usefully(self):
        broken = Path(self._tmp.name) / "broken"
        (broken / ".parley" / "hub").mkdir(parents=True)
        (broken / ".parley" / "hub" / "hub.json").write_text("{ this is not json",
                                                             encoding="utf-8")
        with self.assertRaises(server_mod.NoHubState) as caught:
            server_mod.resume_parley(broken)
        self.assertIn("hub.json", caught.exception.message)

    def test_resume_on_a_hub_json_with_no_root_key_fails_usefully(self):
        """Parses fine, but could not authenticate anybody. Fail now, not per request."""
        gutted = Path(self._tmp.name) / "gutted"
        state = gutted / ".parley" / "hub"
        state.mkdir(parents=True)
        data = HubConfig.load(self.hub.state_dir).to_dict()
        data["root_key_hex"] = ""
        (state / "hub.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(server_mod.NoHubState) as caught:
            server_mod.resume_parley(gutted)
        self.assertIn("root_key_hex", caught.exception.message)

    def test_resume_does_not_create_a_state_directory_it_did_not_find(self):
        empty = Path(self._tmp.name) / "untouched"
        empty.mkdir()
        with self.assertRaises(server_mod.NoHubState):
            server_mod.resume_parley(empty)
        self.assertFalse((empty / ".parley" / "hub").exists(),
                         "a failed resume must leave nothing behind for the next one to find")

    # ------------------------------------------------------- state dir lookup

    def test_find_hub_state_dir_locates_the_hosted_parley(self):
        self.assertEqual(server_mod.find_hub_state_dir(self.host_ws), self.hub.state_dir)

    def test_find_hub_state_dir_ignores_a_workspace_that_only_joined(self):
        """A participant's ``.parley`` holds credentials, not a Hub."""
        self.enrol()
        self.assertIsNone(server_mod.find_hub_state_dir(self.agent_ws))

    def test_a_legacy_hub_json_directly_in_dot_parley_is_found(self):
        legacy = Path(self._tmp.name) / "legacy"
        (legacy / ".parley").mkdir(parents=True)
        (legacy / ".parley" / "hub.json").write_text("{}", encoding="utf-8")
        self.assertEqual(server_mod.find_hub_state_dir(legacy), legacy / ".parley")


class ResumeCliWiringTestCase(unittest.TestCase):
    """The CLI surface SPEC §11 specifies, without starting a Hub."""

    def setUp(self):
        from parley import cli

        self.cli = cli
        self.parser = cli.build_parser()

    def test_resume_is_a_subcommand(self):
        args = self.parser.parse_args(["resume"])
        self.assertIs(args.func, self.cli.cmd_resume)

    def test_resume_accepts_the_spec_11_options(self):
        args = self.parser.parse_args(
            ["resume", "--workspace", "/tmp/x", "--port", "9000", "--bind", "127.0.0.1"]
        )
        self.assertEqual((args.workspace, args.port, args.bind), ("/tmp/x", 9000, "127.0.0.1"))

    def test_resume_supports_json_like_every_other_subcommand(self):
        self.assertTrue(self.parser.parse_args(["resume", "--json"]).json)

    def test_resume_leaves_the_address_alone_when_not_given(self):
        args = self.parser.parse_args(["resume"])
        self.assertIsNone(args.port)
        self.assertIsNone(args.bind)

    def test_init_has_an_explicit_force(self):
        self.assertTrue(self.parser.parse_args(["init", "--force"]).force)
        self.assertFalse(self.parser.parse_args(["init"]).force)

    def test_the_refusal_names_resume_and_spells_out_what_force_costs(self):
        err = self.cli._refuse_to_clobber({
            "state_dir": "/w/.parley/hub", "session": "ses_1", "fingerprint": "a-b-c",
            "name": "x", "agents": 4, "head_seq": 9,
        })
        self.assertEqual(err.code, "hub_state_exists")
        self.assertTrue(err.loud, "this is the one error that must not be a one-liner")
        self.assertIn("parley resume", err.hint)
        self.assertIn("4 agent(s)", err.hint)
        self.assertIn("locked out", err.hint)

    def test_the_resume_banner_never_prints_a_watchword(self):
        import io

        from parley.term import Term

        term = Term(io.StringIO(), colour=False)
        lines = self.cli.render_resume(
            term, name="n", session="ses_1", fingerprint="a-b-c",
            hub_url="http://127.0.0.1:7777", deck_url="http://127.0.0.1:7777/?vt=vwr_x",
            workspace="/w", bind="127.0.0.1", port=7777, agents=3, head_seq=42,
        )
        text = "\n".join(lines)
        for item in ("a-b-c", "3 enrolled", "42 event(s)", "http://127.0.0.1:7777"):
            self.assertIn(item, text)
        self.assertNotIn("SAY THIS OUT LOUD", text,
                         "resume mints no watchword, so it must not print the invite screen")

    def test_the_host_token_travels_in_exactly_one_header(self):
        """SPEC §3.7: ``Authorization: Parley-Host``, and nothing else."""
        import urllib.request

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured.update(req.headers)
            raise AssertionError("stop here; the headers are what is under test")

        original = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(self.cli.CliError):
                self.cli._admin_request("http://127.0.0.1:1", "hst_" + "0" * 32,
                                        "/v1/admin/approve", {}, 1.0)
        finally:
            urllib.request.urlopen = original

        lowered = {k.lower(): v for k, v in captured.items()}
        self.assertEqual(lowered.get("authorization"), "Parley-Host " + "hst_" + "0" * 32)
        self.assertNotIn("x-parley-host-token", lowered,
                         "a second carrier doubles the exposure surface (SPEC §3.7)")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
