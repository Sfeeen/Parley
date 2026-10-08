"""SPEC §7 — workspace synchronisation.

Two halves:

* **§7.2 ignore rules**, which decide what is even eligible to sync.
* **§7.6/§7.7 conflict arbitration**, which is where design rule R5 — *never lose a byte* —
  is either honoured or quietly broken. Every case in the matrix asserts the same thing:
  after arbitration, **both** versions still exist somewhere. That is the property, and a
  conflict handler that merges, drops or overwrites fails it.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from parley import protocol
from parley.client.ignore import IgnoreRules
from parley.hub.server import create_parley
from tests.helpers import AGENT_A, AGENT_B


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class IgnoreTestCase(unittest.TestCase):
    def rules(self, *lines):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace = Path(tmp.name)
        if lines:
            (workspace / ".parleyignore").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return IgnoreRules.load(workspace)


class TestBuiltInIgnores(IgnoreTestCase):
    """SPEC §7.2 — always ignored, with or without a .parleyignore."""

    def test_the_documented_directories_are_always_ignored(self):
        rules = self.rules()
        for directory in (".parley", ".git", ".hg", ".svn", "__pycache__",
                          "node_modules", ".venv", "venv"):
            with self.subTest(directory=directory):
                self.assertTrue(rules.ignored(directory, is_dir=True))
                self.assertTrue(rules.ignored(directory + "/inside.txt"))
                self.assertTrue(rules.ignored("deep/nested/" + directory + "/inside.txt"))

    def test_the_documented_file_patterns_are_always_ignored(self):
        rules = self.rules()
        for path in ("a.pyc", "pkg/mod.pyc", ".DS_Store", "sub/.DS_Store", "Thumbs.db",
                     "file.swp", "notes.txt~", ".#emacs-lock", "sub/.#emacs-lock"):
            with self.subTest(path=path):
                self.assertTrue(rules.ignored(path), path)

    def test_ordinary_project_files_are_not_ignored(self):
        rules = self.rules()
        for path in ("a.py", "parley/hub/server.py", "docs/SPEC.md", "README.md",
                     "venvironment.txt", "my_node_modules.txt", "gitignore.md",
                     "src/pyc.txt", "data.db"):
            with self.subTest(path=path):
                self.assertFalse(rules.ignored(path), path)

    def test_a_credentials_file_can_never_leak_through_sync(self):
        """``.parley/credentials.json`` holds the agent key; syncing it would be fatal."""
        rules = self.rules("!.parley/", "!*", "!**")
        self.assertTrue(rules.ignored(".parley/credentials.json"),
                        "no .parleyignore rule may re-include the credential store")


class TestParleyIgnoreSyntax(IgnoreTestCase):
    """SPEC §7.2 — the gitignore subset: # comments, ! negation, / anchoring, * ** ?,
    trailing / for directory-only."""

    def test_comments_and_blank_lines_are_ignored(self):
        rules = self.rules("# a comment", "", "   ", "*.log", "# *.py")
        self.assertTrue(rules.ignored("a.log"))
        self.assertFalse(rules.ignored("a.py"))

    def test_a_plain_pattern_matches_at_any_depth(self):
        rules = self.rules("*.log")
        self.assertTrue(rules.ignored("a.log"))
        self.assertTrue(rules.ignored("deep/nested/a.log"))

    def test_a_leading_slash_anchors_to_the_workspace_root(self):
        rules = self.rules("/build")
        self.assertTrue(rules.ignored("build"))
        self.assertTrue(rules.ignored("build/out.o"))
        self.assertFalse(rules.ignored("sub/build"))
        self.assertFalse(rules.ignored("sub/build/out.o"))

    def test_an_embedded_slash_also_anchors(self):
        rules = self.rules("doc/draft.md")
        self.assertTrue(rules.ignored("doc/draft.md"))
        self.assertFalse(rules.ignored("sub/doc/draft.md"))

    def test_a_trailing_slash_means_directories_only(self):
        rules = self.rules("build/")
        self.assertTrue(rules.ignored("build", is_dir=True))
        self.assertTrue(rules.ignored("build/out.o"))
        self.assertFalse(rules.ignored("build", is_dir=False))
        self.assertFalse(rules.ignored("buildfile.txt"))

    def test_a_single_star_does_not_cross_a_separator(self):
        rules = self.rules("a/*/c")
        self.assertTrue(rules.ignored("a/b/c"))
        self.assertFalse(rules.ignored("a/b/x/c"))

    def test_a_double_star_crosses_separators(self):
        rules = self.rules("doc/**/draft.md")
        self.assertTrue(rules.ignored("doc/draft.md"))
        self.assertTrue(rules.ignored("doc/a/draft.md"))
        self.assertTrue(rules.ignored("doc/a/b/c/draft.md"))
        self.assertFalse(rules.ignored("other/a/draft.md"))

    def test_a_leading_double_star_matches_at_any_depth(self):
        rules = self.rules("**/generated")
        self.assertTrue(rules.ignored("generated", is_dir=True))
        self.assertTrue(rules.ignored("a/b/generated", is_dir=True))

    def test_a_trailing_double_star_matches_everything_beneath(self):
        rules = self.rules("vendor/**")
        self.assertTrue(rules.ignored("vendor/x.py"))
        self.assertTrue(rules.ignored("vendor/a/b/c.py"))

    def test_a_question_mark_matches_exactly_one_character(self):
        rules = self.rules("log?.txt")
        self.assertTrue(rules.ignored("log1.txt"))
        self.assertFalse(rules.ignored("log.txt"))
        self.assertFalse(rules.ignored("log12.txt"))

    def test_negation_re_includes_a_file(self):
        rules = self.rules("*.log", "!keep.log")
        self.assertTrue(rules.ignored("debug.log"))
        self.assertFalse(rules.ignored("keep.log"))

    def test_the_last_matching_rule_wins(self):
        rules = self.rules("!keep.log", "*.log")
        self.assertTrue(rules.ignored("keep.log"),
                        "gitignore semantics: a later rule overrides an earlier one")

    def test_negation_and_re_exclusion_can_be_stacked(self):
        rules = self.rules("*.log", "!important/*.log", "important/secret.log")
        self.assertTrue(rules.ignored("a.log"))
        self.assertFalse(rules.ignored("important/a.log"))
        self.assertTrue(rules.ignored("important/secret.log"))

    def test_a_malformed_line_does_not_take_the_whole_file_down(self):
        rules = self.rules("[", "*.log", "a[b")
        self.assertTrue(rules.ignored("x.log"))

    def test_an_absent_parleyignore_leaves_only_the_built_in_rules(self):
        rules = self.rules()
        self.assertFalse(rules.ignored("anything.txt"))
        self.assertTrue(rules.ignored(".git/config"))


class HubArbiterTestCase(unittest.TestCase):
    """Drives the Hub's in-process append path, which is where §7.6 is decided."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.hub, self.watchword = create_parley(self.workspace, name="conflict-test", port=0)
        self.addCleanup(self.hub.stop)
        for agent_id, name in ((AGENT_A, "Ada"), (AGENT_B, "Bob")):
            self.hub.store.put_agent({
                "agent_id": agent_id, "name": name, "kind": "test", "model": "",
                "os": "linux", "host": "t", "key_hex": "11" * 32, "status": "active",
                "created": "2026-10-08T12:00:00.000Z", "last_seen": 0.0,
            })

    def put(self, actor, path, data, base=None):
        blob_hash = sha(data)
        self.hub.store.put_blob(blob_hash, data)
        body = {"path": path, "hash": blob_hash, "size": len(data)}
        if base is not None:
            body["base"] = base
        self.hub.submit(protocol.make_event(actor, self.hub.config.session, "file.put", body))
        return blob_hash

    def delete(self, actor, path, base=None):
        body = {"path": path}
        if base is not None:
            body["base"] = base
        self.hub.submit(protocol.make_event(actor, self.hub.config.session, "file.delete", body))

    def log(self, etype=None):
        events = self.hub.store.read(since=0, limit=10000)
        return [e for e in events if etype is None or e["type"] == etype]

    def index(self):
        return self.hub.store.list_files()

    def assert_both_versions_survive(self, path, ours_hash, theirs_hash):
        index = self.index()
        present = {record["hash"] for record in index.values()}
        self.assertIn(theirs_hash, present,
                      "the newly-arrived version must be current (SPEC §7.6)")
        self.assertIn(ours_hash, present,
                      "R5: the displaced version must be preserved, not dropped")
        self.assertEqual(index[path]["hash"], theirs_hash,
                         "last-writer-wins at the original path")
        sidecars = [p for p in index if p != path and p.startswith(path)]
        self.assertTrue(sidecars, "the displaced version needs a sidecar path")
        self.assertIn(".parley-conflict-", sidecars[0])
        return sidecars[0]


