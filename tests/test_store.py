"""SPEC §2, §3.3, §5.2 and §7 as the Hub's store sees them.

The store is the one ordering authority in a parley (SPEC §1.2: "the Hub assigns ``seq``;
``seq`` is the only ordering authority"), and it is hit from every request thread at once.
So the headline test here spawns real threads and proves the invariant directly rather than
reasoning about the locking.
"""

from __future__ import annotations

import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path

from parley import errors, protocol
from parley.hub.store import Store
from tests.helpers import AGENT_A, AGENT_B, SESSION


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = Store(self.state_dir)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.store.close)

    def event(self, actor=AGENT_A, etype="chat.message", body=None, event_id=None):
        return protocol.make_event(actor, SESSION, etype,
                                   {"text": "hi"} if body is None else body,
                                   event_id=event_id)


class TestSeqIsTheOrderingAuthority(StoreTestCase):
    def test_seq_starts_at_one_and_increases_by_one(self):
        for expected in range(1, 11):
            self.assertEqual(self.store.append(self.event())["seq"], expected)
        self.assertEqual(self.store.head_seq(), 10)

    def test_head_seq_of_an_empty_log_is_zero(self):
        self.assertEqual(self.store.head_seq(), 0)

    def test_a_client_supplied_seq_is_overwritten_not_trusted(self):
        """SPEC §2: clients MUST NOT set seq."""
        event = self.event()
        event["seq"] = 9999
        self.assertEqual(self.store.append(event)["seq"], 1)
        self.assertEqual(self.store.head_seq(), 1)

    def test_append_many_assigns_a_contiguous_block(self):
        stored = self.store.append_many([self.event() for _ in range(5)])
        self.assertEqual([e["seq"] for e in stored], [1, 2, 3, 4, 5])

    def test_seq_survives_a_close_and_reopen(self):
        self.store.append(self.event())
        self.store.append(self.event())
        self.store.close()
        reopened = Store(self.state_dir)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.head_seq(), 2)
        self.assertEqual(reopened.append(self.event())["seq"], 3)


