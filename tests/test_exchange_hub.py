"""SPEC §15 — the Exchange as the Hub serves it: snapshot, discovery, expiry.

``parley/exchange.py`` already decides what the Exchange *means*; nothing here
re-tests that. What is tested here is the part only the Hub can get wrong.

**Incremental must equal rebuilt.** ``StateView`` folds every event as it is
appended, and ``rebuild()`` folds the whole log again from scratch through a
*different* code path (``build_registry`` for the catalogue, a fresh
``RequestTracker`` for the state machine). If those two ever disagree, a Hub that
restarts shows a different parley from the one that was running a moment earlier,
and nobody would be able to say which of them was right. One test folds the same
log both ways and demands the same snapshot.

**Expiry is load-bearing, so it is tested for firing exactly once.** SPEC §9 makes
``request.expired`` the sole trigger for the Ledger's only negative term. An
expiry that never fires lets a provider abandon accepted work for free; one that
fires twice charges for one abandonment twice. The test takes the penalty all the
way into the Ledger line, because that is the thing the event exists for.

**The viewer tests assert an absence.** A viewer token is the Deck, and the Deck
must be able to show who asked whom for what without being handed the arguments
or the answer. Several tests therefore assert that a field is *not* there.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from parley import crypto, exchange, protocol
from parley.hub import api
from parley.hub.server import create_parley
from tests.helpers import SECRETS, decode_json


def cap_dict(name="z.search", **over):
    """A capability announcement that passes ``Capability.validate``."""
    body = {
        "name": name,
        "title": "Search the company Z: technical library",
        "kind": "mcp",
        "description": (
            "Full-text search over manuals, schematics and firmware dumps. "
            "Returns canonical Z:\\ paths. Does not fetch the files themselves."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "output": "json",
        "safety": "safe",
        "cost": "cheap",
        "concurrency": 2,
        "exclusive": True,
        "avg_duration_s": 4,
    }
    body.update(over)
    return body


class ExchangeHubTestCase(unittest.TestCase):
    """A real Hub, driven through ``api.handle`` — no socket, no Deck, no client."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.hub, self.watchword = create_parley(self.workspace, name="exchange-test", port=0)
        self.addCleanup(self._shutdown)
        self.session = self.hub.config.session
        root_key = crypto.derive_root_key(
            self.watchword, self.session,
            iterations=getattr(self.hub.config, "pbkdf2_iterations", 200000),
        )
        self.enroll_key = crypto.enroll_key(root_key)
        SECRETS.add_secret(self.watchword, "watchword")

    def _shutdown(self):
        try:
            self.hub.stop()
        except Exception:
            pass

    # ---------------------------------------------------------------- plumbing

    def call(self, method, path, body=None, *, agent="", key=None, headers=None, sign=True):
        raw = b"" if body is None else (
            body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        )
        request_headers = {"Content-Type": "application/json; charset=utf-8"}
        if sign and key is not None:
            stamp = str(int(time.time()))
            nonce = crypto.new_nonce_hex()
            sts = crypto.string_to_sign(method, path, raw, stamp, nonce, self.session, agent)
            request_headers.update({
                "X-Parley-Version": "PARLEY/1",
                "X-Parley-Session": self.session,
                "X-Parley-Agent": agent,
                "X-Parley-Timestamp": stamp,
                "X-Parley-Nonce": nonce,
                "Authorization": "Parley-HMAC-SHA256 " + crypto.sign(key, sts),
            })
        request_headers.update(headers or {})
        status, _headers, response = api.handle(self.hub, method, path, request_headers, raw)
        parsed = decode_json(response)
        if status >= 400:
            SECRETS.record("{0} {1} -> {2}".format(method, path, status), response)
        return status, parsed

    def enrol(self, name="Ada"):
        body = {"session": self.session, "agent": {
            "name": name, "kind": "claude-code", "model": "claude-opus-5", "os": "linux",
            "host": "workbench", "client_version": "1.0.0", "capabilities": ["chat"],
        }}
        status, payload = self.call("POST", "/v1/enroll", body,
                                    agent="enroll", key=self.enroll_key)
        self.assertEqual(status, 201, payload)
        SECRETS.add_secret(payload["agent_key"], "agent_key")
        return payload["agent_id"], bytes.fromhex(payload["agent_key"])

    def viewer_token(self):
        return self.hub.mint_viewer_token(label="test-deck")

    # -- appending ----------------------------------------------------------

    def emit(self, actor, etype, body):
        """Append one agent-authored event in process (used for state fixtures)."""
        return self.hub.submit(protocol.make_event(actor, self.session, etype, dict(body)))

    def post(self, agent_id, key, etype, body):
        """Append one event the way an agent really does: signed, over the API."""
        event = protocol.make_event(agent_id, self.session, etype, dict(body))
        return self.call("POST", "/v1/events", event, agent=agent_id, key=key)

    def announce(self, agent_id, caps):
        return self.emit(agent_id, "capability.announce", {"capabilities": list(caps)})

    def ask(self, requester, to, *, capability="z.search", req_id="", timeout_s=300, **over):
        body = exchange.make_request(
            requester, to, capability=capability, input={"query": "DIAX04"},
            reason="I cannot reach the Z: share from this machine.",
            timeout_s=timeout_s, req_id=req_id or exchange.new_request_id(), **over
        )
        self.emit(requester, "request.create", body)
        return body["id"]