class TestConflictMatrix(HubArbiterTestCase):
    """SPEC §7.6, every row."""

    def test_a_matching_base_is_accepted_without_a_conflict(self):
        first = self.put(AGENT_A, "a.py", b"one\n")
        self.put(AGENT_B, "a.py", b"two\n", base=first)
        self.assertEqual(self.index()["a.py"]["hash"], sha(b"two\n"))
        self.assertEqual(self.log("file.conflict"), [])
        self.assertEqual(len(self.index()), 1)

    def test_an_absent_base_on_an_unknown_path_is_accepted(self):
        self.put(AGENT_A, "new.py", b"hello\n")
        self.assertEqual(self.index()["new.py"]["hash"], sha(b"hello\n"))
        self.assertEqual(self.log("file.conflict"), [])

    def test_an_absent_base_on_a_known_path_diverges_and_keeps_both(self):
        ours = self.put(AGENT_A, "a.py", b"ada\n")
        theirs = self.put(AGENT_B, "a.py", b"bob\n")
        self.assert_both_versions_survive("a.py", ours, theirs)
        self.assertEqual(len(self.log("file.conflict")), 1)

    def test_a_divergent_base_keeps_both(self):
        ours = self.put(AGENT_A, "a.py", b"ada\n")
        theirs = self.put(AGENT_B, "a.py", b"bob\n", base=sha(b"something else\n"))
        self.assert_both_versions_survive("a.py", ours, theirs)

    def test_a_simultaneous_edit_from_a_common_ancestor_keeps_both(self):
        base = self.put(AGENT_A, "a.py", b"base\n")
        ada = self.put(AGENT_A, "a.py", b"ada edit\n", base=base)
        bob = self.put(AGENT_B, "a.py", b"bob edit\n", base=base)
        sidecar = self.assert_both_versions_survive("a.py", ada, bob)
        self.assertEqual(self.index()[sidecar]["hash"], ada)

    def test_the_conflict_event_names_both_sides_and_where_the_loser_went(self):
        ours = self.put(AGENT_A, "a.py", b"ada\n")
        theirs = self.put(AGENT_B, "a.py", b"bob\n")
        conflicts = self.log("file.conflict")
        self.assertEqual(len(conflicts), 1)
        body = conflicts[0]["body"]
        self.assertEqual(body["path"], "a.py")
        self.assertEqual({body["ours"]["hash"], body["theirs"]["hash"]}, {ours, theirs})
        self.assertEqual({body["ours"]["agent"], body["theirs"]["agent"]}, {AGENT_A, AGENT_B})
        self.assertIn(body["kept_as"], self.index())
        self.assertTrue(body["kept_as"].startswith("a.py.parley-conflict-"))

    def test_the_conflict_event_is_hub_authored(self):
        self.put(AGENT_A, "a.py", b"ada\n")
        self.put(AGENT_B, "a.py", b"bob\n")
        self.assertTrue(protocol.is_hub_authored(self.log("file.conflict")[0]))

    def test_the_sidecar_is_itself_announced_as_a_file_put(self):
        """Every client must end up holding both versions on disk, which only happens
        if the sidecar arrives as a normal file.put."""
        self.put(AGENT_A, "a.py", b"ada\n")
        self.put(AGENT_B, "a.py", b"bob\n")
        sidecar_puts = [e for e in self.log("file.put")
                        if ".parley-conflict-" in e["body"]["path"]]
        self.assertEqual(len(sidecar_puts), 1)
        self.assertEqual(sidecar_puts[0]["body"]["hash"], sha(b"ada\n"))

    def test_the_sidecar_name_carries_the_agent_and_hash_so_it_is_identifiable(self):
        self.put(AGENT_A, "a.py", b"ada\n")
        self.put(AGENT_B, "a.py", b"bob\n")
        kept_as = self.log("file.conflict")[0]["body"]["kept_as"]
        suffix = kept_as[len("a.py.parley-conflict-"):]
        short_agent, _, short_hash = suffix.partition("-")
        self.assertTrue(short_agent, "the sidecar must name the displaced author")
        self.assertTrue(short_hash, "the sidecar must name the displaced content")
        self.assertIn(short_agent, AGENT_A)
        self.assertIn(short_hash, sha(b"ada\n"))

    def test_the_sidecar_path_is_still_a_legal_wire_path(self):
        self.put(AGENT_A, "deep/nested/a.py", b"ada\n")
        self.put(AGENT_B, "deep/nested/a.py", b"bob\n")
        kept_as = self.log("file.conflict")[0]["body"]["kept_as"]
        self.assertEqual(protocol.normalise_path(kept_as), kept_as)

    def test_three_way_divergence_preserves_all_three_versions(self):
        first = self.put(AGENT_A, "a.py", b"one\n")
        second = self.put(AGENT_B, "a.py", b"two\n")
        third = self.put(AGENT_A, "a.py", b"three\n")
        present = {record["hash"] for record in self.index().values()}
        for blob_hash in (first, second, third):
            self.assertIn(blob_hash, present, "R5: never lose a byte")
        self.assertEqual(self.index()["a.py"]["hash"], third)

    def test_re_sending_the_identical_content_is_not_a_conflict(self):
        self.put(AGENT_A, "a.py", b"same\n")
        self.put(AGENT_B, "a.py", b"same\n")
        self.assertEqual(self.log("file.conflict"), [],
                         "identical content is not a divergence worth preserving")
        self.assertEqual(len(self.index()), 1)


