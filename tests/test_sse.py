"""SPEC §5.1 and §5.2 — SSE framing and resume.

SSE looks trivial until it meets a real network. A frame can be split at *any* byte by the
TCP stack, a proxy, or a gzip boundary — including between the ``\\r`` and the ``\\n`` of a
CRLF — and a naive parser will happily dispatch half an event. So the headline test here
replays the same stream once for every possible split position and demands an identical
result each time.

The second property is resume: ``id:`` carries the ``seq``, and a reconnect that resumes
from the wrong one either loses events (R5 violation) or replays them (duplicate work).
"""

from __future__ import annotations

import json
import unittest

from parley.client.transport import SSEParser, Transport
from tests.helpers import ScriptedHTTPServer, SESSION, AGENT_A

EVENT_ONE = {"v": "PARLEY/1", "seq": 1284, "type": "chat.message",
             "body": {"text": "I'll take the sync reconciler."}}
EVENT_TWO = {"v": "PARLEY/1", "seq": 1285, "type": "status.update",
             "body": {"state": "working", "headline": "Wiring SSE"}}


def frame(seq, payload, *, name="parley", newline=b"\n"):
    lines = [
        b"id: " + str(seq).encode("ascii"),
        b"event: " + name.encode("ascii"),
        b"data: " + json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        b"",
    ]
    return newline.join(lines) + newline


def parse_all(chunks):
    parser = SSEParser()
    out = []
    for chunk in chunks:
        out.extend(parser.feed(chunk))
    out.extend(parser.close())
    return out, parser


class TestFrameParsing(unittest.TestCase):
    def test_a_single_frame_parses(self):
        frames, _ = parse_all([frame(1284, EVENT_ONE)])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["event"], "parley")
        self.assertEqual(frames[0]["id"], "1284")
        self.assertEqual(json.loads(frames[0]["data"]), EVENT_ONE)

    def test_two_frames_in_one_chunk_both_parse(self):
        frames, _ = parse_all([frame(1284, EVENT_ONE) + frame(1285, EVENT_TWO)])
        self.assertEqual([f["id"] for f in frames], ["1284", "1285"])

    def test_the_default_event_name_is_message(self):
        frames, _ = parse_all([b"data: hello\n\n"])
        self.assertEqual(frames[0]["event"], "message")

    def test_a_single_leading_space_after_the_colon_is_stripped_and_only_one(self):
        frames, _ = parse_all([b"data:  two spaces\n\n"])
        self.assertEqual(frames[0]["data"], " two spaces")
        frames, _ = parse_all([b"data:nospace\n\n"])
        self.assertEqual(frames[0]["data"], "nospace")

    def test_multi_line_data_is_joined_with_newlines(self):
        frames, _ = parse_all([b"event: parley\ndata: line one\ndata: line two\n"
                               b"data: line three\n\n"])
        self.assertEqual(frames[0]["data"], "line one\nline two\nline three")

    def test_an_empty_data_line_contributes_an_empty_line(self):
        frames, _ = parse_all([b"data: a\ndata:\ndata: b\n\n"])
        self.assertEqual(frames[0]["data"], "a\n\nb")

    def test_comment_lines_produce_no_frames(self):
        """SPEC §5.1: `: ping` every 15 s keeps proxies from idling the connection out."""
        frames, _ = parse_all([b": ping\n", b": ping\n", b":\n"])
        self.assertEqual(frames, [])

    def test_a_comment_between_frames_does_not_disturb_them(self):
        frames, _ = parse_all([frame(1, EVENT_ONE) + b": ping\n\n" + frame(2, EVENT_TWO)])
        self.assertEqual([f["id"] for f in frames], ["1", "2"])

    def test_a_comment_is_recorded_as_proof_of_life(self):
        parser = SSEParser()
        self.assertEqual(parser.last_comment_at, 0.0)
        parser.feed(b": ping\n")
        self.assertGreater(parser.last_comment_at, 0.0,
                           "a keepalive is how a client knows the path is still open")

    def test_a_field_with_no_colon_is_a_field_with_an_empty_value(self):
        frames, _ = parse_all([b"data\n\n"])
        self.assertEqual(frames[0]["data"], "")

    def test_unknown_fields_are_ignored(self):
        frames, _ = parse_all([b"future: whatever\ndata: payload\nalso-future\n\n"])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["data"], "payload")

    def test_a_blank_line_with_no_data_dispatches_nothing(self):
        frames, _ = parse_all([b"\n\n\n", b"event: parley\n\n"])
        self.assertEqual(frames, [])

    def test_a_retry_field_is_captured(self):
        _, parser = parse_all([b"retry: 2500\ndata: x\n\n"])
        self.assertEqual(parser.retry_ms, 2500)

    def test_a_malformed_retry_field_is_ignored_rather_than_fatal(self):
        _, parser = parse_all([b"retry: soon\ndata: x\n\n"])
        self.assertIsNone(parser.retry_ms)

    def test_a_utf8_bom_at_the_start_of_the_stream_is_stripped(self):
        frames, _ = parse_all([b"\xef\xbb\xbf" + frame(1, EVENT_ONE)])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["id"], "1")

    def test_a_bom_arriving_one_byte_at_a_time_is_still_stripped(self):
        payload = b"\xef\xbb\xbf" + frame(1, EVENT_ONE)
        frames, _ = parse_all([payload[i:i + 1] for i in range(len(payload))])
        self.assertEqual(len(frames), 1)

    def test_unicode_payloads_survive(self):
        event = {"type": "chat.message", "body": {"text": "héllo ☃ — naïve"}}
        frames, _ = parse_all([frame(7, event)])
        self.assertEqual(json.loads(frames[0]["data"]), event)

    def test_an_id_containing_a_null_byte_is_ignored(self):
        _, parser = parse_all([b"id: 12\x0034\ndata: x\n\n"])
        self.assertIsNone(parser.last_event_id)