# --------------------------------------------------------------------- snapshot


class TestSnapshotShape(ExchangeHubTestCase):
    def test_the_snapshot_carries_the_two_exchange_blocks(self):
        ada, _ = self.enrol("Ada")
        self.announce(ada, [cap_dict()])
        snap = self.hub.view.snapshot()

        self.assertIn("capabilities", snap)
        self.assertIn("requests", snap)
        self.assertEqual(sorted(snap["capabilities"]), ["capabilities", "count"])
        self.assertEqual(sorted(snap["requests"]), ["counts", "in_flight", "recent"])

    def test_a_capability_row_has_everything_the_deck_needs(self):
        ada, _ = self.enrol("Ada")
        self.announce(ada, [cap_dict()])
        rows = self.hub.view.snapshot()["capabilities"]["capabilities"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        for field in ("name", "title", "kind", "description", "safety", "cost",
                      "concurrency", "exclusive", "agent_id", "agent_name",
                      "online", "in_flight"):
            self.assertIn(field, row)
        self.assertEqual(row["agent_id"], ada)
        self.assertEqual(row["agent_name"], "Ada")
        self.assertTrue(row["online"])
        self.assertEqual(row["in_flight"], 0)

    def test_counts_cover_every_request_state(self):
        counts = self.hub.view.snapshot()["requests"]["counts"]
        self.assertEqual(sorted(counts), sorted(exchange.REQUEST_STATES))

    def test_in_flight_is_counted_per_capability_not_per_agent(self):
        ada, _ = self.enrol("Ada")
        bob, _ = self.enrol("Bob")
        self.announce(bob, [cap_dict("z.search"), cap_dict("z.read")])
        req = self.ask(ada, bob, capability="z.search")
        self.emit(bob, "request.accept", {"id": req})

        rows = {r["name"]: r for r in self.hub.view.capabilities()["capabilities"]}
        self.assertEqual(rows["z.search"]["in_flight"], 1)
        # The other capability is idle: an agent's load must not be reported as
        # every one of its capabilities being that busy.
        self.assertEqual(rows["z.read"]["in_flight"], 0)

    def test_a_pending_request_is_not_in_flight_work(self):
        ada, _ = self.enrol("Ada")
        bob, _ = self.enrol("Bob")
        self.announce(bob, [cap_dict()])
        self.ask(ada, bob)
        rows = self.hub.view.capabilities()["capabilities"]
        self.assertEqual(rows[0]["in_flight"], 0)

    def test_going_offline_withdraws_the_catalogue(self):
        ada, _ = self.enrol("Ada")
        self.announce(ada, [cap_dict()])
        self.assertEqual(self.hub.view.capabilities()["count"], 1)
        self.hub.hub_event("agent.offline", {"agent_id": ada, "reason": "timeout"})
        self.assertEqual(self.hub.view.capabilities()["count"], 0)

    def test_announce_replaces_the_whole_catalogue(self):
        ada, _ = self.enrol("Ada")
        self.announce(ada, [cap_dict("z.search"), cap_dict("z.read")])
        self.announce(ada, [cap_dict("z.read")])
        names = [r["name"] for r in self.hub.view.capabilities()["capabilities"]]
        self.assertEqual(names, ["z.read"])

    def test_revoke_withdraws_one_capability(self):
        ada, _ = self.enrol("Ada")
        self.announce(ada, [cap_dict("z.search"), cap_dict("z.read")])
        self.emit(ada, "capability.revoke", {"names": ["z.search"]})
        names = [r["name"] for r in self.hub.view.capabilities()["capabilities"]]
        self.assertEqual(names, ["z.read"])

    def test_a_delegation_edge_appears_in_the_graph(self):
        ada, _ = self.enrol("Ada")
        bob, _ = self.enrol("Bob")
        self.announce(bob, [cap_dict()])
        self.ask(ada, bob)
        edges = self.hub.view.snapshot()["graph"]["edges"]
        match = [e for e in edges if e["source"] == ada and e["target"] == bob]
        self.assertEqual(len(match), 1, edges)
        self.assertEqual(match[0]["kinds"]["delegation"], 1)
        for kind in ("reply", "citation", "co_edit", "blocked_on", "delegation"):
            self.assertIn(kind, match[0]["kinds"])

    def test_delegation_weight_is_request_volume(self):
        ada, _ = self.enrol("Ada")
        bob, _ = self.enrol("Bob")
        self.announce(bob, [cap_dict()])
        for _ in range(3):
            self.ask(ada, bob)
        edges = self.hub.view.snapshot()["graph"]["edges"]
        match = [e for e in edges if e["source"] == ada and e["target"] == bob][0]
        self.assertEqual(match["kinds"]["delegation"], 3)

    def test_an_any_request_draws_its_edge_only_when_somebody_accepts(self):
        ada, _ = self.enrol("Ada")
        bob, _ = self.enrol("Bob")
        self.announce(bob, [cap_dict()])
        req = self.ask(ada, "any")
        self.assertEqual(self.hub.view.snapshot()["graph"]["edges"], [])
        self.emit(bob, "request.accept", {"id": req})
        edges = self.hub.view.snapshot()["graph"]["edges"]
        match = [e for e in edges if e["source"] == ada and e["target"] == bob]
        self.assertEqual(len(match), 1, edges)
        self.assertEqual(match[0]["kinds"]["delegation"], 1)


# ------------------------------------------------------- incremental == rebuilt


class TestIncrementalEqualsRebuild(ExchangeHubTestCase):
    """The single most valuable test for a materialised view."""

    def _busy_log(self):
        ada, ada_key = self.enrol("Ada")
        bob, bob_key = self.enrol("Bob")
        cass, _ = self.enrol("Cass")

        self.announce(ada, [cap_dict("z.search"), cap_dict("z.read", exclusive=False)])
        self.announce(bob, [cap_dict("kvm.relay", kind="hardware", safety="dangerous")])
        self.announce(cass, [cap_dict("gpu.embed", kind="compute")])
        self.emit(cass, "capability.revoke", {"names": ["gpu.embed"]})

        done = self.ask(ada, bob, capability="kvm.relay")
        self.emit(bob, "request.accept", {"id": done, "eta_s": 12})
        self.emit(bob, "request.progress", {"id": done, "progress": 0.5, "note": "relay closed"})
        self.emit(bob, "request.result", {
            "id": done, "ok": True, "output": {"segment": "Bb"},
            "output_text": "The display shows Bb.", "duration_s": 4.2,
        })

        declined = self.ask(bob, ada, capability="z.search")
        self.emit(ada, "request.decline", {"id": declined, "reason": "no", "code": "busy"})

        pending = self.ask(cass, ada, capability="z.read")
        accepted = self.ask(cass, ada, capability="z.search")
        self.emit(ada, "request.accept", {"id": accepted})

        offered = self.ask(ada, "any", capability="z.read")
        self.emit(bob, "request.accept", {"id": offered})
        self.emit(cass, "request.accept", {"id": offered})  # loses the race

        self.emit(ada, "chat.message", {"text": "anyone seen the DIAX04 manual?"})
        self.hub.hub_event("agent.offline", {"agent_id": cass, "reason": "timeout"})
        return {"pending": pending, "accepted": accepted, "done": done, "offered": offered}

    @staticmethod
    def _stable(snap):
        """The snapshot minus what legitimately moves between two reads."""
        out = dict(snap)
        out.pop("server_time", None)
        # The Ledger is recomputed from the log on demand and stamps itself with
        # the wall clock; it is not part of what a rebuild has to reproduce.
        out.pop("ledger", None)
        agents = []
        for agent in out.get("agents", []):
            agent = dict(agent)
            psr = agent.get("psr")
            if isinstance(psr, dict):
                psr = dict(psr)
                psr.pop("age_s", None)
                psr.pop("stale", None)
                agent["psr"] = psr
            agents.append(agent)
        out["agents"] = agents
        return out

    def test_a_rebuilt_view_is_the_view_it_replaced(self):
        self._busy_log()
        incremental = self._stable(self.hub.view.snapshot())
        self.hub.view.rebuild()
        rebuilt = self._stable(self.hub.view.snapshot())
        self.assertEqual(json.dumps(incremental, sort_keys=True),
                         json.dumps(rebuilt, sort_keys=True))

    def test_the_catalogue_survives_a_rebuild(self):
        self._busy_log()
        before = self.hub.view.capabilities()
        self.hub.view.rebuild()
        self.assertEqual(before, self.hub.view.capabilities())
        self.assertEqual(sorted(r["name"] for r in before["capabilities"]),
                         ["kvm.relay", "z.read", "z.search"])

    def test_request_states_survive_a_rebuild(self):
        ids = self._busy_log()
        self.hub.view.rebuild()
        self.assertEqual(self.hub.view.request_state(ids["done"]), "done")
        self.assertEqual(self.hub.view.request_state(ids["pending"]), "pending")
        self.assertEqual(self.hub.view.request_state(ids["accepted"]), "accepted")

    def test_a_restarted_hub_reaches_the_same_snapshot(self):
        self._busy_log()
        before = self._stable(self.hub.view.snapshot())
        state_dir = self.hub.state_dir
        self.hub.stop()
        from parley.hub.server import load_parley

        reopened = load_parley(state_dir, workspace=self.workspace)
        self.addCleanup(reopened.stop)
        after = self._stable(reopened.view.snapshot())
        self.assertEqual(after["capabilities"], before["capabilities"])
        self.assertEqual(after["requests"], before["requests"])


# ----------------------------------------------------------------------- expiry


class TestExpiry(ExchangeHubTestCase):
    def test_an_unanswered_request_expires_exactly_once(self):
        ada, _ = self.enrol("Ada")
        bob, _ = self.enrol("Bob")
        self.announce(bob, [cap_dict()])
        req = self.ask(ada, bob, timeout_s=60)

        self.assertEqual(self.hub.sweep_exchange(time.time()), [])
        later = time.time() + 61
        fired = self.hub.sweep_exchange(later)
        self.assertEqual(len(fired), 1, fired)
        self.assertEqual(fired[0]["type"], "request.expired")
        self.assertEqual(fired[0]["actor"], "hub")
        self.assertEqual(fired[0]["body"]["id"], req)
        self.assertFalse(fired[0]["body"]["abandoned"])
        self.assertEqual(self.hub.view.request_state(req), "expired")

        # Twice would charge one abandonment twice.
        self.assertEqual(self.hub.sweep_exchange(later + 600), [])

    def test_an_accepted_request_expires_as_abandoned(self):
        ada, _ = self.enrol("Ada")
        bob, _ = self.enrol("Bob")
        self.announce(bob, [cap_dict()])
        req = self.ask(ada, bob, timeout_s=30)
        self.emit(bob, "request.accept", {"id": req})

        fired = self.hub.sweep_exchange(time.time() + 31)
        self.assertEqual(len(fired), 1, fired)
        body = fired[0]["body"]
        self.assertTrue(body["abandoned"])
        self.assertEqual(body["provider"], bob)
        self.assertEqual(body["was"], "accepted")

    def test_an_answered_request_never_expires(self):
        ada, _ = self.enrol("Ada")
        bob, _ = self.enrol("Bob")
        self.announce(bob, [cap_dict()])
        req = self.ask(ada, bob, timeout_s=5)
        self.emit(bob, "request.accept", {"id": req})
        self.emit(bob, "request.result", {"id": req, "ok": True, "output": {"paths": []}})
        self.assertEqual(self.hub.sweep_exchange(time.time() + 600), [])
        self.assertEqual(self.hub.view.request_state(req), "done")

    def test_the_expiry_is_what_the_ledger_charges_for(self):
        ada, _ = self.enrol("Ada")
        bob, _ = self.enrol("Bob")
        self.announce(bob, [cap_dict()])
        req = self.ask(ada, bob, timeout_s=10)
        self.emit(bob, "request.accept", {"id": req})

        before = self.hub.view.ledger(force=True)
        bob_before = [l for l in before["lines"] if l["agent_id"] == bob]
        self.assertEqual([l["components"]["service"] for l in bob_before] or [0.0], [0.0])

        self.hub.sweep_exchange(time.time() + 11)
        after = self.hub.view.ledger(force=True)
        line = [l for l in after["lines"] if l["agent_id"] == bob][0]
        self.assertLess(line["components"]["service"], 0.0)
        penalties = [e for e in line["evidence"]["service"] if e.get("penalty")]
        self.assertEqual(len(penalties), 1, line["evidence"]["service"])
        # SPEC R6: a penalty the user cannot trace is not allowed.
        self.assertEqual(penalties[0]["request"], req)
        self.assertIn("never answered", penalties[0]["label"])

    def test_the_requester_is_not_charged_for_its_own_unanswered_request(self):
        ada, _ = self.enrol("Ada")
        bob, _ = self.enrol("Bob")
        self.announce(bob, [cap_dict()])
        self.ask(ada, bob, timeout_s=10)   # never accepted by anybody
        self.hub.sweep_exchange(time.time() + 11)
        result = self.hub.view.ledger(force=True)
        for line in result["lines"]:
            self.assertGreaterEqual(line["components"]["service"], 0.0)

    def test_expiry_is_hub_authored_and_signed(self):
        ada, _ = self.enrol("Ada")
        bob, _ = self.enrol("Bob")
        req = self.ask(ada, bob, timeout_s=1)
        fired = self.hub.sweep_exchange(time.time() + 2)
        stored = fired[0]
        self.assertEqual(stored["actor"], "hub")
        self.assertTrue(stored.get("sig"))
        self.assertTrue(crypto.verify_event(self.hub.root_keys()[0], stored))
        self.assertEqual(stored["body"]["id"], req)

    def test_an_agent_may_not_forge_an_expiry(self):
        ada, ada_key = self.enrol("Ada")
        bob, _ = self.enrol("Bob")
        req = self.ask(bob, ada, timeout_s=600)
        self.emit(ada, "request.accept", {"id": req})

        status, payload = self.post(ada, ada_key, "request.expired", {
            "id": req, "abandoned": True, "provider": bob,
        })
        self.assertEqual(status, 422, payload)
        self.assertEqual(payload["error"]["code"], "bad_event")
        self.assertEqual(self.hub.view.request_state(req), "accepted")

    def test_an_agent_may_not_forge_a_taken(self):
        ada, ada_key = self.enrol("Ada")
        status, payload = self.post(ada, ada_key, "request.taken",
                                    {"id": exchange.new_request_id(), "by": ada})
        self.assertEqual(status, 422, payload)
        self.assertEqual(payload["error"]["code"], "bad_event")


# ------------------------------------------------------------------ the any race


class TestAnyRace(ExchangeHubTestCase):
    def _log_of(self, etype):
        return [e for e in self.hub.store.read(0, 10000) if e["type"] == etype]

    def test_one_winner_and_a_taken_for_every_loser(self):
        ada, _ = self.enrol("Ada")
        bob, bob_key = self.enrol("Bob")
        cass, cass_key = self.enrol("Cass")
        dave, dave_key = self.enrol("Dave")
        for who in (bob, cass, dave):
            self.announce(who, [cap_dict()])

        req = self.ask(ada, "any")
        self.post(bob, bob_key, "request.accept", {"id": req})
        self.post(cass, cass_key, "request.accept", {"id": req})
        self.post(dave, dave_key, "request.accept", {"id": req})

        record = self.hub.view.requests(state=["accepted"])["in_flight"]
        self.assertEqual(len(record), 1)
        self.assertEqual(record[0]["id"], req)
        self.assertEqual(record[0]["accepted_by"], bob)

        taken = self._log_of("request.taken")
        self.assertEqual(len(taken), 2, taken)
        self.assertEqual({e["body"]["late"] for e in taken}, {cass, dave})
        for event in taken:
            self.assertEqual(event["actor"], "hub")
            self.assertEqual(event["body"]["id"], req)
            self.assertEqual(event["body"]["by"], bob)
            self.assertTrue(event["body"]["reason"])

    def test_a_taken_is_emitted_on_the_append_that_lost(self):
        ada, _ = self.enrol("Ada")
        bob, bob_key = self.enrol("Bob")
        cass, cass_key = self.enrol("Cass")
        req = self.ask(ada, "any")
        self.post(bob, bob_key, "request.accept", {"id": req})
        self.assertEqual(self._log_of("request.taken"), [])

        head_before = self.hub.store.head_seq()
        self.post(cass, cass_key, "request.accept", {"id": req})
        taken = self._log_of("request.taken")
        self.assertEqual(len(taken), 1)
        # No reaper tick needed: it lands with the accept that lost.
        self.assertGreater(taken[0]["seq"], head_before)

    def test_the_winner_repeating_itself_is_not_a_loser(self):
        ada, _ = self.enrol("Ada")
        bob, bob_key = self.enrol("Bob")
        req = self.ask(ada, "any")
        self.post(bob, bob_key, "request.accept", {"id": req})
        self.post(bob, bob_key, "request.accept", {"id": req})
        self.assertEqual(self._log_of("request.taken"), [])


# -------------------------------------------------------------------- endpoints


class TestCapabilitiesEndpoint(ExchangeHubTestCase):
    def test_an_agent_reads_the_merged_registry(self):
        ada, ada_key = self.enrol("Ada")
        bob, _ = self.enrol("Bob")
        self.announce(ada, [cap_dict("z.search")])
        self.announce(bob, [cap_dict("kvm.relay", kind="hardware", safety="dangerous")])

        status, payload = self.call("GET", "/v1/capabilities", agent=ada, key=ada_key)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["count"], 2)
        by_name = {r["name"]: r for r in payload["capabilities"]}
        self.assertEqual(by_name["z.search"]["agent_id"], ada)
        self.assertEqual(by_name["kvm.relay"]["agent_name"], "Bob")
        self.assertEqual(by_name["kvm.relay"]["safety"], "dangerous")

    def test_a_viewer_sees_the_same_catalogue(self):
        ada, ada_key = self.enrol("Ada")
        self.announce(ada, [cap_dict()])
        _status, agent_view = self.call("GET", "/v1/capabilities", agent=ada, key=ada_key)
        status, viewer_view = self.call(
            "GET", "/v1/capabilities?vt=" + self.viewer_token(), sign=False
        )
        self.assertEqual(status, 200, viewer_view)
        self.assertEqual(viewer_view, agent_view)

    def test_it_needs_a_credential(self):
        status, payload = self.call("GET", "/v1/capabilities", sign=False)
        self.assertEqual(status, 401, payload)

    def test_it_is_read_only(self):
        ada, ada_key = self.enrol("Ada")
        status, payload = self.call("POST", "/v1/capabilities", {}, agent=ada, key=ada_key)
        self.assertEqual(status, 405, payload)