class TestDeleteVersusEdit(HubArbiterTestCase):
    """SPEC §7.7 — deletion loses data, so it loses ties."""

    def test_a_delete_with_a_matching_base_is_honoured(self):
        blob_hash = self.put(AGENT_A, "a.py", b"ada\n")
        self.delete(AGENT_B, "a.py", base=blob_hash)
        self.assertNotIn("a.py", self.index())
        self.assertEqual(self.log("file.conflict"), [])

    def test_a_delete_with_a_stale_base_keeps_the_file(self):
        self.put(AGENT_A, "a.py", b"ada\n")
        self.put(AGENT_A, "a.py", b"ada v2\n", base=sha(b"ada\n"))
        self.delete(AGENT_B, "a.py", base=sha(b"ada\n"))
        self.assertIn("a.py", self.index(), "SPEC §7.7: deletion loses ties")
        self.assertEqual(self.index()["a.py"]["hash"], sha(b"ada v2\n"))

    def test_a_refused_delete_emits_a_conflict_naming_the_original_path(self):
        self.put(AGENT_A, "a.py", b"ada\n")
        self.put(AGENT_A, "a.py", b"ada v2\n", base=sha(b"ada\n"))
        self.delete(AGENT_B, "a.py", base=sha(b"ada\n"))
        conflicts = self.log("file.conflict")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["body"]["kept_as"], "a.py",
                         "SPEC §7.7: kept_as is the original path")

    def test_a_delete_with_no_base_on_a_known_path_keeps_the_file(self):
        self.put(AGENT_A, "a.py", b"ada\n")
        self.delete(AGENT_B, "a.py")
        self.assertIn("a.py", self.index())

    def test_deleting_an_unknown_path_is_a_no_op(self):
        self.delete(AGENT_A, "never-existed.py")
        self.assertEqual(self.index(), {})

    def test_an_edit_after_a_delete_recreates_the_file(self):
        blob_hash = self.put(AGENT_A, "a.py", b"ada\n")
        self.delete(AGENT_A, "a.py", base=blob_hash)
        self.put(AGENT_B, "a.py", b"bob\n")
        self.assertEqual(self.index()["a.py"]["hash"], sha(b"bob\n"))


