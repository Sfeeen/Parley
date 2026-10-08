"""SPEC §11 + §15 — the Exchange's command-line surface.

The CLI is the only part of the Exchange a human or a shell-driving agent ever
touches, so these tests exercise it the way both of them do: by calling
``parley.cli.main`` with a real ``argv`` and reading what comes back on stdout.

Nothing here opens a socket.  A :class:`FakeClient` stands in for
``ParleyClient``, keeps the event log in a list, and — crucially — answers a
``request.create`` the way a cooperative peer would, by appending the
``request.accept``/``request.result`` events the scenario asks for.  That makes
``ask --wait`` testable end to end, including the exit code it hands back, which
is the part an agent scripting against this actually branches on.

The three things under test, in order of how badly they matter:

1. **Exit codes and the ``--json`` envelope.**  An agent cannot tell "it refused"
   from "it broke" from "it timed out" unless ``data.error.code`` says so, and
   SPEC §11 pins the numeric codes at 0..5 so the distinction has nowhere else to
   live.
2. **Consent is never decided here.**  ``requests --pending`` must show what is
   waiting, never auto-answer it, and must say the declared safety out loud.
3. **A malformed announcement is caught at the CLI**, with every problem reported
   at once and nothing sent to the Hub.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from parley import cli, errors
from parley.exchange import Capability

ME = "agt_49a30c00b9204872"
PEER = "agt_77922fcb48dec836"
SESSION = "ses_9f2c41ab77e0d315"

ZDRIVE = {
    "name": "zdrive.search",
    "title": "Search the company Z: technical library",
    "kind": "mcp",
    "description": "Full-text search over manuals, schematics, firmware dumps and PC "
                   "software for industrial hardware. Returns canonical Z:\\ paths and "
                   "nothing else -- it does not open the documents for you.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}, "brand": {"type": "string"}},
        "required": ["query"],
    },
    "output": "json",
    "safety": "safe",
    "cost": "cheap",
    "concurrency": 2,
    "exclusive": True,
    "avg_duration_s": 4,
    "agent_id": PEER,
    "agent_name": "Ada",
    "online": True,
    "in_flight": 0,
}

RELAY = {
    "name": "kvm.relay",
    "title": "Switch a physical relay on the bench KVM",
    "kind": "hardware",
    "description": "Closes, opens or pulses one of 10 dry contacts wired to the test "
                   "bench. Can power-cycle the device under test.",
    "input_schema": {
        "type": "object",
        "properties": {"relay": {"type": "integer", "minimum": 0, "maximum": 9},
                       "action": {"enum": ["on", "off", "pulse"]}},
        "required": ["relay", "action"],
    },
    "output": "json",
    "safety": "dangerous",
    "cost": "cheap",
    "concurrency": 1,
    "exclusive": True,
    "agent_id": PEER,
    "agent_name": "Ada",
    "online": True,
    "in_flight": 0,
}


# --------------------------------------------------------------------------- #
# The stand-in Hub
# --------------------------------------------------------------------------- #


class FakeCreds:
    def __init__(self, agent_id=ME, name="Bob"):
        self.agent_id = agent_id
        self.name = name
        self.session = SESSION
        self.fingerprint = "crab-distaff-basket"


class FakeTransport:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def get_json(self, path):
        self.calls.append(path)
        if path in self.client.endpoints:
            return self.client.endpoints[path]
        # What a Hub that has not grown this route yet answers.  It must not be
        # fatal: the CLI is required to fall through to /v1/state and the log.
        raise errors.NoSuchBlob("no such route: " + path)


class FakeClient:
    """A ParleyClient with the network taken out and a scripted peer behind it."""

    def __init__(self, agent_id=ME, name="Bob"):
        self.creds = FakeCreds(agent_id, name)
        self.agent_id = agent_id
        self.transport = FakeTransport(self)
        self.endpoints = {}
        self.log = []
        self.seq = 0
        self.last_psr = None
        self.agents = [
            {"agent_id": PEER, "name": "Ada", "kind": "bench", "online": True},
            {"agent_id": ME, "name": "Bob", "kind": "claude-code", "online": True},
        ]
        self.capabilities = []
        #: ``[(type, body-extra)]`` the peer answers a ``request.create`` with,
        #: appended synchronously so a terminal state is there before `wait` runs.
        self.auto_reply = []
        #: The same, but drip-fed from a thread, which is what makes the live
        #: progress narration observable.
        self.timed_reply = []
        self.timed_gap = 0.12
        self.emit_fails = False
        self.statuses = []

    # -- writes ------------------------------------------------------------ #
    def emit(self, etype, body, event_id=None):
        if self.emit_fails:
            raise errors.TransportError("the Hub is not answering")
        event = self._append(self.agent_id, etype, body)
        if etype == "request.create":
            for reply_type, extra in self.auto_reply:
                self._reply(body["id"], reply_type, extra)
            if self.timed_reply:
                thread = threading.Thread(target=self._drip, args=(body["id"],))
                thread.daemon = True
                thread.start()
        return event

    def _reply(self, req_id, reply_type, extra):
        reply = dict(extra)
        reply["id"] = req_id
        self._append("hub" if reply_type == "request.expired" else PEER, reply_type, reply)

    def _drip(self, req_id):
        for reply_type, extra in self.timed_reply:
            time.sleep(self.timed_gap)
            self._reply(req_id, reply_type, extra)

    def _append(self, actor, etype, body):
        self.seq += 1
        event = {
            "v": "PARLEY/1",
            "id": "evt_%016x" % self.seq,
            "seq": self.seq,
            "ts": _stamp(self.seq),
            "session": SESSION,
            "actor": actor,
            "type": etype,
            "body": dict(body),
        }
        self.log.append(event)
        return event

    def status(self, headline, **kwargs):
        self.statuses.append((headline, kwargs))
        return self._append(self.agent_id, "status.update",
                            dict(kwargs, headline=headline))

    def set_last_psr(self, body):
        self.last_psr = body

    # -- reads ------------------------------------------------------------- #
    def state(self):
        doc = {"session": SESSION, "name": "test", "head_seq": self.seq,
               "agents": self.agents}
        doc.update(self.endpoints.get("/v1/state", {}))
        return doc

    def events(self, since=0, limit=1000):
        return [e for e in self.log if e["seq"] > since][:limit]


def _stamp(_seq):
    """Wire-form `ts` for *now*.

    Real timestamps matter here: the CLI folds the log with each event's own
    ``ts`` so that "auto-declines in 4 min" is a real countdown, and a fixture
    stamped in 1970 would make every request look long expired.
    """
    now = time.time()
    return (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
            + ".%03dZ" % int((now % 1) * 1000))


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class CliCase(unittest.TestCase):
    """Runs ``cli.main`` against a FakeClient and a throwaway workspace."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        (self.workspace / ".parley").mkdir(parents=True, exist_ok=True)
        self.client = FakeClient()

        original = cli._client
        cli._client = lambda workspace: (self.client, self.client.creds)
        self.addCleanup(lambda: setattr(cli, "_client", original))
        self._restore_json_mode = cli._JSON_MODE[0]
        self.addCleanup(lambda: cli._JSON_MODE.__setitem__(0, self._restore_json_mode))

    def offer_registry(self, *caps):
        self.client.endpoints["/v1/capabilities"] = {"capabilities": list(caps)}

    def run_cli(self, *argv, **kwargs):
        """-> (exit_code, stdout, stderr).  ``--workspace`` is added for you."""
        args = list(argv)
        if kwargs.get("workspace", True):
            args += ["--workspace", str(self.workspace)]
        args += ["--no-color", "--ascii"]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(args)
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *argv, **kwargs):
        """-> (exit_code, parsed envelope).  Asserts stdout really is one object."""
        code, out, _err = self.run_cli(*(list(argv) + ["--json"]), **kwargs)
        self.assertTrue(out.strip(), "a --json command printed nothing on stdout")
        return code, json.loads(out)

    def assertEnvelope(self, doc, command, *, ok=True, exit_code=0):
        self.assertEqual(doc["command"], command)
        self.assertIs(doc["ok"], ok)
        self.assertEqual(doc["exit_code"], exit_code)
        self.assertIn("data" if ok or command in ("ask", "instruct") else "error", doc)

    def emitted(self, etype):
        return [e for e in self.client.log if e["type"] == etype]