class TestRequestsEndpoint(ExchangeHubTestCase):
    def setUp(self):
        super().setUp()
        self.ada, self.ada_key = self.enrol("Ada")
        self.bob, self.bob_key = self.enrol("Bob")
        self.announce(self.bob, [cap_dict()])
        self.pending = self.ask(self.ada, self.bob)
        self.finished = self.ask(self.ada, self.bob)
        self.emit(self.bob, "request.accept", {"id": self.finished})
        self.emit(self.bob, "request.result", {
            "id": self.finished, "ok": True,
            "output": {"paths": ["Z:\\Indramat\\DIAX04\\manual.pdf"]},
            "output_text": "Found the 1997 commissioning manual.",
        })
        self.other = self.ask(self.bob, self.ada, capability="z.read")

    def get(self, query="", **kw):
        return self.call("GET", "/v1/requests" + query, agent=self.ada, key=self.ada_key, **kw)

    def test_it_returns_the_documented_shape(self):
        status, payload = self.get()
        self.assertEqual(status, 200, payload)
        for field in ("in_flight", "recent", "counts"):
            self.assertIn(field, payload)
        ids = [r["id"] for r in payload["in_flight"]]
        self.assertIn(self.pending, ids)
        self.assertEqual([r["id"] for r in payload["recent"]], [self.finished])
        self.assertEqual(payload["counts"]["done"], 1)
        self.assertEqual(payload["counts"]["pending"], 2)

    def test_the_state_filter(self):
        _status, payload = self.get("?state=done")
        self.assertEqual(payload["in_flight"], [])
        self.assertEqual([r["id"] for r in payload["recent"]], [self.finished])

    def test_several_states_at_once(self):
        _status, payload = self.get("?state=pending,done")
        self.assertEqual(len(payload["in_flight"]), 2)
        self.assertEqual(len(payload["recent"]), 1)

    def test_the_to_filter(self):
        _status, payload = self.get("?to=" + self.bob)
        self.assertEqual([r["id"] for r in payload["in_flight"]], [self.pending])

    def test_the_from_filter(self):
        _status, payload = self.get("?from=" + self.bob)
        self.assertEqual([r["id"] for r in payload["in_flight"]], [self.other])

    def test_the_filters_combine(self):
        _status, payload = self.get("?from=" + self.ada + "&to=" + self.bob + "&state=pending")
        self.assertEqual([r["id"] for r in payload["in_flight"]], [self.pending])

    def test_an_unknown_state_is_an_error_not_an_empty_answer(self):
        status, payload = self.get("?state=accepeted")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "bad_request")
        self.assertIn("accepted", payload["error"]["hint"])

    def test_the_counts_are_the_session_tally_not_the_filtered_one(self):
        _status, payload = self.get("?state=done")
        self.assertEqual(payload["counts"]["pending"], 2)

    def test_it_is_read_only(self):
        status, payload = self.call("POST", "/v1/requests", {},
                                    agent=self.ada, key=self.ada_key)
        self.assertEqual(status, 405, payload)