class TestConcurrentAppends(StoreTestCase):
    """The invariant under load: no gaps, no duplicates, no reordering."""

    THREADS = 16
    PER_THREAD = 50

    def test_many_threads_appending_produce_a_dense_gapless_sequence(self):
        total = self.THREADS * self.PER_THREAD
        results = {}
        errors_seen = []
        barrier = threading.Barrier(self.THREADS)

        def worker(index):
            mine = []
            try:
                barrier.wait(timeout=20)
                for n in range(self.PER_THREAD):
                    event = self.event(
                        body={"text": "t{0}-{1}".format(index, n)},
                        event_id="evt_{0:08x}{1:08x}".format(index, n),
                    )
                    mine.append(self.store.append(event)["seq"])
            except Exception as exc:  # recorded, then asserted on the main thread
                errors_seen.append((index, type(exc).__name__, str(exc)))
            results[index] = mine

        threads = [threading.Thread(target=worker, args=(i,), name="appender-{0}".format(i))
                   for i in range(self.THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        for thread in threads:
            self.assertFalse(thread.is_alive(), "{0} never finished".format(thread.name))

        self.assertEqual(errors_seen, [], "concurrent appends must not raise")

        assigned = [seq for seqs in results.values() for seq in seqs]
        self.assertEqual(len(assigned), total)
        self.assertEqual(len(set(assigned)), total, "a seq was handed out twice")
        self.assertEqual(sorted(assigned), list(range(1, total + 1)), "the sequence has a gap")
        self.assertEqual(self.store.head_seq(), total)

        for index, seqs in results.items():
            self.assertEqual(seqs, sorted(seqs),
                             "thread {0} saw its own appends reordered".format(index))

    def test_the_log_reads_back_in_seq_order_with_no_gaps(self):
        self.test_many_threads_appending_produce_a_dense_gapless_sequence()
        total = self.THREADS * self.PER_THREAD
        read = self.store.read(since=0, limit=total + 10)
        self.assertEqual([e["seq"] for e in read], list(range(1, total + 1)))

    def test_concurrent_readers_never_see_a_partial_log(self):
        stop = threading.Event()
        anomalies = []

        def reader():
            while not stop.is_set():
                seqs = [e["seq"] for e in self.store.read(since=0, limit=10000)]
                if seqs != sorted(seqs) or len(seqs) != len(set(seqs)):
                    anomalies.append(seqs[:20])

        thread = threading.Thread(target=reader, name="log-reader", daemon=True)
        thread.start()
        try:
            for _ in range(200):
                self.store.append(self.event())
        finally:
            stop.set()
            thread.join(timeout=20)
        self.assertFalse(thread.is_alive(), "the reader thread never stopped")
        self.assertEqual(anomalies, [], "a reader observed an out-of-order or duplicated log")


class TestDeduplicationLookup(StoreTestCase):
    """SPEC §5.2's lookup half.

    The store provides ``find_by_author_id``; the Hub's ingest path is what refuses the
    duplicate (see ``test_hub_api`` and ``test_conformance``). The contract tested here is
    that the lookup is exact, actor-scoped, and good enough to build the dedup on.
    """

    def test_find_by_author_id_returns_the_stored_event(self):
        stored = self.store.append(self.event(event_id="evt_0123456789abcdef"))
        found = self.store.find_by_author_id(AGENT_A, "evt_0123456789abcdef")
        self.assertIsNotNone(found)
        self.assertEqual(found["seq"], stored["seq"])

    def test_find_by_author_id_is_scoped_to_the_actor(self):
        self.store.append(self.event(actor=AGENT_A, event_id="evt_0123456789abcdef"))
        self.assertIsNone(self.store.find_by_author_id(AGENT_B, "evt_0123456789abcdef"))

    def test_find_by_author_id_returns_none_for_an_unknown_id(self):
        self.assertIsNone(self.store.find_by_author_id(AGENT_A, "evt_ffffffffffffffff"))

    def test_the_lookup_finds_the_original_after_later_appends(self):
        first = self.store.append(self.event(event_id="evt_0123456789abcdef"))
        for _ in range(20):
            self.store.append(self.event())
        found = self.store.find_by_author_id(AGENT_A, "evt_0123456789abcdef")
        self.assertEqual(found["seq"], first["seq"],
                         "SPEC §5.2: the original seq must stay retrievable")

    def test_the_same_id_from_a_different_actor_is_a_different_event(self):
        self.store.append(self.event(actor=AGENT_A, event_id="evt_0123456789abcdef"))
        stored = self.store.append(self.event(actor=AGENT_B, event_id="evt_0123456789abcdef"))
        self.assertEqual(stored["seq"], 2)
        self.assertEqual(self.store.head_seq(), 2)
        self.assertEqual(
            self.store.find_by_author_id(AGENT_A, "evt_0123456789abcdef")["seq"], 1
        )
        self.assertEqual(
            self.store.find_by_author_id(AGENT_B, "evt_0123456789abcdef")["seq"], 2
        )

    def test_the_lookup_is_usable_from_many_threads_at_once(self):
        self.store.append(self.event(event_id="evt_cafebabecafebabe"))
        barrier = threading.Barrier(12)
        seen = []

        def worker():
            barrier.wait(timeout=20)
            found = self.store.find_by_author_id(AGENT_A, "evt_cafebabecafebabe")
            seen.append(None if found is None else found["seq"])

        threads = [threading.Thread(target=worker, name="dedup-reader", daemon=True)
                   for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(set(seen), {1},
                         "every concurrent reader must see the same original seq")


class TestReading(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.store.append(self.event(etype="chat.message"))
        self.store.append(self.event(etype="file.put",
                                     body={"path": "a.py", "hash": "sha256:" + "ab" * 32,
                                           "size": 1}))
        self.store.append(self.event(etype="chat.reaction",
                                     body={"target": "evt_0123456789abcdef", "reaction": "+1"}))
        self.store.append(self.event(etype="status.update",
                                     body={"state": "working", "headline": "x"}))

    def test_since_is_exclusive(self):
        self.assertEqual([e["seq"] for e in self.store.read(since=2)], [3, 4])
        self.assertEqual([e["seq"] for e in self.store.read(since=0)], [1, 2, 3, 4])
        self.assertEqual(self.store.read(since=4), [])

    def test_limit_is_honoured(self):
        self.assertEqual([e["seq"] for e in self.store.read(since=0, limit=2)], [1, 2])

    def test_the_type_filter_matches_by_prefix(self):
        """SPEC §5: ``types`` is a comma-separated prefix filter."""
        self.assertEqual([e["seq"] for e in self.store.read(types=["chat"])], [1, 3])
        self.assertEqual([e["seq"] for e in self.store.read(types=["chat."])], [1, 3])
        self.assertEqual([e["seq"] for e in self.store.read(types=["file"])], [2])
        self.assertEqual([e["seq"] for e in self.store.read(types=["chat", "file"])], [1, 2, 3])
        self.assertEqual(self.store.read(types=["nothing"]), [])

    def test_a_read_returns_whole_events_including_the_body(self):
        event = self.store.read(since=1, limit=1)[0]
        self.assertEqual(event["type"], "file.put")
        self.assertEqual(event["body"]["path"], "a.py")
        self.assertEqual(event["v"], "PARLEY/1")

    def test_unknown_fields_survive_a_store_round_trip(self):
        """SPEC §2.1 forward compatibility, all the way through persistence."""
        event = self.event(etype="x.vendor.thing", body={"deep": {"list": [1, 2]},
                                                         "x_future": True})
        event["x_top_level"] = "kept"
        stored_seq = self.store.append(event)["seq"]
        read_back = [e for e in self.store.read(since=stored_seq - 1, limit=1)][0]
        self.assertEqual(read_back["body"]["x_future"], True)
        self.assertEqual(read_back["body"]["deep"], {"list": [1, 2]})
        self.assertEqual(read_back.get("x_top_level"), "kept")


class TestBlobs(StoreTestCase):
    def test_a_blob_round_trips_byte_for_byte(self):
        data = bytes(range(256)) * 400
        blob_hash = "sha256:" + hashlib.sha256(data).hexdigest()
        self.assertFalse(self.store.has_blob(blob_hash))
        self.assertEqual(self.store.put_blob(blob_hash, data), len(data))
        self.assertTrue(self.store.has_blob(blob_hash))
        self.assertEqual(self.store.blob_size(blob_hash), len(data))
        with self.store.open_blob(blob_hash) as handle:
            self.assertEqual(handle.read(), data)

    def test_an_empty_blob_is_storable(self):
        blob_hash = "sha256:" + hashlib.sha256(b"").hexdigest()
        self.store.put_blob(blob_hash, b"")
        self.assertTrue(self.store.has_blob(blob_hash))
        self.assertEqual(self.store.blob_size(blob_hash), 0)

    def test_a_body_that_does_not_match_its_hash_is_refused(self):
        """Accepting it would let a peer poison the content-addressed store."""
        claimed = "sha256:" + hashlib.sha256(b"honest").hexdigest()
        with self.assertRaises((errors.ParleyError, ValueError)):
            self.store.put_blob(claimed, b"tampered")
        self.assertFalse(self.store.has_blob(claimed),
                         "a rejected blob must leave nothing behind")

    def test_a_malformed_hash_is_refused(self):
        for bad in ("", "deadbeef", "sha256:short", "md5:" + "0" * 32,
                    "sha256:" + "Z" * 64, "sha256:" + "0" * 63):
            with self.subTest(blob_hash=bad):
                with self.assertRaises((errors.ParleyError, ValueError)):
                    self.store.put_blob(bad, b"x")

    def test_storing_the_same_blob_twice_is_idempotent(self):
        data = b"identical"
        blob_hash = "sha256:" + hashlib.sha256(data).hexdigest()
        self.store.put_blob(blob_hash, data)
        self.store.put_blob(blob_hash, data)
        self.assertEqual(self.store.blob_size(blob_hash), len(data))

    def test_an_absent_blob_reports_absent_rather_than_guessing(self):
        missing = "sha256:" + "0" * 64
        self.assertFalse(self.store.has_blob(missing))
        self.assertIsNone(self.store.blob_size(missing))
        with self.assertRaises((errors.NoSuchBlob, FileNotFoundError, OSError)):
            self.store.open_blob(missing)

    def test_blobs_are_sharded_so_one_directory_does_not_hold_everything(self):
        data = b"shard me"
        blob_hash = "sha256:" + hashlib.sha256(data).hexdigest()
        self.store.put_blob(blob_hash, data)
        hex_digest = blob_hash.split(":", 1)[1]
        expected = self.state_dir / "blobs" / hex_digest[:2] / hex_digest
        self.assertTrue(expected.exists(), "SPEC: blobs at <state_dir>/blobs/<first2>/<sha256>")


class TestNonceReplay(StoreTestCase):
    """SPEC §3.3: reject an ``(agent_id, nonce)`` pair seen in the last 600 s.

    ``ts`` is the request's own timestamp (SPEC §3.3 sends integer Unix seconds), so these
    tests pass realistic values derived from one captured ``NOW`` rather than sleeping.
    """

    TTL = 600.0

    def setUp(self):
        super().setUp()
        self.now = time.time()

    def test_a_fresh_nonce_is_not_a_replay(self):
        self.assertFalse(self.store.seen_nonce(AGENT_A, "3f8a1c04bb9e7d62",
                                               self.now, self.TTL))

    def test_the_same_nonce_again_is_a_replay(self):
        self.store.seen_nonce(AGENT_A, "3f8a1c04bb9e7d62", self.now, self.TTL)
        self.assertTrue(self.store.seen_nonce(AGENT_A, "3f8a1c04bb9e7d62",
                                              self.now, self.TTL))

    def test_a_replay_within_the_window_is_still_a_replay(self):
        self.store.seen_nonce(AGENT_A, "nonce0001", self.now, self.TTL)
        self.assertTrue(self.store.seen_nonce(AGENT_A, "nonce0001",
                                              self.now + 299.0, self.TTL))

    def test_a_nonce_recorded_before_the_window_is_fresh_again(self):
        """Expiry without sleeping: the stored entry is dated outside the TTL."""
        self.store.seen_nonce(AGENT_A, "nonce0001", self.now - self.TTL - 60.0, self.TTL)
        self.assertFalse(self.store.seen_nonce(AGENT_A, "nonce0001", self.now, self.TTL))

    def test_nonces_are_scoped_per_agent(self):
        self.store.seen_nonce(AGENT_A, "shared-nonce", self.now, self.TTL)
        self.assertFalse(self.store.seen_nonce(AGENT_B, "shared-nonce", self.now, self.TTL))

    def test_many_threads_replaying_one_nonce_see_exactly_one_acceptance(self):
        barrier = threading.Barrier(12)
        outcomes = []

        def worker():
            barrier.wait(timeout=20)
            outcomes.append(self.store.seen_nonce(AGENT_A, "race-nonce", self.now, self.TTL))

        threads = [threading.Thread(target=worker, name="nonce-racer", daemon=True)
                   for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(outcomes.count(False), 1,
                         "exactly one caller may be told the nonce was fresh")


class TestViewerTokens(StoreTestCase):
    """SPEC §3.7."""

    def test_a_live_token_checks_out(self):
        import time

        self.store.put_viewer_token("vwr_" + "a" * 32, time.time() + 3600, "deck")
        self.assertTrue(self.store.check_viewer_token("vwr_" + "a" * 32))

    def test_an_expired_token_does_not(self):
        import time

        self.store.put_viewer_token("vwr_" + "b" * 32, time.time() - 1, "stale")
        self.assertFalse(self.store.check_viewer_token("vwr_" + "b" * 32))

    def test_an_unknown_token_does_not(self):
        self.assertFalse(self.store.check_viewer_token("vwr_" + "c" * 32))
        self.assertFalse(self.store.check_viewer_token(""))

    def test_revocation_takes_effect_immediately(self):
        import time

        token = "vwr_" + "d" * 32
        self.store.put_viewer_token(token, time.time() + 3600)
        self.store.revoke_viewer_token(token)
        self.assertFalse(self.store.check_viewer_token(token))


class TestAgents(StoreTestCase):
    def record(self, agent_id=AGENT_A, status="active"):
        return {
            "agent_id": agent_id, "name": "Ada", "kind": "claude-code",
            "model": "claude-opus-5", "os": "linux", "host": "workbench",
            "key_hex": "11" * 32, "status": status,
            "created": "2026-10-08T12:00:00.000Z", "last_seen": 1791460800.0,
        }

    def test_an_agent_round_trips(self):
        self.store.put_agent(self.record())
        found = self.store.get_agent(AGENT_A)
        self.assertEqual(found["name"], "Ada")
        self.assertEqual(found["key_hex"], "11" * 32)
        self.assertEqual(found["status"], "active")

    def test_an_unknown_agent_is_none_not_an_exception(self):
        self.assertIsNone(self.store.get_agent("agt_ffffffffffffffff"))

    def test_listing_returns_every_agent(self):
        self.store.put_agent(self.record(AGENT_A))
        self.store.put_agent(self.record(AGENT_B, status="pending"))
        self.assertEqual({a["agent_id"] for a in self.store.list_agents()}, {AGENT_A, AGENT_B})

    def test_status_transitions_persist(self):
        self.store.put_agent(self.record(status="pending"))
        self.store.set_agent_status(AGENT_A, "active")
        self.assertEqual(self.store.get_agent(AGENT_A)["status"], "active")
        self.store.set_agent_status(AGENT_A, "revoked")
        self.assertEqual(self.store.get_agent(AGENT_A)["status"], "revoked")

    def test_touching_an_agent_records_when_it_was_last_seen(self):
        self.store.put_agent(self.record())
        self.store.touch_agent(AGENT_A, 1791470000.0)
        self.assertAlmostEqual(self.store.get_agent(AGENT_A)["last_seen"], 1791470000.0,
                               places=3)

    def test_putting_an_agent_twice_updates_rather_than_duplicates(self):
        self.store.put_agent(self.record())
        updated = self.record()
        updated["name"] = "Ada II"
        self.store.put_agent(updated)
        self.assertEqual(len(self.store.list_agents()), 1)
        self.assertEqual(self.store.get_agent(AGENT_A)["name"], "Ada II")


class TestFileIndex(StoreTestCase):
    def record(self, path="a.py", author=AGENT_A, seq=1):
        return {"hash": "sha256:" + "ab" * 32, "size": 12, "mode": 0o644,
                "author": author, "seq": seq, "ts": "2026-10-08T12:00:00.000Z"}

    def test_a_file_round_trips(self):
        self.store.put_file("a.py", self.record())
        found = self.store.get_file("a.py")
        self.assertEqual(found["hash"], "sha256:" + "ab" * 32)
        self.assertEqual(found["author"], AGENT_A)

    def test_an_unknown_path_is_none(self):
        self.assertIsNone(self.store.get_file("nope.py"))

    def test_putting_a_path_twice_replaces_the_current_version(self):
        self.store.put_file("a.py", self.record(seq=1))
        second = self.record(author=AGENT_B, seq=2)
        second["hash"] = "sha256:" + "cd" * 32
        self.store.put_file("a.py", second)
        self.assertEqual(self.store.get_file("a.py")["author"], AGENT_B)
        self.assertEqual(len(self.store.list_files()), 1)

    def test_listing_is_keyed_by_path(self):
        self.store.put_file("a.py", self.record("a.py"))
        self.store.put_file("sub/b.py", self.record("sub/b.py"))
        listing = self.store.list_files()
        self.assertEqual(set(listing), {"a.py", "sub/b.py"})

    def test_deleting_removes_the_entry(self):
        self.store.put_file("a.py", self.record())
        self.store.delete_file("a.py")
        self.assertIsNone(self.store.get_file("a.py"))
        self.assertEqual(self.store.list_files(), {})

    def test_deleting_an_absent_path_is_not_an_error(self):
        self.store.delete_file("never-existed.py")

    def test_a_path_with_unicode_round_trips(self):
        self.store.put_file("café/naïve.txt", self.record())
        self.assertIn("café/naïve.txt", self.store.list_files())


if __name__ == "__main__":
    unittest.main()