# --------------------------------------------------------------------------- #
# 1. argparse construction
# --------------------------------------------------------------------------- #


class TestParserConstruction(unittest.TestCase):
    """Every SPEC §11 Exchange subcommand exists, and takes what the grammar says."""

    EXCHANGE_COMMANDS = ("offer", "revoke", "capabilities", "ask", "instruct",
                         "requests", "accept", "decline", "fulfil")

    def setUp(self):
        self.parser = cli.build_parser()
        self.subs = {}
        for action in self.parser._subparsers._group_actions:
            self.subs.update(getattr(action, "choices", {}))
            break

    def options(self, command):
        out = set()
        for action in self.subs[command]._actions:
            out.update(action.option_strings)
        return out

    def positionals(self, command):
        return [a.dest for a in self.subs[command]._actions if not a.option_strings]

    def test_every_exchange_command_is_registered(self):
        for name in self.EXCHANGE_COMMANDS:
            self.assertIn(name, self.subs, "`parley %s` is missing" % name)
            self.assertTrue(callable(self.subs[name].get_default("func")))
            self.assertTrue((self.subs[name].description or "").strip(),
                            "%s has no description; `parley --json` lists it" % name)

    def test_every_exchange_command_takes_json(self):
        for name in self.EXCHANGE_COMMANDS:
            self.assertIn("--json", self.options(name))

    def test_offer_grammar(self):
        opts = self.options("offer")
        for flag in ("--name", "--title", "--kind", "--schema", "--safety", "--desc", "--from"):
            self.assertIn(flag, opts)

    def test_offer_safety_choices_and_consequences(self):
        safety = [a for a in self.subs["offer"]._actions if "--safety" in a.option_strings][0]
        self.assertEqual(list(safety.choices), ["safe", "guarded", "dangerous"])
        self.assertEqual(safety.default, "guarded")
        epilog = self.subs["offer"].epilog or ""
        # SPEC §15.4: dangerous can never be auto-accepted, and the help text is
        # where someone decides which level to declare.
        self.assertIn("NEVER auto-accepted", epilog)
        for level in ("safe", "guarded", "dangerous"):
            self.assertIn(level, epilog)

    def test_revoke_grammar(self):
        self.assertIn("--name", self.options("revoke"))

    def test_capabilities_grammar(self):
        opts = self.options("capabilities")
        self.assertIn("--kind", opts)
        self.assertIn("--agent", opts)

    def test_ask_grammar(self):
        self.assertEqual(self.positionals("ask"), ["agent", "capability"])
        opts = self.options("ask")
        for flag in ("--input", "--reason", "--wait", "--timeout"):
            self.assertIn(flag, opts)

    def test_ask_timeout_is_the_request_timeout_not_the_network_one(self):
        """SPEC §11 spells `--timeout S` on ask as the request's timeout_s."""
        timeout = [a for a in self.subs["ask"]._actions if "--timeout" in a.option_strings][0]
        self.assertIsNone(timeout.default)
        self.assertIn("timeout_s", timeout.help)
        # ...and the shared network --timeout is untouched everywhere else, which
        # it would not be if this had been done with conflict_handler="resolve".
        for name in ("say", "status", "offer", "requests", "fulfil"):
            shared = [a for a in self.subs[name]._actions
                      if "--timeout" in a.option_strings][0]
            self.assertIn("network", shared.help, "%s lost the network --timeout" % name)
        top = [a for a in self.parser._actions if "--timeout" in a.option_strings][0]
        self.assertEqual(top.default, 15.0)

    def test_instruct_grammar(self):
        self.assertEqual(self.positionals("instruct"), ["agent", "instruction"])
        for flag in ("--reason", "--wait"):
            self.assertIn(flag, self.options("instruct"))

    def test_requests_grammar(self):
        opts = self.options("requests")
        for flag in ("--pending", "--mine", "--to-me", "--state"):
            self.assertIn(flag, opts)

    def test_accept_decline_fulfil_grammar(self):
        self.assertEqual(self.positionals("accept"), ["req_id"])
        self.assertIn("--eta", self.options("accept"))
        self.assertEqual(self.positionals("decline"), ["req_id"])
        self.assertIn("--reason", self.options("decline"))
        self.assertIn("--code", self.options("decline"))
        self.assertEqual(self.positionals("fulfil"), ["req_id"])
        for flag in ("--output", "--text", "--file", "--fail", "--error"):
            self.assertIn(flag, self.options("fulfil"))

    def test_decline_reason_is_required(self):
        reason = [a for a in self.subs["decline"]._actions if "--reason" in a.option_strings][0]
        self.assertTrue(reason.required)

    def test_decline_codes_match_the_spec(self):
        code = [a for a in self.subs["decline"]._actions if "--code" in a.option_strings][0]
        self.assertEqual(
            set(code.choices),
            {"unknown_capability", "bad_input", "policy", "busy", "unsafe",
             "offline", "needs_human", "other"},
        )

    def test_constants_have_not_drifted_from_the_core(self):
        """cli.py spells these out so `--help` works on a half-built tree."""
        from parley import exchange

        self.assertEqual(cli.CAPABILITY_KINDS, exchange.CAPABILITY_KINDS)
        self.assertEqual(cli.SAFETY_LEVELS, exchange.SAFETY_LEVELS)
        self.assertEqual(cli.COST_LEVELS, exchange.COST_LEVELS)
        self.assertEqual(cli.OUTPUT_KINDS, exchange.OUTPUT_KINDS)
        self.assertEqual(cli.REQUEST_STATES, exchange.REQUEST_STATES)
        self.assertEqual(set(cli.DECLINE_CODES) | {"cancelled"},
                         set(exchange.DECLINE_CODES))