class TestLineTerminators(unittest.TestCase):
    def test_lf_crlf_and_cr_all_frame_identically(self):
        reference = None
        for newline in (b"\n", b"\r\n", b"\r"):
            with self.subTest(newline=newline):
                frames, _ = parse_all([frame(1284, EVENT_ONE, newline=newline)])
                self.assertEqual(len(frames), 1)
                if reference is None:
                    reference = frames[0]
                self.assertEqual(frames[0], reference)

    def test_a_crlf_split_across_chunks_is_not_two_line_ends(self):
        """The classic bug: a lone trailing CR must be held back, not acted on."""
        payload = frame(1284, EVENT_ONE, newline=b"\r\n")
        cut = payload.index(b"\r\n") + 1
        frames, _ = parse_all([payload[:cut], payload[cut:]])
        self.assertEqual(len(frames), 1)
        self.assertEqual(json.loads(frames[0]["data"]), EVENT_ONE)

    def test_mixed_terminators_in_one_stream_are_tolerated(self):
        payload = frame(1, EVENT_ONE, newline=b"\n") + frame(2, EVENT_TWO, newline=b"\r\n")
        frames, _ = parse_all([payload])
        self.assertEqual([f["id"] for f in frames], ["1", "2"])


class TestAdversarialChunkBoundaries(unittest.TestCase):
    """Replay the same stream split at every possible byte and demand one answer."""

    def reference(self, payload):
        frames, parser = parse_all([payload])
        return frames, parser.last_event_id

    def assert_split_invariant(self, payload):
        expected_frames, expected_id = self.reference(payload)
        for cut in range(len(payload) + 1):
            with self.subTest(cut=cut):
                frames, parser = parse_all([payload[:cut], payload[cut:]])
                self.assertEqual(frames, expected_frames,
                                 "split at byte {0} changed the result".format(cut))
                self.assertEqual(parser.last_event_id, expected_id)

    def test_a_single_lf_frame_survives_every_split(self):
        self.assert_split_invariant(frame(1284, EVENT_ONE))

    def test_a_single_crlf_frame_survives_every_split(self):
        self.assert_split_invariant(frame(1284, EVENT_ONE, newline=b"\r\n"))

    def test_two_frames_with_a_keepalive_survive_every_split(self):
        payload = (frame(1284, EVENT_ONE) + b": ping\n\n"
                   + frame(1285, EVENT_TWO, newline=b"\r\n"))
        self.assert_split_invariant(payload)

    def test_a_stream_delivered_one_byte_at_a_time_is_identical(self):
        payload = frame(1, EVENT_ONE) + b": ping\n" + frame(2, EVENT_TWO)
        expected, _ = self.reference(payload)
        frames, _ = parse_all([payload[i:i + 1] for i in range(len(payload))])
        self.assertEqual(frames, expected)

    def test_a_stream_delivered_in_three_random_but_fixed_cuts_is_identical(self):
        payload = frame(1, EVENT_ONE) + frame(2, EVENT_TWO) + frame(3, EVENT_ONE)
        expected, _ = self.reference(payload)
        for cuts in ((5, 40), (1, len(payload) - 1), (len(payload) // 2, len(payload) // 2)):
            with self.subTest(cuts=cuts):
                a, b = cuts
                frames, _ = parse_all([payload[:a], payload[a:b], payload[b:]])
                self.assertEqual(frames, expected)


class TestTruncationAtEOF(unittest.TestCase):
    """A half-written frame must be discarded; resume replays it (R5 is preserved)."""

    def test_a_frame_cut_before_its_blank_line_is_not_dispatched(self):
        payload = frame(1284, EVENT_ONE)
        frames, _ = parse_all([payload[:-1]])
        self.assertEqual(frames, [], "a frame without its terminator is incomplete")

    def test_a_frame_cut_mid_data_is_not_dispatched(self):
        payload = frame(1284, EVENT_ONE)
        cut = payload.index(b"data: ") + 10
        frames, _ = parse_all([payload[:cut]])
        self.assertEqual(frames, [])

    def test_a_complete_frame_followed_by_a_truncated_one_keeps_the_complete_one(self):
        payload = frame(1284, EVENT_ONE) + frame(1285, EVENT_TWO)[:20]
        frames, _ = parse_all([payload])
        self.assertEqual([f["id"] for f in frames], ["1284"])

    def test_a_trailing_cr_at_eof_terminates_its_line(self):
        payload = frame(1284, EVENT_ONE, newline=b"\r\n")
        parser = SSEParser()
        got = parser.feed(payload[:-1])  # everything but the final LF
        got += parser.close()
        self.assertEqual(len(got), 1, "the held-back CR must be flushed at EOF")

    def test_closing_twice_is_harmless(self):
        parser = SSEParser()
        parser.feed(frame(1, EVENT_ONE))
        self.assertEqual(parser.close(), [])
        self.assertEqual(parser.close(), [])

    def test_a_discarded_partial_frame_does_not_bleed_into_the_next_parse(self):
        parser = SSEParser()
        parser.feed(b"data: half")
        parser.close()
        frames = parser.feed(frame(9, EVENT_TWO))
        self.assertEqual(len(frames), 1)
        self.assertEqual(json.loads(frames[0]["data"]), EVENT_TWO)


class TestResume(unittest.TestCase):
    """SPEC §5.1: ``id:`` is the ``seq``, so Last-Event-ID resumes exactly."""

    def test_the_last_event_id_tracks_the_most_recent_frame(self):
        _, parser = parse_all([frame(10, EVENT_ONE) + frame(11, EVENT_TWO)
                               + frame(12, EVENT_ONE)])
        self.assertEqual(parser.last_event_id, "12")

    def test_the_last_event_id_persists_across_frames_without_an_id(self):
        """Per the SSE spec the id field is sticky; a client must not reset to zero."""
        _, parser = parse_all([frame(10, EVENT_ONE) + b"data: {}\n\n"])
        self.assertEqual(parser.last_event_id, "10")

    def test_the_last_event_id_is_none_before_anything_arrives(self):
        self.assertIsNone(SSEParser().last_event_id)

    def test_the_id_is_updated_even_when_the_frame_carries_no_data(self):
        _, parser = parse_all([b"id: 42\n\n"])
        self.assertEqual(parser.last_event_id, "42")

    def test_a_partial_frame_at_eof_leaves_the_resume_point_on_the_last_whole_frame(self):
        payload = frame(10, EVENT_ONE) + b"id: 11\nevent: parley\ndata: {\"seq\":11"
        _, parser = parse_all([payload])
        self.assertEqual(
            parser.last_event_id, "11",
            "the id line was complete; resuming from 11 would skip the truncated frame",
        )


class TestTransportConsumesAStream(unittest.TestCase):
    """End to end over a real socket, with the server controlling the chunk boundaries."""

    def transport(self, url):
        transport = Transport(url, SESSION, AGENT_A, b"k" * 32, timeout=5.0)
        self.addCleanup(transport.close)
        return transport

    def collect(self, transport, since, count):
        got = []
        stream = transport.stream(since)
        try:
            for event in stream:
                got.append(event)
                if len(got) >= count:
                    break
        finally:
            stream.close()
        return got

    def test_events_arrive_in_seq_order_from_a_live_stream(self):
        payload = frame(1284, EVENT_ONE) + b": ping\n\n" + frame(1285, EVENT_TWO)
        with ScriptedHTTPServer([payload]) as server:
            got = self.collect(self.transport(server.url), 1283, 2)
        self.assertEqual([e["seq"] for e in got], [1284, 1285])
        self.assertEqual(got[0]["body"]["text"], EVENT_ONE["body"]["text"])

    def test_a_stream_split_across_writes_still_delivers_every_event(self):
        payload = frame(1284, EVENT_ONE) + frame(1285, EVENT_TWO)
        chunks = [payload[i:i + 7] for i in range(0, len(payload), 7)]
        with ScriptedHTTPServer(chunks) as server:
            got = self.collect(self.transport(server.url), 0, 2)
        self.assertEqual([e["seq"] for e in got], [1284, 1285])

    def test_the_resume_point_is_sent_to_the_hub_as_since(self):
        with ScriptedHTTPServer([frame(1284, EVENT_ONE)]) as server:
            self.collect(self.transport(server.url), 1283, 1)
            paths = [path for _, path, _ in server.requests]
        self.assertTrue(paths, "the transport never connected")
        self.assertIn("since=1283", paths[0])

    def test_the_stream_request_is_authenticated_like_any_other(self):
        with ScriptedHTTPServer([frame(1284, EVENT_ONE)]) as server:
            self.collect(self.transport(server.url), 0, 1)
            headers = server.requests[0][2]
        lowered = {k.lower(): v for k, v in headers.items()}
        self.assertIn("authorization", lowered)
        self.assertTrue(lowered["authorization"].startswith("Parley-HMAC-SHA256 "))
        self.assertEqual(lowered.get("x-parley-session"), SESSION)
        self.assertEqual(lowered.get("x-parley-agent"), AGENT_A)

    def test_a_non_json_data_payload_does_not_kill_the_stream(self):
        payload = b"event: parley\ndata: not json at all\n\n" + frame(1285, EVENT_TWO)
        with ScriptedHTTPServer([payload]) as server:
            got = self.collect(self.transport(server.url), 0, 1)
        self.assertEqual([e["seq"] for e in got], [1285])


if __name__ == "__main__":
    unittest.main()
