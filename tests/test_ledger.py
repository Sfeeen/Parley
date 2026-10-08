"""SPEC §9 — the Ledger.

Covers the five components, the R6 evidence requirement, the degenerate cases, and the
gaming resistance: the defences only matter if they are nailed down by a test, because an
autonomous agent reading the spec will try every one of them.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from parley import ledger
from tests.helpers import AGENT_A, AGENT_B, AGENT_C, ev

W = ledger.DEFAULT_WEIGHTS


def hello(seq, agent, name):
    return ev(seq=seq, actor=agent, etype="agent.hello", body={"name": name})


def contribution(seq, agent, kind, title="t", event_id="", **body):
    payload = {"kind": kind, "title": title}
    payload.update(body)
    return ev(
        seq=seq,
        actor=agent,
        etype="knowledge.contribution",
        body=payload,
        event_id=event_id or "evt_c{0:014x}".format(seq),
    )


def chat(seq, agent, text="hi", refs=None, event_id=""):
    body = {"text": text}
    if refs is not None:
        body["refs"] = refs
    return ev(
        seq=seq,
        actor=agent,
        etype="chat.message",
        body=body,
        event_id=event_id or "evt_m{0:014x}".format(seq),
    )


def file_record(author, lines=10, seq=1, **extra):
    record = {
        "hash": "sha256:" + "ab" * 32,
        "size": 100,
        "author": author,
        "seq": seq,
        "ts": "2026-10-08T12:00:00.000Z",
        "lines": lines,
    }
    record.update(extra)
    return record


class TestLineCounting(unittest.TestCase):
    def test_count_lines_ignores_a_trailing_newline(self):
        self.assertEqual(ledger.count_lines(b"a\nb\n"), 2)
        self.assertEqual(ledger.count_lines(b"a\nb"), 2)

    def test_empty_file_has_zero_lines(self):
        self.assertEqual(ledger.count_lines(b""), 0)

    def test_binary_content_reports_no_line_count(self):
        self.assertIsNone(ledger.count_lines(b"\x89PNG\r\n\x1a\n\x00\x00"))
        self.assertTrue(ledger.is_binary(b"\x00"))
        self.assertFalse(ledger.is_binary(b"plain text\n"))

    def test_nul_beyond_the_sniff_window_is_still_treated_as_text(self):
        # The heuristic is deliberately bounded: it must not read a 25 MiB blob to decide.
        data = b"x" * 9000 + b"\x00"
        self.assertFalse(ledger.is_binary(data))


class TestContributions(unittest.TestCase):
    def test_each_kind_scores_its_published_weight(self):
        events = [hello(1, AGENT_A, "Ada")]
        seq = 2
        for kind in W["contribution_weights"]:
            events.append(contribution(seq, AGENT_A, kind))
            seq += 1
        result = ledger.compute(events, {})
        expected = sum(W["contribution_weights"].values())
        self.assertAlmostEqual(result.lines[0].components["contributions"], expected)

    def test_an_unweighted_kind_scores_nothing_but_is_still_explained(self):
        events = [hello(1, AGENT_A, "Ada"), contribution(2, AGENT_A, "vibes")]
        line = ledger.compute(events, {}).lines[0]
        self.assertEqual(line.components["contributions"], 0.0)
        entries = line.evidence["contributions"]
        self.assertEqual(len(entries), 1)
        self.assertIn("vibes", entries[0]["label"])
        self.assertEqual(entries[0]["points"], 0.0)

    def test_evidence_names_the_seq_of_every_scoring_event(self):
        events = [hello(1, AGENT_A, "Ada"), contribution(7, AGENT_A, "decision", "pick SSE")]
        line = ledger.compute(events, {}).lines[0]
        entry = line.evidence["contributions"][0]
        self.assertEqual(entry["seq"], 7)
        self.assertEqual(entry["points"], float(W["contribution_weights"]["decision"]))
        self.assertIn("pick SSE", entry["label"])
        self.assertEqual(entry["id"], "evt_c00000000000007")

    def test_evidence_points_sum_to_the_component_total(self):
        events = [hello(1, AGENT_A, "Ada")]
        for i, kind in enumerate(("decision", "design", "fix", "answer")):
            events.append(contribution(2 + i, AGENT_A, kind))
        line = ledger.compute(events, {}).lines[0]
        for component in ledger.COMPONENTS:
            total = sum(e["points"] for e in line.evidence[component])
            self.assertAlmostEqual(total, line.components[component], places=6,
                                   msg="evidence must account for every point in " + component)


class TestSupersedes(unittest.TestCase):
    def test_a_superseded_contribution_does_not_double_count(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            contribution(2, AGENT_A, "design", "v1", event_id="evt_first"),
            contribution(3, AGENT_A, "design", "v2", supersedes="evt_first"),
        ]
        line = ledger.compute(events, {}).lines[0]
        self.assertEqual(line.components["contributions"], float(W["contribution_weights"]["design"]))

    def test_a_supersede_chain_collapses_to_its_tip(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            contribution(2, AGENT_A, "decision", "v1", event_id="evt_1"),
            contribution(3, AGENT_A, "decision", "v2", event_id="evt_2", supersedes="evt_1"),
            contribution(4, AGENT_A, "decision", "v3", event_id="evt_3", supersedes="evt_2"),
        ]
        line = ledger.compute(events, {}).lines[0]
        self.assertEqual(line.components["contributions"], float(W["contribution_weights"]["decision"]))

    def test_a_superseded_contribution_still_appears_in_the_evidence(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            contribution(2, AGENT_A, "design", "v1", event_id="evt_first"),
            contribution(3, AGENT_A, "design", "v2", supersedes="evt_first"),
        ]
        labels = [e["label"] for e in ledger.compute(events, {}).lines[0].evidence["contributions"]]
        self.assertTrue(any("superseded" in label for label in labels),
                        "R6: a zero must be explained, not silently omitted")

    def test_another_agent_cannot_supersede_your_contribution_away(self):
        # Gaming: without the same-actor rule, B could delete A from the scoreboard.
        events = [
            hello(1, AGENT_A, "Ada"),
            hello(2, AGENT_B, "Bob"),
            contribution(3, AGENT_A, "decision", "mine", event_id="evt_mine"),
            contribution(4, AGENT_B, "answer", "nope", supersedes="evt_mine"),
        ]
        result = ledger.compute(events, {})
        self.assertEqual(result.line(AGENT_A).components["contributions"],
                         float(W["contribution_weights"]["decision"]))

    def test_restating_one_contribution_many_times_pays_once(self):
        # Fifty restatements that all supersede the same original are one contribution,
        # not fifty-one. A chain and a fan-out must collapse the same way.
        events = [hello(1, AGENT_A, "Ada"),
                  contribution(2, AGENT_A, "decision", "v1", event_id="evt_root")]
        for i in range(50):
            events.append(contribution(3 + i, AGENT_A, "decision", "again",
                                       event_id="evt_r{0}".format(i),
                                       supersedes="evt_root"))
        line = ledger.compute(events, {}).lines[0]
        self.assertEqual(line.components["contributions"],
                         float(W["contribution_weights"]["decision"]))

    def test_only_the_newest_member_of_a_supersede_group_scores(self):
        events = [hello(1, AGENT_A, "Ada"),
                  contribution(2, AGENT_A, "decision", "v1", event_id="evt_root"),
                  contribution(5, AGENT_A, "decision", "v2", event_id="evt_mid",
                               supersedes="evt_root"),
                  contribution(9, AGENT_A, "decision", "v3", event_id="evt_tip",
                               supersedes="evt_root")]
        entries = ledger.compute(events, {}).lines[0].evidence["contributions"]
        scoring = [e for e in entries if e["points"] > 0]
        self.assertEqual([e["id"] for e in scoring], ["evt_tip"])

    def test_superseding_an_unknown_event_is_harmless(self):
        events = [hello(1, AGENT_A, "Ada"),
                  contribution(2, AGENT_A, "fix", "x", supersedes="evt_nonexistent")]
        line = ledger.compute(events, {}).lines[0]
        self.assertEqual(line.components["contributions"], float(W["contribution_weights"]["fix"]))


class TestAuthoredSubstance(unittest.TestCase):
    def test_surviving_lines_score_at_the_published_rate(self):
        files = {"a.py": file_record(AGENT_A, lines=50, seq=4)}
        line = ledger.compute([hello(1, AGENT_A, "Ada")], files).lines[0]
        self.assertAlmostEqual(line.components["authored"], 50 * W["surviving_line_points"])

    def test_lines_are_capped_per_file(self):
        files = {"big.py": file_record(AGENT_A, lines=100000, seq=4)}
        line = ledger.compute([hello(1, AGENT_A, "Ada")], files).lines[0]
        cap = W["surviving_lines_cap_per_file"]
        self.assertAlmostEqual(line.components["authored"], cap * W["surviving_line_points"])
        self.assertIn("capped", line.evidence["authored"][0]["label"])

    def test_binary_files_contribute_no_lines(self):
        files = {"logo.png": file_record(AGENT_A, lines=None, seq=4, binary=True)}
        line = ledger.compute([hello(1, AGENT_A, "Ada")], files).lines[0]
        self.assertEqual(line.components["authored"], 0.0)
        self.assertIn("binary", line.evidence["authored"][0]["label"])

    def test_a_file_without_a_recorded_line_count_scores_nothing_and_says_so(self):
        files = {"mystery.bin": {"hash": "sha256:x", "size": 1, "author": AGENT_A, "seq": 2}}
        line = ledger.compute([hello(1, AGENT_A, "Ada")], files).lines[0]
        self.assertEqual(line.components["authored"], 0.0)
        self.assertIn("no line count", line.evidence["authored"][0]["label"])

    def test_the_author_is_the_last_writer_not_the_first(self):
        # blame-lite: whoever wrote the version that is standing now owns its lines.
        events = [
            hello(1, AGENT_A, "Ada"),
            hello(2, AGENT_B, "Bob"),
            ev(seq=3, actor=AGENT_A, etype="file.put", body={"path": "x.py", "hash": "h1"}),
            ev(seq=4, actor=AGENT_B, etype="file.put", body={"path": "x.py", "hash": "h2"}),
        ]
        files = {"x.py": {"hash": "h2", "size": 1, "seq": 4, "lines": 10}}
        result = ledger.compute(events, files)
        self.assertEqual(result.line(AGENT_B).components["authored"], 10 * W["surviving_line_points"])
        self.assertEqual(result.line(AGENT_A).components["authored"], 0.0)

    def test_a_file_authored_by_an_agent_that_left_still_scores_for_that_agent(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            ev(seq=2, actor=AGENT_A, etype="file.put", body={"path": "x.py", "hash": "h"}),
            ev(seq=3, actor=AGENT_A, etype="agent.bye", body={"reason": "done"}),
            ev(seq=4, actor="hub", etype="agent.offline",
               body={"agent_id": AGENT_A, "reason": "bye"}),
        ]
        files = {"x.py": file_record(AGENT_A, lines=20, seq=2)}
        line = ledger.compute(events, files).line(AGENT_A)
        self.assertIsNotNone(line, "an agent that left must keep its entry")
        self.assertAlmostEqual(line.components["authored"], 20 * W["surviving_line_points"])

    def test_an_author_never_seen_in_the_log_still_gets_a_line(self):
        files = {"legacy.py": file_record(AGENT_C, lines=5, seq=1)}
        result = ledger.compute([], files)
        self.assertEqual([ln.agent_id for ln in result.lines], [AGENT_C])
        self.assertEqual(result.lines[0].name, AGENT_C, "with no hello, the id is the name")

    def test_a_conflict_sidecar_is_not_counted_twice(self):
        files = {
            "x.py": file_record(AGENT_A, lines=10, seq=5),
            "x.py.parley-conflict-5518aa-abc123": file_record(AGENT_A, lines=10, seq=5),
        }
        line = ledger.compute([hello(1, AGENT_A, "Ada")], files).lines[0]
        self.assertAlmostEqual(line.components["authored"], 10 * W["surviving_line_points"],
                               msg="SPEC §7.6 sidecars are the same content preserved twice")

    def test_a_deleted_file_stops_paying(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            ev(seq=2, actor=AGENT_A, etype="file.put", body={"path": "x.py", "hash": "h"}),
            ev(seq=3, actor=AGENT_A, etype="file.delete", body={"path": "x.py"}),
        ]
        line = ledger.compute(events, {}).line(AGENT_A)
        self.assertEqual(line.components["authored"], 0.0)

    def test_hub_authored_files_never_score(self):
        files = {"notice.txt": file_record("hub", lines=10, seq=2)}
        result = ledger.compute([hello(1, AGENT_A, "Ada")], files)
        self.assertNotIn("hub", [ln.agent_id for ln in result.lines])
        self.assertEqual(result.line(AGENT_A).components["authored"], 0.0)


class TestDelivery(unittest.TestCase):
    def test_a_claimed_task_pays_on_done(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            ev(seq=2, actor=AGENT_A, etype="task.claim", body={"id": "tsk_4b19ac72"}),
            ev(seq=3, actor=AGENT_A, etype="task.done", body={"id": "tsk_4b19ac72"}),
        ]
        line = ledger.compute(events, {}).lines[0]
        self.assertEqual(line.components["delivery"], W["task_done_points"])

    def test_closing_a_task_you_never_claimed_pays_nothing(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            hello(2, AGENT_B, "Bob"),
            ev(seq=3, actor=AGENT_A, etype="task.claim", body={"id": "tsk_1"}),
            ev(seq=4, actor=AGENT_B, etype="task.done", body={"id": "tsk_1"}),
        ]
        result = ledger.compute(events, {})
        self.assertEqual(result.line(AGENT_B).components["delivery"], 0.0)
        self.assertIn("not claimed", result.line(AGENT_B).evidence["delivery"][0]["label"])

    def test_the_same_task_cannot_be_delivered_twice(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            ev(seq=2, actor=AGENT_A, etype="task.claim", body={"id": "tsk_1"}),
            ev(seq=3, actor=AGENT_A, etype="task.done", body={"id": "tsk_1"}),
            ev(seq=4, actor=AGENT_A, etype="task.done", body={"id": "tsk_1"}),
            ev(seq=5, actor=AGENT_A, etype="task.done", body={"id": "tsk_1"}),
        ]
        line = ledger.compute(events, {}).lines[0]
        self.assertEqual(line.components["delivery"], W["task_done_points"])

    def test_releasing_a_task_gives_up_the_delivery_points(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            ev(seq=2, actor=AGENT_A, etype="task.claim", body={"id": "tsk_1"}),
            ev(seq=3, actor=AGENT_A, etype="task.release", body={"id": "tsk_1"}),
            ev(seq=4, actor=AGENT_A, etype="task.done", body={"id": "tsk_1"}),
        ]
        self.assertEqual(ledger.compute(events, {}).lines[0].components["delivery"], 0.0)

    def test_a_reclaim_by_another_agent_moves_the_delivery_points(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            hello(2, AGENT_B, "Bob"),
            ev(seq=3, actor=AGENT_A, etype="task.claim", body={"id": "tsk_1"}),
            ev(seq=4, actor=AGENT_B, etype="task.claim", body={"id": "tsk_1"}),
            ev(seq=5, actor=AGENT_B, etype="task.done", body={"id": "tsk_1"}),
        ]
        result = ledger.compute(events, {})
        self.assertEqual(result.line(AGENT_B).components["delivery"], W["task_done_points"])
        self.assertEqual(result.line(AGENT_A).components["delivery"], 0.0)


class TestInfluence(unittest.TestCase):
    def test_a_citation_from_another_agent_pays_the_cited_agent(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            hello(2, AGENT_B, "Bob"),
            contribution(3, AGENT_A, "finding", "leak in sync", event_id="evt_find"),
            chat(4, AGENT_B, "good catch", refs=[{"kind": "event", "value": "evt_find"}]),
        ]
        result = ledger.compute(events, {})
        self.assertEqual(result.line(AGENT_A).components["influence"], W["citation_received_points"])
        self.assertEqual(result.line(AGENT_B).components["influence"], 0.0)

    def test_self_citation_scores_exactly_nothing(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            contribution(2, AGENT_A, "finding", "mine", event_id="evt_mine"),
            chat(3, AGENT_A, "as I said", refs=[{"kind": "event", "value": "evt_mine"}]),
            chat(4, AGENT_A, "as I said", refs=[{"kind": "event", "value": "evt_mine"}]),
            chat(5, AGENT_A, "as I said", refs=[{"kind": "event", "value": "evt_mine"}]),
        ]
        line = ledger.compute(events, {}).lines[0]
        self.assertEqual(line.components["influence"], 0.0)
        self.assertEqual(line.evidence["influence"], [])

    def test_self_citation_of_your_own_file_scores_nothing(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            ev(seq=2, actor=AGENT_A, etype="file.put", body={"path": "a.py", "hash": "h"}),
            chat(3, AGENT_A, "see a.py", refs=[{"kind": "file", "value": "a.py"}]),
        ]
        files = {"a.py": file_record(AGENT_A, lines=1, seq=2)}
        self.assertEqual(ledger.compute(events, files).lines[0].components["influence"], 0.0)

    def test_citing_another_agents_file_pays_that_agent(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            hello(2, AGENT_B, "Bob"),
            ev(seq=3, actor=AGENT_A, etype="file.put", body={"path": "a.py", "hash": "h"}),
            chat(4, AGENT_B, "see a.py", refs=[{"kind": "file", "value": "a.py"}]),
        ]
        files = {"a.py": file_record(AGENT_A, lines=1, seq=3)}
        result = ledger.compute(events, files)
        self.assertEqual(result.line(AGENT_A).components["influence"], W["citation_received_points"])

    def test_one_event_citing_the_same_target_twice_counts_once(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            hello(2, AGENT_B, "Bob"),
            contribution(3, AGENT_A, "doc", "x", event_id="evt_x"),
            chat(4, AGENT_B, "x", refs=[{"kind": "event", "value": "evt_x"}] * 25),
        ]
        result = ledger.compute(events, {})
        self.assertEqual(result.line(AGENT_A).components["influence"], W["citation_received_points"])

    def test_refs_on_other_event_types_do_not_feed_influence(self):
        # SPEC §9 names chat.message.refs and knowledge.contribution.refs, and only those.
        events = [
            hello(1, AGENT_A, "Ada"),
            hello(2, AGENT_B, "Bob"),
            contribution(3, AGENT_A, "doc", "x", event_id="evt_x"),
            ev(seq=4, actor=AGENT_B, etype="task.done",
               body={"id": "tsk_1", "refs": [{"kind": "event", "value": "evt_x"}]}),
        ]
        self.assertEqual(ledger.compute(events, {}).line(AGENT_A).components["influence"], 0.0)

    def test_the_evidence_records_who_cited_and_from_where(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            hello(2, AGENT_B, "Bob"),
            contribution(3, AGENT_A, "doc", "x", event_id="evt_x"),
            chat(9, AGENT_B, "x", refs=[{"kind": "event", "value": "evt_x"}]),
        ]
        entry = ledger.compute(events, {}).line(AGENT_A).evidence["influence"][0]
        self.assertEqual(entry["cited_by"], AGENT_B)
        self.assertEqual(entry["citing_seq"], 9)
        self.assertEqual(entry["ref_value"], "evt_x")

    def test_a_citation_of_an_unknown_event_pays_nobody(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            hello(2, AGENT_B, "Bob"),
            chat(3, AGENT_B, "x", refs=[{"kind": "event", "value": "evt_ghost"}]),
        ]
        for line in ledger.compute(events, {}).lines:
            self.assertEqual(line.components["influence"], 0.0)

    def test_malformed_refs_are_ignored_without_crashing(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            ev(seq=2, actor=AGENT_B, etype="chat.message",
               body={"text": "x", "refs": ["not-an-object", {"value": 7}, {"kind": "task"},
                                            {"kind": "event"}, None]}),
        ]
        result = ledger.compute(events, {})
        self.assertEqual(sum(ln.components["influence"] for ln in result.lines), 0.0)


class TestPresence(unittest.TestCase):
    def test_chat_pays_the_published_rate(self):
        events = [hello(1, AGENT_A, "Ada")] + [chat(2 + i, AGENT_A) for i in range(4)]
        line = ledger.compute(events, {}).lines[0]
        self.assertAlmostEqual(line.components["presence"], 4 * W["chat_message_points"])

    def test_chat_spam_cannot_exceed_the_hard_cap(self):
        # The headline gaming defence: 5000 messages must not outscore one decision stack.
        events = [hello(1, AGENT_A, "Ada")] + [chat(2 + i, AGENT_A) for i in range(5000)]
        line = ledger.compute(events, {}).lines[0]
        self.assertAlmostEqual(line.components["presence"], W["chat_points_cap"])
        self.assertLessEqual(line.components["presence"], W["chat_points_cap"])

    def test_the_cap_is_explained_in_the_evidence(self):
        events = [hello(1, AGENT_A, "Ada")] + [chat(2 + i, AGENT_A) for i in range(900)]
        entries = ledger.compute(events, {}).lines[0].evidence["presence"]
        self.assertTrue(any("cap" in e["label"] for e in entries),
                        "R6: the user must be able to see why further chat scored nothing")
        self.assertAlmostEqual(sum(e["points"] for e in entries), W["chat_points_cap"])

    def test_the_cap_is_per_agent_not_global(self):
        events = [hello(1, AGENT_A, "Ada"), hello(2, AGENT_B, "Bob")]
        seq = 3
        for _ in range(400):
            events.append(chat(seq, AGENT_A))
            seq += 1
            events.append(chat(seq, AGENT_B))
            seq += 1
        result = ledger.compute(events, {})
        for line in result.lines:
            self.assertAlmostEqual(line.components["presence"], W["chat_points_cap"])

    def test_chat_cannot_beat_substance(self):
        spam = [hello(1, AGENT_A, "Ada")] + [chat(2 + i, AGENT_A) for i in range(10000)]
        substance = [hello(1, AGENT_B, "Bob")] + [
            contribution(2 + i, AGENT_B, "decision") for i in range(2)
        ]
        result = ledger.compute(spam + [dict(e, seq=e["seq"] + 20000) for e in substance], {})
        self.assertGreater(result.line(AGENT_B).total, 0.0)
        self.assertLessEqual(result.line(AGENT_A).total, W["chat_points_cap"])


class TestShares(unittest.TestCase):
    def test_shares_sum_to_exactly_one_hundred(self):
        events = [hello(1, AGENT_A, "Ada"), hello(2, AGENT_B, "Bob"), hello(3, AGENT_C, "Cy")]
        events += [contribution(4, AGENT_A, "decision"), contribution(5, AGENT_B, "answer"),
                   contribution(6, AGENT_C, "fix")]
        result = ledger.compute(events, {})
        self.assertEqual(round(sum(ln.share for ln in result.lines), 6), 100.0)

    def test_shares_sum_to_one_hundred_for_an_awkward_three_way_split(self):
        # 1/3 each is the classic rounding trap: 33.33 * 3 = 99.99 without apportionment.
        events = [hello(i + 1, a, a) for i, a in enumerate((AGENT_A, AGENT_B, AGENT_C))]
        events += [contribution(4 + i, a, "answer") for i, a in
                   enumerate((AGENT_A, AGENT_B, AGENT_C))]
        result = ledger.compute(events, {})
        self.assertEqual(round(sum(ln.share for ln in result.lines), 6), 100.0)

    def test_shares_sum_to_one_hundred_across_many_agents(self):
        events = []
        agents = ["agt_{0:016x}".format(i) for i in range(16)]
        for i, agent in enumerate(agents):
            events.append(hello(i + 1, agent, "a{0}".format(i)))
            events.append(contribution(100 + i, agent, "answer"))
        result = ledger.compute(events, {})
        self.assertEqual(len(result.lines), 16)
        self.assertEqual(round(sum(ln.share for ln in result.lines), 6), 100.0)

    def test_shares_are_sane_when_every_total_is_zero(self):
        events = [hello(1, AGENT_A, "Ada"), hello(2, AGENT_B, "Bob")]
        result = ledger.compute(events, {})
        self.assertEqual([ln.total for ln in result.lines], [0.0, 0.0])
        self.assertEqual(round(sum(ln.share for ln in result.lines), 6), 100.0)
        self.assertEqual(sorted(ln.share for ln in result.lines), [50.0, 50.0])

    def test_a_single_agent_holds_the_whole_share(self):
        events = [hello(1, AGENT_A, "Ada"), contribution(2, AGENT_A, "decision")]
        result = ledger.compute(events, {})
        self.assertEqual(len(result.lines), 1)
        self.assertEqual(result.lines[0].share, 100.0)

    def test_lines_are_ordered_by_total_descending(self):
        events = [
            hello(1, AGENT_A, "Ada"), hello(2, AGENT_B, "Bob"), hello(3, AGENT_C, "Cy"),
            contribution(4, AGENT_B, "decision"),
            contribution(5, AGENT_C, "design"),
            contribution(6, AGENT_A, "answer"),
        ]
        result = ledger.compute(events, {})
        self.assertEqual([ln.agent_id for ln in result.lines], [AGENT_B, AGENT_C, AGENT_A])


class TestDegenerateCases(unittest.TestCase):
    def test_zero_events_and_no_files(self):
        result = ledger.compute([], {})
        self.assertEqual(result.lines, [])
        self.assertEqual(result.event_count, 0)
        self.assertEqual(result.to_dict()["total_points"], 0.0)

    def test_an_agent_with_no_contributions_at_all_still_has_a_row(self):
        events = [hello(1, AGENT_A, "Ada"), hello(2, AGENT_B, "Bob"),
                  contribution(3, AGENT_A, "decision")]
        result = ledger.compute(events, {})
        bob = result.line(AGENT_B)
        self.assertIsNotNone(bob)
        self.assertEqual(bob.total, 0.0)
        self.assertEqual(set(bob.components), set(ledger.COMPONENTS))
        self.assertEqual(set(bob.evidence), set(ledger.COMPONENTS))

    def test_the_hub_is_never_a_participant(self):
        events = [
            ev(seq=1, actor="hub", etype="hub.started", body={}),
            ev(seq=2, actor="hub", etype="hub.notice", body={"text": "skipped big.bin"}),
            hello(3, AGENT_A, "Ada"),
        ]
        result = ledger.compute(events, {})
        self.assertEqual([ln.agent_id for ln in result.lines], [AGENT_A])

    def test_unknown_extension_events_are_ignored_gracefully(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            ev(seq=2, actor=AGENT_A, etype="x.vendor.thing", body={"anything": [1, 2, 3]}),
            ev(seq=3, actor=AGENT_A, etype="x.other", body={"refs": "not a list"}),
        ]
        result = ledger.compute(events, {})
        self.assertEqual(result.lines[0].total, 0.0)

    def test_events_missing_fields_do_not_crash_the_scorer(self):
        events = [
            {},
            {"type": "chat.message"},
            {"actor": AGENT_A},
            {"actor": AGENT_A, "type": "chat.message", "body": "not an object"},
            {"actor": AGENT_A, "type": "knowledge.contribution", "body": {"kind": None}},
            {"actor": None, "type": "chat.message", "body": {}},
            {"actor": AGENT_A, "type": None},
            hello(1, AGENT_A, "Ada"),
        ]
        result = ledger.compute(events, {})
        self.assertEqual([ln.agent_id for ln in result.lines], [AGENT_A])

    def test_events_are_scored_in_seq_order_regardless_of_iteration_order(self):
        ordered = [
            hello(1, AGENT_A, "Ada"),
            ev(seq=2, actor=AGENT_A, etype="task.claim", body={"id": "tsk_1"}),
            ev(seq=3, actor=AGENT_A, etype="task.done", body={"id": "tsk_1"}),
        ]
        shuffled = [ordered[2], ordered[0], ordered[1]]
        self.assertEqual(
            ledger.compute(shuffled, {}).to_dict(), ledger.compute(ordered, {}).to_dict()
        )

    def test_compute_is_pure_and_repeatable(self):
        events = [hello(1, AGENT_A, "Ada"), contribution(2, AGENT_A, "decision"),
                  chat(3, AGENT_A, "hi")]
        files = {"a.py": file_record(AGENT_A, lines=7, seq=2)}
        first = ledger.compute(events, files).to_dict()
        second = ledger.compute(events, files).to_dict()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_compute_does_not_mutate_its_arguments(self):
        events = [hello(1, AGENT_A, "Ada"), contribution(2, AGENT_A, "decision")]
        files = {"a.py": file_record(AGENT_A, lines=7, seq=2)}
        before = json.dumps([events, files], sort_keys=True)
        ledger.compute(events, files)
        self.assertEqual(json.dumps([events, files], sort_keys=True), before)

    def test_compute_does_not_mutate_the_default_weights(self):
        snapshot = json.dumps(ledger.DEFAULT_WEIGHTS, sort_keys=True)
        ledger.compute([hello(1, AGENT_A, "A")], {},
                       {"chat_points_cap": 1.0, "contribution_weights": {"decision": 99}})
        self.assertEqual(json.dumps(ledger.DEFAULT_WEIGHTS, sort_keys=True), snapshot)

    def test_computed_at_defaults_to_the_last_events_timestamp(self):
        events = [hello(1, AGENT_A, "Ada"), chat(2, AGENT_A)]
        events[1]["ts"] = "2026-10-09T08:00:00.000Z"
        self.assertEqual(ledger.compute(events, {}).computed_at, "2026-10-09T08:00:00.000Z")

    def test_computed_at_can_be_injected(self):
        result = ledger.compute([], {}, computed_at="2026-01-01T00:00:00.000Z")
        self.assertEqual(result.computed_at, "2026-01-01T00:00:00.000Z")


class TestDecay(unittest.TestCase):
    def test_zero_half_life_means_no_decay(self):
        events = [
            hello(1, AGENT_A, "Ada"),
            contribution(2, AGENT_A, "decision", ts="2020-01-01T00:00:00.000Z"),
        ]
        events[1]["ts"] = "2020-01-01T00:00:00.000Z"
        line = ledger.compute(events, {}, {"decay_half_life_days": 0},
                              now=1791456896.0).lines[0]
        self.assertEqual(line.components["contributions"], float(W["contribution_weights"]["decision"]))

    def test_one_half_life_halves_the_points(self):
        events = [hello(1, AGENT_A, "Ada"), contribution(2, AGENT_A, "decision")]
        events[1]["ts"] = "2026-10-01T12:00:00.000Z"
        now = ledger._to_unix("2026-10-08T12:00:00.000Z")
        line = ledger.compute(events, {}, {"decay_half_life_days": 7}, now=now).lines[0]
        self.assertAlmostEqual(line.components["contributions"],
                               W["contribution_weights"]["decision"] / 2.0, places=6)

    def test_two_half_lives_quarter_the_points(self):
        events = [hello(1, AGENT_A, "Ada"), contribution(2, AGENT_A, "decision")]
        events[1]["ts"] = "2026-09-24T12:00:00.000Z"
        now = ledger._to_unix("2026-10-08T12:00:00.000Z")
        line = ledger.compute(events, {}, {"decay_half_life_days": 7}, now=now).lines[0]
        self.assertAlmostEqual(line.components["contributions"],
                               W["contribution_weights"]["decision"] / 4.0, places=6)

    def test_decay_uses_the_injected_reference_time_not_the_clock(self):
        events = [hello(1, AGENT_A, "Ada"), contribution(2, AGENT_A, "decision")]
        events[1]["ts"] = "2026-10-01T12:00:00.000Z"
        a = ledger.compute(events, {}, {"decay_half_life_days": 7},
                           now=ledger._to_unix("2026-10-08T12:00:00.000Z")).lines[0].total
        b = ledger.compute(events, {}, {"decay_half_life_days": 7},
                           now=ledger._to_unix("2026-10-15T12:00:00.000Z")).lines[0].total
        self.assertAlmostEqual(a, 2 * b, places=6)

    def test_decay_evidence_shows_the_factor_and_the_undecayed_points(self):
        events = [hello(1, AGENT_A, "Ada"), contribution(2, AGENT_A, "decision")]
        events[1]["ts"] = "2026-10-01T12:00:00.000Z"
        now = ledger._to_unix("2026-10-08T12:00:00.000Z")
        entry = ledger.compute(events, {}, {"decay_half_life_days": 7},
                               now=now).lines[0].evidence["contributions"][0]
        self.assertAlmostEqual(entry["decay"], 0.5, places=6)
        self.assertAlmostEqual(entry["base_points"], 8.0, places=4)

    def test_surviving_lines_are_not_decayed(self):
        # Standing code has not become less true with age; it is a measurement of now.
        files = {"a.py": file_record(AGENT_A, lines=100, seq=2)}
        files["a.py"]["ts"] = "2020-01-01T00:00:00.000Z"
        line = ledger.compute([hello(1, AGENT_A, "Ada")], files,
                              {"decay_half_life_days": 7},
                              now=ledger._to_unix("2026-10-08T12:00:00.000Z")).lines[0]
        self.assertAlmostEqual(line.components["authored"], 100 * W["surviving_line_points"])

    def test_an_unparseable_timestamp_simply_does_not_decay(self):
        events = [hello(1, AGENT_A, "Ada"), contribution(2, AGENT_A, "decision")]
        events[1]["ts"] = "not a timestamp"
        line = ledger.compute(events, {}, {"decay_half_life_days": 7}, now=1791456896.0).lines[0]
        self.assertEqual(line.components["contributions"], float(W["contribution_weights"]["decision"]))

    def test_rfc3339_parsing_matches_the_spec_example(self):
        self.assertAlmostEqual(ledger._to_unix("2026-10-08T12:34:56.789Z"), 1791462896.789, places=3)
        self.assertAlmostEqual(ledger._to_unix("2026-10-08T13:34:56.789+01:00"),
                               ledger._to_unix("2026-10-08T12:34:56.789Z"), places=3)
        self.assertEqual(ledger._to_unix("1970-01-01T00:00:00.000Z"), 0.0)
        self.assertIsNone(ledger._to_unix("garbage"))
        self.assertIsNone(ledger._to_unix(None))


class TestEvidenceShape(unittest.TestCase):
    def test_every_component_key_is_present_even_when_empty(self):
        line = ledger.compute([hello(1, AGENT_A, "Ada")], {}).lines[0]
        self.assertEqual(list(line.to_dict()["components"]), list(ledger.COMPONENTS))
        self.assertEqual(list(line.to_dict()["evidence"]), list(ledger.COMPONENTS))

    def test_every_evidence_entry_is_renderable_without_re_derivation(self):
        events = [
            hello(1, AGENT_A, "Ada"), hello(2, AGENT_B, "Bob"),
            contribution(3, AGENT_A, "decision", "pick SSE", event_id="evt_x"),
            chat(4, AGENT_B, "agreed", refs=[{"kind": "event", "value": "evt_x"}]),
            chat(5, AGENT_A, "thanks"),
            ev(seq=6, actor=AGENT_A, etype="task.claim", body={"id": "tsk_1"}),
            ev(seq=7, actor=AGENT_A, etype="task.done", body={"id": "tsk_1"}),
        ]
        files = {"a.py": file_record(AGENT_A, lines=12, seq=8)}
        line = ledger.compute(events, files).line(AGENT_A)
        for component, entries in line.evidence.items():
            for entry in entries:
                self.assertIsInstance(entry["seq"], int, component)
                self.assertIsInstance(entry["label"], str, component)
                self.assertTrue(entry["label"], "evidence must carry a human label")
                self.assertIsInstance(entry["points"], float, component)

    def test_the_whole_result_is_json_serialisable(self):
        events = [hello(1, AGENT_A, "Ada"), contribution(2, AGENT_A, "decision"),
                  chat(3, AGENT_A, "héllo ☃")]
        payload = json.dumps(ledger.compute(events, {"a.py": file_record(AGENT_A)}).to_dict())
        self.assertIn("evidence", payload)

    def test_evidence_is_ordered_by_seq(self):
        events = [hello(1, AGENT_A, "Ada")] + [
            contribution(s, AGENT_A, "answer") for s in (9, 3, 7, 5)
        ]
        entries = ledger.compute(events, {}).lines[0].evidence["contributions"]
        self.assertEqual([e["seq"] for e in entries], [3, 5, 7, 9])

    def test_max_evidence_truncates_with_an_honest_summary(self):
        events = [hello(1, AGENT_A, "Ada")] + [
            contribution(2 + i, AGENT_A, "answer") for i in range(50)
        ]
        line = ledger.compute(events, {}, max_evidence=10).lines[0]
        entries = line.evidence["contributions"]
        self.assertEqual(len(entries), 11)
        self.assertTrue(entries[-1]["truncated"])
        self.assertAlmostEqual(sum(e["points"] for e in entries), line.components["contributions"])

    def test_max_evidence_zero_keeps_everything(self):
        events = [hello(1, AGENT_A, "Ada")] + [
            contribution(2 + i, AGENT_A, "answer") for i in range(50)
        ]
        self.assertEqual(len(ledger.compute(events, {}).lines[0].evidence["contributions"]), 50)

    def test_the_result_dict_carries_the_honest_caveat(self):
        payload = ledger.compute([], {}).to_dict()
        self.assertIn("caveat", payload)
        self.assertIn("not quality", payload["caveat"].lower())


class TestWhy(unittest.TestCase):
    def test_why_names_the_agent_the_total_and_every_component(self):
        events = [hello(1, AGENT_A, "Ada"), contribution(2, AGENT_A, "decision", "pick SSE")]
        text = ledger.compute(events, {}).why(AGENT_A)
        self.assertIn("Ada", text)
        self.assertIn(AGENT_A, text)
        for component in ledger.COMPONENTS:
            self.assertIn(component, text)
        self.assertIn("pick SSE", text)
        self.assertIn("seq 2", text)

    def test_why_states_the_caveat(self):
        text = ledger.compute([hello(1, AGENT_A, "Ada")], {}).why(AGENT_A)
        self.assertIn("not quality", text.lower())

    def test_why_for_an_unknown_agent_is_a_sentence_not_an_exception(self):
        self.assertIn("No ledger entry", ledger.compute([], {}).why("agt_nope"))


class TestWeights(unittest.TestCase):
    def test_the_published_defaults_match_the_spec(self):
        self.assertEqual(
            ledger.DEFAULT_WEIGHTS["contribution_weights"],
            {"decision": 8, "design": 6, "finding": 5, "fix": 3,
             "review": 3, "doc": 2, "code": 2, "answer": 1},
        )
        self.assertEqual(ledger.DEFAULT_WEIGHTS["surviving_line_points"], 0.02)
        self.assertEqual(ledger.DEFAULT_WEIGHTS["surviving_lines_cap_per_file"], 400)
        self.assertEqual(ledger.DEFAULT_WEIGHTS["task_done_points"], 2.0)
        self.assertEqual(ledger.DEFAULT_WEIGHTS["citation_received_points"], 0.5)
        self.assertEqual(ledger.DEFAULT_WEIGHTS["chat_message_points"], 0.05)
        self.assertEqual(ledger.DEFAULT_WEIGHTS["chat_points_cap"], 10.0)
        self.assertEqual(ledger.DEFAULT_WEIGHTS["decay_half_life_days"], 0)

    def test_contribution_weights_merge_key_by_key(self):
        merged = ledger.merge_weights({"contribution_weights": {"decision": 99}})
        self.assertEqual(merged["contribution_weights"]["decision"], 99)
        self.assertEqual(merged["contribution_weights"]["answer"], 1)

    def test_an_override_changes_the_score(self):
        events = [hello(1, AGENT_A, "Ada"), contribution(2, AGENT_A, "answer")]
        line = ledger.compute(events, {},
                              {"contribution_weights": {"answer": 50}}).lines[0]
        self.assertEqual(line.components["contributions"], 50.0)

    def test_load_weights_reads_the_workspace_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".parley").mkdir()
            (workspace / ".parley" / "ledger.json").write_text(
                json.dumps({"chat_points_cap": 1.5,
                            "contribution_weights": {"decision": 20}}),
                encoding="utf-8",
            )
            weights = ledger.load_weights(workspace)
        self.assertEqual(weights["chat_points_cap"], 1.5)
        self.assertEqual(weights["contribution_weights"]["decision"], 20)
        self.assertEqual(weights["contribution_weights"]["answer"], 1)

    def test_load_weights_falls_back_to_the_defaults_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ledger.load_weights(Path(tmp)), ledger.DEFAULT_WEIGHTS)

    def test_load_weights_survives_a_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".parley").mkdir()
            (workspace / ".parley" / "ledger.json").write_bytes(b"{not json")
            with self.assertLogs("parley.ledger", level="WARNING"):
                self.assertEqual(ledger.load_weights(workspace), ledger.DEFAULT_WEIGHTS)

    def test_load_weights_survives_a_non_object_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".parley").mkdir()
            (workspace / ".parley" / "ledger.json").write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertLogs("parley.ledger", level="WARNING"):
                self.assertEqual(ledger.load_weights(workspace), ledger.DEFAULT_WEIGHTS)

    def test_the_weights_used_are_reported_back_in_the_result(self):
        result = ledger.compute([], {}, {"chat_points_cap": 3.0})
        self.assertEqual(result.weights["chat_points_cap"], 3.0)
        self.assertEqual(result.to_dict()["weights"]["chat_points_cap"], 3.0)


class TestGamingResistanceSummary(unittest.TestCase):
    """One test per documented defence, so a regression cannot quietly re-open a hole."""

    def test_the_full_gaming_playbook_does_not_beat_honest_work(self):
        cheat = [hello(1, AGENT_A, "Cheater")]
        seq = 10
        for i in range(500):  # chat spam
            cheat.append(chat(seq, AGENT_A))
            seq += 1
        for i in range(200):  # self-citation
            cheat.append(chat(seq, AGENT_A, refs=[{"kind": "event", "value": "evt_self"}]))
            seq += 1
        cheat.append(contribution(seq, AGENT_A, "decision", "x", event_id="evt_self"))
        seq += 1
        for i in range(50):  # restating the same contribution under supersedes
            cheat.append(contribution(seq, AGENT_A, "decision", "x",
                                      event_id="evt_s{0}".format(i),
                                      supersedes="evt_self"))
            seq += 1
        for i in range(50):  # closing tasks nobody gave it
            cheat.append(ev(seq=seq, actor=AGENT_A, etype="task.done",
                            body={"id": "tsk_{0}".format(i)}))
            seq += 1

        honest = [hello(2, AGENT_B, "Honest")]
        seq = 5000
        for kind in ("decision", "design", "finding", "fix", "review"):
            honest.append(contribution(seq, AGENT_B, kind))
            seq += 1
        honest.append(ev(seq=seq, actor=AGENT_B, etype="task.claim", body={"id": "tsk_real"}))
        seq += 1
        honest.append(ev(seq=seq, actor=AGENT_B, etype="task.done", body={"id": "tsk_real"}))

        files = {"real.py": file_record(AGENT_B, lines=300, seq=5100)}
        result = ledger.compute(cheat + honest, files)
        cheater = result.line(AGENT_A)
        worker = result.line(AGENT_B)
        self.assertEqual(cheater.components["influence"], 0.0, "self-citation must pay nothing")
        self.assertEqual(cheater.components["delivery"], 0.0, "unclaimed tasks must pay nothing")
        self.assertLessEqual(cheater.components["presence"], W["chat_points_cap"])
        self.assertGreater(worker.total, cheater.total,
                           "honest work must outscore the whole gaming playbook")


if __name__ == "__main__":
    unittest.main()