class TestCommandInventory(unittest.TestCase):
    """`parley --json` is how an agent discovers the CLI; it must list these."""

    def setUp(self):
        self.payload = cli._overview_payload()

    def test_inventory_includes_the_exchange_commands(self):
        names = {c["name"] for c in self.payload["commands"]}
        for name in TestParserConstruction.EXCHANGE_COMMANDS:
            self.assertIn(name, names)
            help_text = [c["help"] for c in self.payload["commands"] if c["name"] == name][0]
            self.assertTrue(help_text.strip())

    def test_inventory_explains_the_request_outcomes(self):
        outcomes = self.payload["exchange"]["request_outcomes"]
        self.assertEqual(outcomes["done"]["exit_code"], 0)
        for state, code in (("declined", "request_declined"), ("failed", "request_failed"),
                            ("expired", "request_expired"), ("cancelled", "request_cancelled")):
            self.assertEqual(outcomes[state]["error_code"], code)
            self.assertEqual(outcomes[state]["exit_code"], 1)
        self.assertEqual(outcomes["still_live"]["error_code"], "wait_timeout")

    def test_inventory_names_the_safety_consequences(self):
        levels = self.payload["exchange"]["safety_levels"]
        self.assertIn("NEVER auto-accepted", levels["dangerous"])

    def test_epilogue_mentions_the_exchange(self):
        for fragment in ("parley capabilities", "parley offer", "parley ask",
                         "parley requests --pending"):
            self.assertIn(fragment, cli.EPILOGUE)


# --------------------------------------------------------------------------- #
# 2. offer / revoke
# --------------------------------------------------------------------------- #


class TestOffer(CliCase):

    def test_offer_announces_and_persists(self):
        code, doc = self.run_json(
            "offer", "--name", "zdrive.search", "--title", "Search the Z: library",
            "--kind", "mcp", "--safety", "safe", "--desc", "What it does and what it does not.",
        )
        self.assertEqual(code, 0)
        self.assertEnvelope(doc, "offer")
        data = doc["data"]
        self.assertEqual(data["added"], ["zdrive.search"])
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["type"], "capability.announce")
        announced = self.emitted("capability.announce")
        self.assertEqual(len(announced), 1)
        self.assertEqual([c["name"] for c in announced[0]["body"]["capabilities"]],
                         ["zdrive.search"])
        stored = json.loads((self.workspace / ".parley" / "capabilities.json").read_text("utf-8"))
        self.assertEqual([c["name"] for c in stored["capabilities"]], ["zdrive.search"])

    def test_offer_is_total_and_re_announces_what_was_already_there(self):
        """SPEC §15.1: announce replaces the whole catalogue, so one `offer`
        that only re-sent its own entry would silently withdraw the rest."""
        self.offer_registry(dict(ZDRIVE, agent_id=ME, agent_name="Bob"))
        code, doc = self.run_json(
            "offer", "--name", "kvm.relay", "--title", "Switch a relay",
            "--kind", "hardware", "--safety", "dangerous",
            "--desc", "Closes a dry contact on the bench.",
        )
        self.assertEqual(code, 0)
        self.assertEqual(doc["data"]["added"], ["kvm.relay"])
        self.assertEqual(doc["data"]["kept"], ["zdrive.search"])
        body = self.emitted("capability.announce")[0]["body"]
        self.assertEqual(sorted(c["name"] for c in body["capabilities"]),
                         ["kvm.relay", "zdrive.search"])

    def test_offer_from_a_catalogue_file(self):
        path = self.workspace / "catalogue.json"
        path.write_text(json.dumps({"capabilities": [
            {k: v for k, v in ZDRIVE.items() if k not in ("agent_id", "agent_name",
                                                          "online", "in_flight")},
            {k: v for k, v in RELAY.items() if k not in ("agent_id", "agent_name",
                                                         "online", "in_flight")},
        ]}), encoding="utf-8")
        code, doc = self.run_json("offer", "--from", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(doc["data"]["count"], 2)
        self.assertEqual(sorted(doc["data"]["added"]), ["kvm.relay", "zdrive.search"])

    def test_offer_schema_file(self):
        schema = self.workspace / "schema.json"
        schema.write_text(json.dumps(ZDRIVE["input_schema"]), encoding="utf-8")
        code, doc = self.run_json(
            "offer", "--name", "a.b", "--title", "T", "--kind", "tool",
            "--desc", "D", "--schema", str(schema),
        )
        self.assertEqual(code, 0)
        self.assertEqual(doc["data"]["announced"][0]["input_schema"], ZDRIVE["input_schema"])

    # -- the validation gate ------------------------------------------------ #
    def test_malformed_capability_is_refused_before_anything_is_announced(self):
        code, doc = self.run_json(
            "offer", "--name", "BadName", "--title", "T", "--kind", "mcp",
        )
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["error"]["code"], "bad_capability")
        problems = doc["error"]["detail"]["problems"]["BadName"]
        self.assertTrue(any("lowercase" in p for p in problems))
        self.assertTrue(any("description is required" in p for p in problems))
        self.assertEqual(self.emitted("capability.announce"), [],
                         "a malformed catalogue must not reach the Hub")

    def test_every_problem_in_a_catalogue_is_reported_at_once(self):
        path = self.workspace / "bad.json"
        path.write_text(json.dumps({"capabilities": [
            {"name": "a.b", "title": "t", "kind": "mcp", "description": "d", "safety": "sfae"},
            {"name": "c.d", "title": "", "kind": "wrong", "description": ""},
        ]}), encoding="utf-8")
        code, doc = self.run_json("offer", "--from", str(path))
        self.assertEqual(code, cli.EXIT_USAGE)
        problems = doc["error"]["detail"]["problems"]
        self.assertEqual(sorted(problems), ["a.b", "c.d"])
        self.assertTrue(any("safety must be one of" in p for p in problems["a.b"]))
        self.assertGreaterEqual(len(problems["c.d"]), 3)

    def test_capability_validate_problems_are_rendered_for_a_human(self):
        code, out, err = self.run_cli(
            "offer", "--name", "BadName", "--title", "T", "--kind", "mcp",
        )
        self.assertEqual(code, cli.EXIT_USAGE)
        screen = out + err
        self.assertIn("BadName", screen)
        self.assertIn("lowercase", screen)
        self.assertIn("description is required", screen)
        self.assertIn("nothing was announced", screen)

    def test_offer_needs_name_title_and_kind(self):
        code, doc = self.run_json("offer", "--name", "a.b")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("--title", doc["error"]["message"])
        self.assertIn("--kind", doc["error"]["message"])

    def test_offer_reports_an_undelivered_announcement(self):
        self.client.emit_fails = True
        code, doc = self.run_json(
            "offer", "--name", "a.b", "--title", "T", "--kind", "tool", "--desc", "D",
        )
        self.assertEqual(code, cli.EXIT_NO_HUB)
        self.assertEqual(doc["error"]["code"], "not_delivered")