class TestViewerVisibility(ExchangeHubTestCase):
    def setUp(self):
        super().setUp()
        self.ada, self.ada_key = self.enrol("Ada")
        self.bob, _ = self.enrol("Bob")
        self.announce(self.bob, [cap_dict()])
        self.req = self.ask(self.ada, self.bob)
        self.emit(self.bob, "request.accept", {"id": self.req})
        self.emit(self.bob, "request.result", {
            "id": self.req, "ok": False,
            "output": {"secret": "Z:\\customer\\pricing.xlsx"},
            "output_text": "the share is unreachable",
            "files": ["handoff/search.json"],
            "error": {"code": "other", "message": "SMB mount /mnt/z is gone",
                      "hint": "remount it"},
        })
        self.free = self.ask(self.ada, self.bob, capability="",
                             instruction="Power-cycle relay 3 and read the 7-segment.",
                             expects="text")

    def viewer_requests(self):
        status, payload = self.call(
            "GET", "/v1/requests?vt=" + self.viewer_token(), sign=False
        )
        self.assertEqual(status, 200, payload)
        return payload

    def test_a_viewer_sees_who_asked_whom_for_what(self):
        payload = self.viewer_requests()
        record = payload["recent"][0]
        self.assertEqual(record["id"], self.req)
        self.assertEqual(record["from"], self.ada)
        self.assertEqual(record["to"], self.bob)
        self.assertEqual(record["capability"], "z.search")
        self.assertEqual(record["state"], "failed")
        self.assertIn("reason", record)
        self.assertIn("timeout_s", record)
        self.assertTrue(record["redacted"])

    def test_a_viewer_never_sees_the_input_or_the_output(self):
        payload = self.viewer_requests()
        blob = json.dumps(payload)
        self.assertNotIn("DIAX04", blob)
        self.assertNotIn("pricing.xlsx", blob)
        self.assertNotIn("the share is unreachable", blob)
        self.assertNotIn("handoff/search.json", blob)
        # A free-form instruction is request content too.
        self.assertNotIn("Power-cycle relay 3", blob)

    def test_a_viewer_sees_that_it_failed_but_not_the_provider_internals(self):
        record = self.viewer_requests()["recent"][0]
        self.assertEqual(record["error"], {"code": "other"})
        self.assertNotIn("SMB mount", json.dumps(record))

    def test_an_agent_sees_the_whole_record(self):
        status, payload = self.call("GET", "/v1/requests", agent=self.ada, key=self.ada_key)
        self.assertEqual(status, 200, payload)
        record = payload["recent"][0]
        self.assertEqual(record["input"], {"query": "DIAX04"})
        self.assertEqual(record["output_text"], "the share is unreachable")
        self.assertEqual(record["files"], ["handoff/search.json"])
        self.assertNotIn("redacted", record)

    def test_the_state_snapshot_redacts_for_a_viewer_too(self):
        blob = json.dumps(self.hub.view.snapshot(for_viewer=True)["requests"])
        self.assertNotIn("DIAX04", blob)
        self.assertIn(self.bob, blob)
        self.assertIn("DIAX04", json.dumps(self.hub.view.snapshot()["requests"]))

    def test_a_viewer_still_cannot_write(self):
        event = protocol.make_event(self.ada, self.session, "chat.message", {"text": "hi"})
        status, payload = self.call(
            "POST", "/v1/events?vt=" + self.viewer_token(), event, sign=False
        )
        self.assertEqual(status, 403, payload)