class TestLocksAreAdvisory(HubArbiterTestCase):
    """SPEC §4.5: a lock is a social signal. The Hub still accepts the write (R5)."""

    def test_a_write_to_a_locked_path_is_accepted_and_flagged(self):
        self.hub.submit(protocol.make_event(
            AGENT_A, self.hub.config.session, "lock.acquire",
            {"paths": ["a.py"], "ttl_s": 600, "intent": "rewriting"},
        ))
        self.put(AGENT_B, "a.py", b"bob wrote anyway\n")
        self.assertEqual(self.index()["a.py"]["hash"], sha(b"bob wrote anyway\n"),
                         "R5: the Hub MUST still accept the write")
        puts = [e for e in self.log("file.put") if e["actor"] == AGENT_B]
        self.assertTrue(puts[-1]["body"].get("lock_violation"),
                        "SPEC §4.5: the write MUST be flagged with lock_violation")

    def test_a_write_to_an_unlocked_path_is_not_flagged(self):
        self.put(AGENT_B, "b.py", b"fine\n")
        put = [e for e in self.log("file.put")][-1]
        self.assertFalse(put["body"].get("lock_violation"))

    def test_the_lock_holders_own_write_is_not_a_violation(self):
        self.hub.submit(protocol.make_event(
            AGENT_A, self.hub.config.session, "lock.acquire",
            {"paths": ["a.py"], "ttl_s": 600, "intent": "rewriting"},
        ))
        self.put(AGENT_A, "a.py", b"mine\n")
        put = [e for e in self.log("file.put")][-1]
        self.assertFalse(put["body"].get("lock_violation"))