class TestRevoke(CliCase):

    def test_revoke_emits_and_rewrites_the_catalogue(self):
        self.offer_registry(dict(ZDRIVE, agent_id=ME, agent_name="Bob"),
                            dict(RELAY, agent_id=ME, agent_name="Bob"))
        code, doc = self.run_json("revoke", "--name", "kvm.relay")
        self.assertEqual(code, 0)
        self.assertEnvelope(doc, "revoke")
        self.assertEqual(doc["data"]["revoked"], ["kvm.relay"])
        self.assertEqual([c["name"] for c in doc["data"]["remaining"]], ["zdrive.search"])
        self.assertEqual(self.emitted("capability.revoke")[0]["body"]["names"], ["kvm.relay"])
        stored = json.loads((self.workspace / ".parley" / "capabilities.json").read_text("utf-8"))
        self.assertEqual([c["name"] for c in stored["capabilities"]], ["zdrive.search"])

    def test_revoke_accepts_several_names(self):
        self.offer_registry(dict(ZDRIVE, agent_id=ME), dict(RELAY, agent_id=ME))
        code, doc = self.run_json("revoke", "--name", "kvm.relay,zdrive.search")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(doc["data"]["revoked"]), ["kvm.relay", "zdrive.search"])

    def test_revoke_needs_a_name(self):
        code, doc = self.run_json("revoke")
        self.assertEqual(code, cli.EXIT_USAGE)


# --------------------------------------------------------------------------- #
# 3. capabilities -- discovery
# --------------------------------------------------------------------------- #


class TestCapabilities(CliCase):

    def setUp(self):
        super().setUp()
        self.offer_registry(ZDRIVE, RELAY)

    def test_json_is_complete(self):
        code, doc = self.run_json("capabilities")
        self.assertEqual(code, 0)
        self.assertEnvelope(doc, "capabilities")
        data = doc["data"]
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["exclusive"], 2)
        self.assertEqual(data["dangerous"], 1)
        self.assertEqual(data["source"], "/v1/capabilities")
        self.assertEqual(data["agent_names"][PEER], "Ada")
        row = [c for c in data["capabilities"] if c["name"] == "kvm.relay"][0]
        # Everything a model needs to decide whether to ask survives the trip.
        for field in ("description", "input_schema", "safety", "kind", "output",
                      "cost", "concurrency", "exclusive", "agent_id", "agent_name"):
            self.assertIn(field, row)

    def test_falls_back_to_the_state_snapshot(self):
        self.client.endpoints.pop("/v1/capabilities")
        self.client.endpoints["/v1/state"] = {"capabilities": {"capabilities": [ZDRIVE]}}
        code, doc = self.run_json("capabilities")
        self.assertEqual(code, 0)
        self.assertEqual(doc["data"]["source"], "/v1/state")

    def test_falls_back_to_the_event_log(self):
        """A Hub with neither route still has a log, and the log is the authority."""
        self.client.endpoints.pop("/v1/capabilities")
        self.client._append(PEER, "agent.hello", {"name": "Ada", "kind": "bench"})
        self.client._append(PEER, "capability.announce", {"capabilities": [ZDRIVE, RELAY]})
        code, doc = self.run_json("capabilities")
        self.assertEqual(code, 0)
        self.assertEqual(doc["data"]["source"], "the event log")
        self.assertEqual(doc["data"]["count"], 2)
        self.assertEqual({c["agent_name"] for c in doc["data"]["capabilities"]}, {"Ada"})

    def test_filters(self):
        code, doc = self.run_json("capabilities", "--kind", "hardware")
        self.assertEqual([c["name"] for c in doc["data"]["capabilities"]], ["kvm.relay"])
        code, doc = self.run_json("capabilities", "--agent", "Ada")
        self.assertEqual(doc["data"]["count"], 2)
        self.assertEqual(doc["data"]["filters"]["agent"], PEER)
        code, doc = self.run_json("capabilities", "--safety", "safe")
        self.assertEqual([c["name"] for c in doc["data"]["capabilities"]], ["zdrive.search"])
        code, doc = self.run_json("capabilities", "--exclusive")
        self.assertEqual(doc["data"]["count"], 2)

    def test_unknown_agent_filter_is_a_usage_error(self):
        code, doc = self.run_json("capabilities", "--agent", "Nobody")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(doc["error"]["code"], "no_such_agent")

    # -- the human view ----------------------------------------------------- #
    def test_human_view_groups_and_highlights(self):
        code, out, _err = self.run_cli("capabilities")
        self.assertEqual(code, 0)
        self.assertIn("Ada", out)
        self.assertIn("EXCLUSIVE", out)
        self.assertIn("DANGEROUS", out)
        # The description is the highest-value field in the Exchange; it must not
        # be clipped to a column width.
        self.assertIn("it does not open the documents for you", out)
        self.assertIn("relay (integer, 0..9) required", out)
        self.assertIn("parley ask Ada zdrive.search", out)

    def test_empty_registry_says_how_to_fill_it(self):
        self.client.endpoints["/v1/capabilities"] = {"capabilities": []}
        code, out, _err = self.run_cli("capabilities")
        self.assertEqual(code, 0)
        self.assertIn("nobody has announced a capability yet", out)
        self.assertIn("parley offer", out)


# --------------------------------------------------------------------------- #
# 4. ask / instruct, and the exit codes that matter
# --------------------------------------------------------------------------- #