# ------------------------------------------------------------------ rate limits


class TestRequestCreateRateLimit(ExchangeHubTestCase):
    def test_the_twenty_first_request_in_a_minute_is_refused(self):
        ada, ada_key = self.enrol("Ada")
        bob, _ = self.enrol("Bob")
        self.announce(bob, [cap_dict()])

        for index in range(20):
            body = exchange.make_request(
                ada, bob, capability="z.search", input={"query": "q%d" % index},
                reason="because the Z: share is not reachable from here",
            )
            status, payload = self.post(ada, ada_key, "request.create", body)
            self.assertEqual(status, 201, (index, payload))

        body = exchange.make_request(
            ada, bob, capability="z.search", input={"query": "one too many"},
            reason="because the Z: share is not reachable from here",
        )
        status, payload = self.post(ada, ada_key, "request.create", body)
        self.assertEqual(status, 429, payload)
        self.assertEqual(payload["error"]["code"], "rate_limited")
        self.assertIn("15.6", payload["error"]["hint"])
        self.assertGreaterEqual(payload["error"]["detail"]["retry_after_s"], 1)

    def test_the_limit_is_per_agent(self):
        ada, ada_key = self.enrol("Ada")
        bob, bob_key = self.enrol("Bob")
        for _ in range(20):
            body = exchange.make_request(ada, bob, capability="z.search",
                                         reason="a perfectly good reason")
            self.assertEqual(self.post(ada, ada_key, "request.create", body)[0], 201)
        self.assertEqual(self.post(ada, ada_key, "request.create", exchange.make_request(
            ada, bob, capability="z.search", reason="a perfectly good reason"))[0], 429)
        # Bob has not spent anything.
        self.assertEqual(self.post(bob, bob_key, "request.create", exchange.make_request(
            bob, ada, capability="z.search", reason="a perfectly good reason"))[0], 201)

    def test_other_event_types_are_not_charged_to_it(self):
        ada, ada_key = self.enrol("Ada")
        for index in range(25):
            status, payload = self.post(ada, ada_key, "chat.message",
                                        {"text": "note %d" % index})
            self.assertEqual(status, 201, payload)

    def test_a_batch_costs_what_it_contains(self):
        ada, ada_key = self.enrol("Ada")
        bob, _ = self.enrol("Bob")
        events = [
            protocol.make_event(ada, self.session, "request.create", exchange.make_request(
                ada, bob, capability="z.search", input={"query": "q%d" % i},
                reason="a perfectly good reason"))
            for i in range(21)
        ]
        status, payload = self.call("POST", "/v1/events", {"events": events},
                                    agent=ada, key=ada_key)
        self.assertEqual(status, 429, payload)
        # Nothing in the batch landed.
        self.assertEqual(self.hub.view.snapshot()["requests"]["counts"]["pending"], 0)