class TestTheFileIndexFeedsTheLedger(HubArbiterTestCase):
    """SPEC §9's "authored substance" is computed from the Hub's file index.

    ``parley.ledger.compute`` is pure and may not open a blob, so the *indexer* has to
    record how many lines each stored text file has — ``parley.ledger.count_lines`` exists
    for exactly this and returns ``None`` for binary content. Without that key on the file
    record the whole component silently scores zero for every agent, and one of the five
    things the Deck shows is dead.
    """

    def test_a_text_file_is_indexed_with_a_line_count(self):
        self.put(AGENT_A, "a.py", b"one\ntwo\nthree\n")
        record = self.index()["a.py"]
        self.assertIn("lines", record,
                      "the file index must carry `lines` or ledger.compute() cannot score "
                      "authored substance (SPEC §9)")
        self.assertEqual(record["lines"], 3)

    def test_a_binary_file_is_indexed_as_binary_rather_than_line_counted(self):
        self.put(AGENT_A, "logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        record = self.index()["logo.png"]
        self.assertTrue(record.get("binary"),
                        "a binary file must be flagged so its bytes are not counted as lines")

    def test_authored_substance_is_actually_awarded_end_to_end(self):
        from parley import ledger

        self.put(AGENT_A, "big.py", b"line\n" * 120)
        result = ledger.compute(self.hub.store.read(since=0, limit=10000), self.index())
        line = result.line(AGENT_A)
        self.assertIsNotNone(line)
        self.assertGreater(
            line.components["authored"], 0.0,
            "the Ledger's authored-substance component is scoring zero for a 120-line "
            "file that Ada wrote; the file index is not recording line counts",
        )


class TestBadPathsAreRefusedByTheHub(HubArbiterTestCase):
    """SPEC §7.1: the Hub MUST reject a non-conforming path with 422 bad_path."""

    def test_a_traversal_path_never_reaches_the_file_index(self):
        from parley.errors import BadPath, ParleyError

        for path in ("../escape.py", "/etc/passwd", "a\\b.py", "C:\\x", "a/../b"):
            with self.subTest(path=path):
                try:
                    self.put(AGENT_A, path, b"x")
                except (BadPath, ParleyError, ValueError):
                    pass
                self.assertNotIn(path, self.index())
                for stored in self.index():
                    self.assertNotIn("..", stored.split("/"))


if __name__ == "__main__":
    unittest.main()