class TestAsk(CliCase):

    def setUp(self):
        super().setUp()
        self.offer_registry(ZDRIVE, RELAY)

    def reply(self, *events):
        self.client.auto_reply = list(events)

    def test_without_wait_it_prints_the_id_and_returns(self):
        code, doc = self.run_json(
            "ask", "Ada", "zdrive.search", "--input", '{"query": "DIAX04"}',
            "--reason", "I cannot reach the Z: share from here.",
        )
        self.assertEqual(code, 0)
        self.assertEnvelope(doc, "ask")
        data = doc["data"]
        self.assertTrue(data["request_id"].startswith("req_"))
        self.assertIs(data["waited"], False)
        self.assertEqual(data["state"], "pending")
        self.assertEqual(data["to"], PEER)
        body = self.emitted("request.create")[0]["body"]
        self.assertEqual(body["capability"], "zdrive.search")
        self.assertEqual(body["input"], {"query": "DIAX04"})
        self.assertEqual(body["timeout_s"], 300)

    def test_reason_is_required(self):
        code, doc = self.run_json("ask", "Ada", "zdrive.search", "--input", '{"query": "x"}')
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("reason", doc["error"]["message"])
        self.assertEqual(self.emitted("request.create"), [])

    def test_input_is_validated_against_their_schema_first(self):
        """SPEC §15.4 rule 4 is the provider's job, but catching it here saves a
        round trip and a decline."""
        code, doc = self.run_json(
            "ask", "Ada", "zdrive.search", "--input", '{"brand": "Indramat"}',
            "--reason", "why",
        )
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(doc["error"]["code"], "bad_input")
        self.assertTrue(any("query" in p for p in doc["error"]["detail"]["problems"]))
        self.assertEqual(self.emitted("request.create"), [])

    def test_no_check_sends_it_anyway(self):
        code, doc = self.run_json(
            "ask", "Ada", "zdrive.search", "--input", '{"brand": "Indramat"}',
            "--reason", "why", "--no-check",
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(self.emitted("request.create")), 1)

    def test_unknown_capability_lists_what_they_do_offer(self):
        code, doc = self.run_json("ask", "Ada", "nope.nope", "--reason", "why")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(doc["error"]["code"], "unknown_capability")
        self.assertIn("zdrive.search", doc["error"]["hint"])
        self.assertEqual(self.emitted("request.create"), [])

    def test_force_sends_despite_a_stale_registry(self):
        code, doc = self.run_json("ask", "Ada", "nope.nope", "--reason", "why", "--force")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.emitted("request.create")), 1)

    def test_bad_input_json_is_a_usage_error(self):
        code, doc = self.run_json(
            "ask", "Ada", "zdrive.search", "--input", "{not json}", "--reason", "why")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(doc["error"]["code"], "bad_json")

    def test_input_must_be_an_object(self):
        code, doc = self.run_json(
            "ask", "Ada", "zdrive.search", "--input", "[1,2]", "--reason", "why")
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_timeout_bounds(self):
        code, doc = self.run_json(
            "ask", "Ada", "zdrive.search", "--input", '{"query":"x"}',
            "--reason", "why", "--timeout", "99999999")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("86400", doc["error"]["message"])

    def test_any_is_a_legal_target(self):
        code, doc = self.run_json(
            "ask", "any", "zdrive.search", "--input", '{"query":"x"}', "--reason", "why")
        self.assertEqual(code, 0)
        self.assertEqual(doc["data"]["to"], "any")
        self.assertEqual(self.emitted("request.create")[0]["body"]["to"], "any")

    def test_unknown_agent(self):
        code, doc = self.run_json("ask", "Nobody", "zdrive.search", "--reason", "why")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(doc["error"]["code"], "no_such_agent")

    # -- --wait: the four unhappy endings and the happy one ------------------ #
    def ask_and_wait(self, *extra):
        argv = ["ask", "Ada", "zdrive.search", "--input", '{"query": "DIAX04"}',
                "--reason", "I cannot reach the Z: share.", "--wait", "--timeout", "30"]
        return self.run_json(*(argv + list(extra)))

    def test_wait_success_is_exit_zero(self):
        self.reply(("request.accept", {"eta_s": 4}),
                   ("request.result", {"ok": True, "output": {"hits": 7},
                                       "output_text": "Found 7 documents.",
                                       "files": ["handoff/out.json"], "duration_s": 3.8}))
        code, doc = self.ask_and_wait()
        self.assertEqual(code, 0)
        self.assertEnvelope(doc, "ask")
        data = doc["data"]
        self.assertIs(data["waited"], True)
        self.assertEqual(data["state"], "done")
        self.assertEqual(data["output"], {"hits": 7})
        self.assertEqual(data["output_text"], "Found 7 documents.")
        self.assertEqual(data["files"], ["handoff/out.json"])
        self.assertNotIn("error", data)

    def test_wait_declined_is_exit_one_and_says_so(self):
        self.reply(("request.decline", {"reason": "Nobody is at the bench.",
                                        "code": "needs_human"}))
        code, doc = self.ask_and_wait()
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertFalse(doc["ok"])
        data = doc["data"]
        self.assertEqual(data["state"], "declined")
        self.assertEqual(data["error"]["code"], "request_declined")
        self.assertEqual(data["error"]["detail"]["decline_code"], "needs_human")
        self.assertIn("Nobody is at the bench.", data["error"]["message"])

    def test_wait_failed_is_exit_one_with_their_error(self):
        self.reply(("request.accept", {}),
                   ("request.result", {"ok": False, "error": {
                       "code": "hardware_offline",
                       "message": "The bench 24 V supply is off.",
                       "hint": "Switch the bench on."}}))
        code, doc = self.ask_and_wait()
        self.assertEqual(code, cli.EXIT_ERROR)
        data = doc["data"]
        self.assertEqual(data["state"], "failed")
        self.assertEqual(data["error"]["code"], "request_failed")
        self.assertEqual(data["error"]["detail"]["error"]["code"], "hardware_offline")
        self.assertEqual(data["error"]["hint"], "Switch the bench on.")

    def test_wait_expired_is_exit_one_and_retryable(self):
        self.reply(("request.expired", {"was": "pending", "abandoned": False}))
        code, doc = self.ask_and_wait()
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual(doc["data"]["state"], "expired")
        self.assertEqual(doc["data"]["error"]["code"], "request_expired")
        self.assertTrue(doc["data"]["error"]["retryable"])

    def test_wait_expired_after_an_accept_names_the_abandonment(self):
        self.reply(("request.accept", {}),
                   ("request.expired", {"was": "accepted", "abandoned": True}))
        code, doc = self.ask_and_wait()
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("unforgivable", doc["data"]["error"]["hint"])

    def test_wait_cancelled_is_exit_one(self):
        req = {}

        def cancel_after_create(etype, body, event_id=None):
            event = FakeClient.emit(self.client, etype, body, event_id)
            if etype == "request.create":
                req["id"] = body["id"]
                self.client._append(ME, "request.cancel",
                                    {"id": body["id"], "reason": "found it myself"})
            return event

        self.client.emit = cancel_after_create
        code, doc = self.ask_and_wait()
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual(doc["data"]["state"], "cancelled")
        self.assertEqual(doc["data"]["error"]["code"], "request_cancelled")

    def test_wait_timeout_is_distinguishable_from_every_other_ending(self):
        """Nobody answered and this terminal gave up: the request is still live,
        so it is retryable and must not read as a decline or a failure.

        ``Requester.wait`` is stubbed rather than really waited out -- its own
        contract is "return the record even if the wait times out locally; check
        ``state``", and that return value is the only thing under test here.
        """
        from parley.client.exchange import Requester

        original = Requester.wait
        Requester.wait = lambda self, req_id, **kw: (
            self.tracker.get(req_id) or {"id": req_id, "state": "unknown"})
        self.addCleanup(lambda: setattr(Requester, "wait", original))

        code, doc = self.ask_and_wait()
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["data"]["error"]["code"], "wait_timeout")
        self.assertTrue(doc["data"]["error"]["retryable"])
        self.assertIn(doc["data"]["state"], ("pending", "accepted", "unknown"))
        # Nothing was withdrawn: the request really is still out there.
        self.assertEqual(self.emitted("request.cancel"), [])

    def test_the_four_outcome_codes_are_all_different(self):
        codes = {cli._OUTCOME_CODES[s][0]
                 for s in ("failed", "declined", "expired", "cancelled")}
        self.assertEqual(len(codes), 4)
        self.assertNotIn("", codes)

    def test_wait_narrates_progress_as_it_arrives(self):
        self.client.timed_reply = [
            ("request.accept", {"eta_s": 30}),
            ("request.progress", {"progress": 0.28, "note": "reading the index"}),
            ("request.progress", {"progress": 0.9, "note": "writing the handoff"}),
            ("request.result", {"ok": True, "output_text": "done"}),
        ]
        code, out, err = self.run_cli(
            "ask", "Ada", "zdrive.search", "--input", '{"query": "x"}',
            "--reason", "why", "--wait", "--timeout", "30")
        self.assertEqual(code, 0)
        screen = out + err
        self.assertIn("Ada accepted it", screen)
        # Live progress, one line per thing that actually changed.
        self.assertIn("reading the index", screen)
        self.assertIn("writing the handoff", screen)
        self.assertIn("28%", screen)

    def test_wait_progress_never_lands_on_stdout_in_json_mode(self):
        self.client.timed_reply = [
            ("request.accept", {}),
            ("request.progress", {"progress": 0.5, "note": "halfway"}),
            ("request.result", {"ok": True, "output_text": "done"}),
        ]
        code, out, err = self.run_cli(
            "ask", "Ada", "zdrive.search", "--input", '{"query": "x"}',
            "--reason", "why", "--wait", "--timeout", "30", "--json")
        self.assertEqual(code, 0)
        doc = json.loads(out)  # stdout is still exactly one JSON object
        self.assertEqual(doc["data"]["state"], "done")
        self.assertIn("halfway", err)
        self.assertTrue(out.lstrip().startswith("{"))

    def test_human_wait_output_names_the_outcome(self):
        self.reply(("request.decline", {"reason": "Not now.", "code": "busy"}))
        code, out, _err = self.run_cli(
            "ask", "Ada", "zdrive.search", "--input", '{"query": "x"}',
            "--reason", "why", "--wait", "--timeout", "30")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("declined", out)
        self.assertIn("Not now.", out)
        self.assertIn("busy", out)