# ------------------------------------------------------------ ingest validation


class TestAnnounceValidation(ExchangeHubTestCase):
    def announce_over_http(self, caps):
        ada, ada_key = getattr(self, "_agent", (None, None))
        if ada is None:
            ada, ada_key = self.enrol("Ada")
            self._agent = (ada, ada_key)
        return ada, self.post(ada, ada_key, "capability.announce", {"capabilities": caps})

    def test_a_good_announcement_is_accepted(self):
        _ada, (status, payload) = self.announce_over_http([cap_dict()])
        self.assertEqual(status, 201, payload)
        self.assertEqual(self.hub.view.capabilities()["count"], 1)

    def test_a_capability_with_no_description_is_rejected(self):
        cap = cap_dict()
        del cap["description"]
        ada, (status, payload) = self.announce_over_http([cap])
        self.assertEqual(status, 422, payload)
        self.assertEqual(payload["error"]["code"], "bad_event")
        self.assertIn("description", json.dumps(payload["error"]["detail"]))
        self.assertTrue(payload["error"]["hint"])
        # Rejected means registered nothing, not "registered the good ones".
        self.assertEqual(self.hub.view.capabilities()["count"], 0)

    def test_a_misdeclared_safety_level_is_rejected(self):
        _ada, (status, payload) = self.announce_over_http([cap_dict(safety="sfae")])
        self.assertEqual(status, 422, payload)
        self.assertIn("safety", json.dumps(payload["error"]["detail"]))

    def test_an_uppercase_name_is_rejected(self):
        _ada, (status, payload) = self.announce_over_http([cap_dict("Z.Search")])
        self.assertEqual(status, 422, payload)
        self.assertIn("lowercase", json.dumps(payload["error"]["detail"]))

    def test_an_unknown_kind_is_rejected(self):
        _ada, (status, payload) = self.announce_over_http([cap_dict(kind="wetware")])
        self.assertEqual(status, 422, payload)

    def test_a_broken_input_schema_is_rejected(self):
        _ada, (status, payload) = self.announce_over_http(
            [cap_dict(input_schema={"type": 7})]
        )
        self.assertEqual(status, 422, payload)
        self.assertIn("input_schema", json.dumps(payload["error"]["detail"]))

    def test_one_bad_capability_rejects_the_whole_announcement(self):
        ada, (status, payload) = self.announce_over_http([cap_dict("z.search"), cap_dict("")])
        self.assertEqual(status, 422, payload)
        self.assertEqual(self.hub.view.capabilities()["count"], 0)

    def test_a_duplicate_name_is_rejected(self):
        _ada, (status, payload) = self.announce_over_http([cap_dict("z.search"),
                                                           cap_dict("z.search")])
        self.assertEqual(status, 422, payload)
        self.assertIn("unique", json.dumps(payload["error"]["detail"]))

    def test_the_capabilities_field_must_be_a_list(self):
        ada, ada_key = self.enrol("Ada")
        status, payload = self.post(ada, ada_key, "capability.announce",
                                    {"capabilities": {"z.search": {}}})
        self.assertEqual(status, 422, payload)
        self.assertEqual(payload["error"]["code"], "bad_event")

    def test_an_empty_catalogue_is_how_an_agent_withdraws(self):
        ada, ada_key = self.enrol("Ada")
        self.assertEqual(self.post(ada, ada_key, "capability.announce",
                                   {"capabilities": [cap_dict()]})[0], 201)
        self.assertEqual(self.post(ada, ada_key, "capability.announce",
                                   {"capabilities": []})[0], 201)
        self.assertEqual(self.hub.view.capabilities()["count"], 0)

    def test_an_absurd_catalogue_is_refused(self):
        ada, ada_key = self.enrol("Ada")
        caps = [cap_dict("z.search%d" % i) for i in range(200)]
        status, payload = self.post(ada, ada_key, "capability.announce",
                                    {"capabilities": caps})
        self.assertEqual(status, 413, payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