class TestInstruct(CliCase):

    def test_instruct_posts_a_free_form_request(self):
        code, doc = self.run_json(
            "instruct", "Ada", "Power-cycle the device on bench relay 3.",
            "--reason", "Need the boot code.")
        self.assertEqual(code, 0)
        self.assertEnvelope(doc, "instruct")
        self.assertEqual(doc["data"]["timeout_s"], 600)
        self.assertEqual(doc["data"]["safety"], "guarded")
        body = self.emitted("request.create")[0]["body"]
        self.assertEqual(body["instruction"], "Power-cycle the device on bench relay 3.")
        self.assertNotIn("capability", body)
        self.assertEqual(body["expects"], "text")

    def test_instruct_needs_a_reason(self):
        code, doc = self.run_json("instruct", "Ada", "do a thing")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(self.emitted("request.create"), [])

    def test_instruct_wait_declined(self):
        self.client.auto_reply = [("request.decline", {"reason": "no", "code": "policy"})]
        code, doc = self.run_json(
            "instruct", "Ada", "do a thing", "--reason", "because", "--wait", "--timeout", "30")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual(doc["data"]["error"]["code"], "request_declined")


# --------------------------------------------------------------------------- #
# 5. requests -- the consent surface
# --------------------------------------------------------------------------- #


class TestRequests(CliCase):

    def incoming(self, *, req_id="req_7c2a91f4", capability="kvm.relay",
                 to=None, timeout_s=300, reason="Need the boot code.", **extra):
        body = {"id": req_id, "to": to or ME, "reason": reason,
                "timeout_s": timeout_s, "priority": 3}
        if capability:
            body["capability"] = capability
            body["input"] = {"relay": 3, "action": "pulse"}
        else:
            body["instruction"] = "Power-cycle bench relay 3."
        body.update(extra)
        return self.client._append(PEER, "request.create", body)

    def test_requests_json_envelope(self):
        self.incoming()
        code, doc = self.run_json("requests")
        self.assertEqual(code, 0)
        self.assertEnvelope(doc, "requests")
        data = doc["data"]
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["source"], "the event log")
        self.assertEqual(data["counts"], {"pending": 1})
        self.assertEqual(data["requests"][0]["id"], "req_7c2a91f4")

    def test_requests_reads_the_hub_route_when_it_exists(self):
        self.client.endpoints["/v1/requests"] = {
            "in_flight": [{"id": "req_aaaabbbb", "state": "pending", "from": PEER, "to": ME}],
            "recent": [{"id": "req_ccccdddd", "state": "done", "from": ME, "to": PEER}],
        }
        code, doc = self.run_json("requests")
        self.assertEqual(doc["data"]["source"], "/v1/requests")
        self.assertEqual({r["id"] for r in doc["data"]["requests"]},
                         {"req_aaaabbbb", "req_ccccdddd"})

    def test_filters(self):
        self.incoming(req_id="req_11111111")
        self.client._append(ME, "request.create",
                            {"id": "req_22222222", "to": PEER, "capability": "zdrive.search",
                             "reason": "r", "timeout_s": 300})
        code, doc = self.run_json("requests", "--mine")
        self.assertEqual([r["id"] for r in doc["data"]["requests"]], ["req_22222222"])
        code, doc = self.run_json("requests", "--to-me")
        self.assertEqual([r["id"] for r in doc["data"]["requests"]], ["req_11111111"])
        code, doc = self.run_json("requests", "--state", "pending")
        self.assertEqual(doc["data"]["count"], 2)

    def test_unknown_state_is_a_usage_error(self):
        code, doc = self.run_json("requests", "--state", "nope")
        self.assertEqual(code, cli.EXIT_USAGE)

    # -- --pending: the human-in-the-loop surface --------------------------- #
    def test_pending_shows_what_is_blocked_on_me(self):
        self.incoming()
        (self.workspace / ".parley" / "capabilities.json").write_text(
            json.dumps({"capabilities": [{k: v for k, v in RELAY.items()
                                          if k not in ("agent_id", "agent_name",
                                                       "online", "in_flight")}]}),
            encoding="utf-8")
        code, doc = self.run_json("requests", "--pending")
        self.assertEqual(code, 0)
        entry = doc["data"]["pending"][0]
        self.assertEqual(entry["id"], "req_7c2a91f4")
        self.assertEqual(entry["safety"], "dangerous")
        self.assertEqual(entry["title"], RELAY["title"])
        self.assertEqual(entry["from_name"], "Ada")
        self.assertGreater(entry["expires_in_s"], 0)
        self.assertEqual(doc["data"]["dangerous"], 1)

    def test_pending_treats_an_instruction_as_at_least_guarded(self):
        """SPEC §15.4 rule 2: nothing schema-validated it, so it is never safe."""
        self.incoming(capability=None)
        code, doc = self.run_json("requests", "--pending")
        self.assertEqual(doc["data"]["pending"][0]["safety"], "guarded")

    def test_pending_merges_the_daemons_policy_judgement(self):
        self.incoming()
        (self.workspace / ".parley" / "pending.json").write_text(json.dumps({
            "pending": [{"id": "req_7c2a91f4", "safety": "dangerous",
                         "why": "This is a dangerous capability: it needs a person here.",
                         "detail": "no rule matched",
                         "decline_code": "needs_human",
                         "deadline_ts": time.time() + 120}],
        }), encoding="utf-8")
        code, doc = self.run_json("requests", "--pending")
        entry = doc["data"]["pending"][0]
        self.assertIn("needs a person here", entry["why"])
        self.assertTrue(entry["surfaced_by_daemon"])

    def test_pending_ignores_requests_that_are_not_mine_to_decide(self):
        self.incoming(to=PEER)
        code, doc = self.run_json("requests", "--pending")
        self.assertEqual(doc["data"]["count"], 0)

    def test_pending_never_answers_anything_itself(self):
        self.incoming()
        self.run_json("requests", "--pending")
        self.assertEqual(self.emitted("request.accept"), [])
        self.assertEqual(self.emitted("request.decline"), [])

    def test_pending_human_view_carries_the_decision(self):
        self.incoming()
        (self.workspace / ".parley" / "capabilities.json").write_text(
            json.dumps({"capabilities": [{k: v for k, v in RELAY.items()
                                          if k not in ("agent_id", "agent_name",
                                                       "online", "in_flight")}]}),
            encoding="utf-8")
        code, out, _err = self.run_cli("requests", "--pending")
        self.assertEqual(code, 0)
        for fragment in ("req_7c2a91f4", "Ada", "kvm.relay", "DANGEROUS",
                         "Need the boot code.", "auto-declines in",
                         "parley accept req_7c2a91f4", "parley decline req_7c2a91f4"):
            self.assertIn(fragment, out)

    def test_empty_pending_queue_reads_as_good_news(self):
        code, out, _err = self.run_cli("requests", "--pending")
        self.assertEqual(code, 0)
        self.assertIn("nothing is waiting on your decision", out)


# --------------------------------------------------------------------------- #
# 6. accept / decline / fulfil
# --------------------------------------------------------------------------- #


class TestServicing(CliCase):

    REQ = "req_7c2a91f4"

    def setUp(self):
        super().setUp()
        self.client._append(PEER, "request.create", {
            "id": self.REQ, "to": ME, "capability": "kvm.relay",
            "input": {"relay": 3, "action": "pulse"},
            "reason": "Need the boot code.", "timeout_s": 300, "priority": 3,
        })

    def accept(self):
        return self.client._append(ME, "request.accept", {"id": self.REQ})

    # -- accept ------------------------------------------------------------- #
    def test_accept_emits_and_reports(self):
        code, doc = self.run_json("accept", self.REQ, "--eta", "45")
        self.assertEqual(code, 0)
        self.assertEnvelope(doc, "accept")
        self.assertEqual(doc["data"]["request_id"], self.REQ)
        self.assertEqual(doc["data"]["state"], "accepted")
        body = self.emitted("request.accept")[0]["body"]
        self.assertEqual(body["id"], self.REQ)
        self.assertEqual(body["eta_s"], 45.0)

    def test_accept_says_what_is_owed(self):
        code, out, _err = self.run_cli("accept", self.REQ)
        self.assertEqual(code, 0)
        self.assertIn("You now owe an answer", out)
        self.assertIn("parley fulfil %s" % self.REQ, out)

    def test_accept_unknown_request(self):
        code, doc = self.run_json("accept", "req_deadbeef")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual(doc["error"]["code"], "no_such_request")

    def test_accept_refuses_a_request_addressed_to_somebody_else(self):
        self.client._append(PEER, "request.create", {
            "id": "req_aaaabbbb", "to": "agt_someoneelse000", "capability": "kvm.relay",
            "reason": "r", "timeout_s": 300})
        code, doc = self.run_json("accept", "req_aaaabbbb")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual(doc["error"]["code"], "not_addressed_to_me")
        self.assertEqual(self.emitted("request.accept"), [])

    def test_accept_refuses_a_terminal_request(self):
        self.accept()
        self.client._append(ME, "request.result", {"id": self.REQ, "ok": True})
        code, doc = self.run_json("accept", self.REQ)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual(doc["error"]["code"], "already_terminal")

    def test_accept_reports_an_undelivered_acceptance(self):
        self.client.emit_fails = True
        code, doc = self.run_json("accept", self.REQ)
        self.assertEqual(code, cli.EXIT_NO_HUB)
        self.assertEqual(doc["error"]["code"], "not_delivered")

    # -- decline ------------------------------------------------------------ #
    def test_decline_emits_reason_and_code(self):
        code, doc = self.run_json(
            "decline", self.REQ, "--reason", "Nobody is at the bench.", "--code", "unsafe")
        self.assertEqual(code, 0)
        self.assertEnvelope(doc, "decline")
        self.assertEqual(doc["data"]["decline_code"], "unsafe")
        body = self.emitted("request.decline")[0]["body"]
        self.assertEqual(body["code"], "unsafe")
        self.assertEqual(body["reason"], "Nobody is at the bench.")

    def test_decline_defaults_to_policy(self):
        code, doc = self.run_json("decline", self.REQ, "--reason", "no")
        self.assertEqual(doc["data"]["decline_code"], "policy")

    def test_decline_rejects_an_unknown_code(self):
        code, _out, err = self.run_cli("decline", self.REQ, "--reason", "no", "--code", "nope")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("invalid choice", err)

    def test_decline_reports_an_undelivered_decline(self):
        self.client.emit_fails = True
        code, doc = self.run_json("decline", self.REQ, "--reason", "no")
        self.assertEqual(code, cli.EXIT_NO_HUB)
        self.assertEqual(doc["error"]["code"], "not_delivered")

    # -- fulfil ------------------------------------------------------------- #
    def test_fulfil_with_structured_output(self):
        self.accept()
        code, doc = self.run_json("fulfil", self.REQ, "--output", '{"pulsed_ms": 500}')
        self.assertEqual(code, 0)
        self.assertEnvelope(doc, "fulfil")
        self.assertEqual(doc["data"]["state"], "done")
        body = self.emitted("request.result")[0]["body"]
        self.assertIs(body["ok"], True)
        self.assertEqual(body["output"], {"pulsed_ms": 500})

    def test_fulfil_with_text(self):
        self.accept()
        code, doc = self.run_json("fulfil", self.REQ, "--text", "The 7-segment shows F06.")
        self.assertEqual(code, 0)
        self.assertEqual(self.emitted("request.result")[0]["body"]["output_text"],
                         "The 7-segment shows F06.")

    def test_fulfil_with_both_shapes_and_a_file(self):
        self.accept()
        (self.workspace / "handoff").mkdir()
        (self.workspace / "handoff" / "out.json").write_text("{}", encoding="utf-8")
        code, doc = self.run_json(
            "fulfil", self.REQ, "--output", '{"hits": 7}', "--text", "Found 7.",
            "--file", "handoff/out.json")
        self.assertEqual(code, 0)
        body = self.emitted("request.result")[0]["body"]
        self.assertEqual(body["output"], {"hits": 7})
        self.assertEqual(body["output_text"], "Found 7.")
        self.assertEqual(body["files"], ["handoff/out.json"])

    def test_fulfil_reads_output_from_a_file(self):
        self.accept()
        path = self.workspace / "result.json"
        path.write_text(json.dumps({"paths": ["Z:\\x"]}), encoding="utf-8")
        code, doc = self.run_json("fulfil", self.REQ, "--output", "@" + str(path))
        self.assertEqual(code, 0)
        self.assertEqual(self.emitted("request.result")[0]["body"]["output"],
                         {"paths": ["Z:\\x"]})

    def test_fulfil_fail_reports_an_honest_failure(self):
        self.accept()
        code, doc = self.run_json(
            "fulfil", self.REQ, "--fail", "--error", "The bench 24 V supply is off.",
            "--error-code", "hardware_offline", "--hint", "Switch the bench on.")
        self.assertEqual(code, 0)
        self.assertEqual(doc["data"]["state"], "failed")
        body = self.emitted("request.result")[0]["body"]
        self.assertIs(body["ok"], False)
        self.assertEqual(body["error"]["code"], "hardware_offline")
        self.assertEqual(body["error"]["hint"], "Switch the bench on.")

    def test_fail_needs_an_error(self):
        self.accept()
        code, doc = self.run_json("fulfil", self.REQ, "--fail")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(self.emitted("request.result"), [])

    def test_fulfil_needs_something_to_say(self):
        self.accept()
        code, doc = self.run_json("fulfil", self.REQ)
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("--output", doc["error"]["hint"])
        self.assertEqual(self.emitted("request.result"), [])

    def test_fulfil_refuses_a_file_outside_the_workspace(self):
        self.accept()
        code, doc = self.run_json("fulfil", self.REQ, "--text", "x", "--file", "/etc/hostname")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(doc["error"]["code"], "bad_path")
        self.assertEqual(self.emitted("request.result"), [])

    def test_fulfil_accepts_a_still_pending_request_first(self):
        """SPEC §15.3 wants an accept before a result; doing both in one command
        is what an operator servicing a request by hand actually does."""
        code, doc = self.run_json("fulfil", self.REQ, "--text", "done")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.emitted("request.accept")), 1)
        self.assertEqual(len(self.emitted("request.result")), 1)

    def test_fulfil_refuses_a_terminal_request(self):
        self.accept()
        self.client._append(ME, "request.result", {"id": self.REQ, "ok": True})
        code, doc = self.run_json("fulfil", self.REQ, "--text", "again")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual(doc["error"]["code"], "already_terminal")

    def test_fulfil_refuses_a_request_held_by_someone_else(self):
        self.client._append(PEER, "request.create", {
            "id": "req_aaaabbbb", "to": "any", "capability": "kvm.relay",
            "reason": "r", "timeout_s": 300})
        self.client._append("agt_thirdparty00000", "request.accept", {"id": "req_aaaabbbb"})
        code, doc = self.run_json("fulfil", "req_aaaabbbb", "--text", "mine now")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual(doc["error"]["code"], "already_taken")

    def test_fulfil_reports_an_undelivered_result(self):
        self.accept()
        self.client.emit_fails = True
        code, doc = self.run_json("fulfil", self.REQ, "--text", "x")
        self.assertEqual(code, cli.EXIT_NO_HUB)
        self.assertEqual(doc["error"]["code"], "not_delivered")

    def test_a_one_shot_command_never_rewrites_the_daemons_sidecars(self):
        """.parley/pending.json is a running `parley run`'s consent queue, and
        this process cannot see the half of it that lives in memory."""
        pending = self.workspace / ".parley" / "pending.json"
        pending.write_text('{"pending": [{"id": "req_other0", "why": "mine"}]}',
                           encoding="utf-8")
        before = pending.read_text("utf-8")
        self.run_json("accept", self.REQ)
        self.assertEqual(pending.read_text("utf-8"), before)


# --------------------------------------------------------------------------- #
# 7. The small pieces the screens are built from
# --------------------------------------------------------------------------- #


class TestHelpers(unittest.TestCase):

    def test_epoch_parses_the_wire_timestamp(self):
        self.assertAlmostEqual(cli._epoch("1970-01-01T00:00:10.000Z"), 10.0, places=3)
        self.assertAlmostEqual(cli._epoch("1970-01-01T00:00:10.5Z"), 10.5, places=3)
        self.assertEqual(cli._epoch(""), 0.0)
        self.assertEqual(cli._epoch("not a date"), 0.0)
        self.assertEqual(cli._epoch(None), 0.0)

    def test_epoch_keeps_request_ages_honest(self):
        """Folding a log with one `now` would make every request look brand new,
        and `auto-declines in 4 min` would be a lie."""
        self.assertGreater(cli._epoch("2026-10-08T12:00:01.000Z"),
                           cli._epoch("2026-10-08T12:00:00.000Z"))

    def test_merge_request_rows(self):
        merged = cli._merge_request_rows({
            "in_flight": [{"id": "a"}], "recent": [{"id": "b"}, {"id": "a"}]})
        self.assertEqual([r["id"] for r in merged], ["a", "b"])
        self.assertIsNone(cli._merge_request_rows({"nothing": 1}))

    def test_schema_fields(self):
        fields = cli._schema_fields(RELAY["input_schema"])
        self.assertIn("relay (integer, 0..9) required", fields)
        self.assertIn('action ("on"|"off"|"pulse") required', fields)
        self.assertEqual(cli._schema_fields(None), [])

    def test_example_input_is_valid_against_the_schema(self):
        from parley.exchange import validate_input

        sample = json.loads(cli._example_input(ZDRIVE["input_schema"]))
        self.assertEqual(validate_input(ZDRIVE["input_schema"], sample), [])
        sample = json.loads(cli._example_input(RELAY["input_schema"]))
        self.assertEqual(validate_input(RELAY["input_schema"], sample), [])

    def test_safety_text_fails_closed_on_an_unknown_level(self):
        term = cli.Term(io.StringIO(), colour=False, unicode=False)
        self.assertIn("DANGEROUS", cli._safety_text(term, "sfae"))
        self.assertIn("DANGEROUS", cli._safety_text(term, "dangerous"))
        self.assertEqual(cli._safety_text(term, "safe"), "safe")

    def test_safety_consequences_are_spelled_out(self):
        self.assertEqual(set(cli._SAFETY_CONSEQUENCE), set(cli.SAFETY_LEVELS))
        self.assertIn("NEVER auto-accepted", cli._SAFETY_CONSEQUENCE["dangerous"])

    def test_render_capabilities_survives_a_hostile_registry(self):
        term = cli.Term(io.StringIO(), colour=False, unicode=False)
        rows = [{"name": "x", "safety": "sfae", "input_schema": "not a schema",
                 "description": None, "agent_id": None}]
        lines = cli.render_capabilities(term, rows)
        self.assertTrue(lines)

    def test_render_pending_survives_a_hostile_entry(self):
        term = cli.Term(io.StringIO(), colour=False, unicode=False)
        lines = cli.render_pending(term, [{"id": None, "reason": None, "input": None,
                                           "safety": None, "expires_in_s": None}])
        self.assertTrue(lines)

    def test_json_argument_forms(self):
        self.assertEqual(cli._json_argument('{"a": 1}', "--input"), {"a": 1})
        self.assertIsNone(cli._json_argument(None, "--input"))
        with self.assertRaises(cli.CliError) as caught:
            cli._json_argument("{oops", "--input")
        self.assertEqual(caught.exception.exit_code, cli.EXIT_USAGE)

    def test_not_delivered_is_exit_four_and_says_it_is_safe_to_retry(self):
        err = cli._not_delivered("result", "req_1")
        self.assertEqual(err.exit_code, cli.EXIT_NO_HUB)
        self.assertIn("safe to repeat", err.hint)

    def test_is_fatal_transport(self):
        self.assertTrue(cli._is_fatal_transport(errors.BadSignature("no")))
        self.assertTrue(cli._is_fatal_transport(OSError("connection refused")))
        # A Hub that simply does not serve the route yet is not fatal.
        self.assertFalse(cli._is_fatal_transport(errors.NoSuchBlob("no such route")))

    def test_capability_validate_is_what_offer_leans_on(self):
        self.assertEqual(Capability(
            name="a.b", title="T", kind="mcp", description="D", safety="safe").validate(), [])
        problems = Capability(name="A.B", title="", kind="nope", description="").validate()
        self.assertGreaterEqual(len(problems), 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
