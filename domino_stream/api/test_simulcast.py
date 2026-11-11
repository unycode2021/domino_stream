# Copyright (c) 2026, Unycode Limited and contributors
# License: MIT
"""Simulcast layer choice for spectator pulls. No database."""

import unittest

from domino_stream.api import simulcast

SIMULCAST_OFFER = "\r\n".join(
	[
		"v=0",
		"m=audio 9 UDP/TLS/RTP/SAVPF 111",
		"a=mid:0",
		"a=rid:x send",
		"m=video 9 UDP/TLS/RTP/SAVPF 96",
		"a=mid:1",
		"a=rid:f send",
		"a=rid:h send",
		"a=rid:q send",
		"a=simulcast:send f;h;q",
		"",
	]
)

SINGLE_OFFER = "\r\n".join(
	["v=0", "m=audio 9 UDP/TLS/RTP/SAVPF 111", "a=mid:0", "m=video 9 UDP/TLS/RTP/SAVPF 96", "a=mid:1", ""]
)


class TestParseRids(unittest.TestCase):
	def test_reads_video_send_rids_only(self):
		self.assertEqual(simulcast.parse_send_rids(SIMULCAST_OFFER), ["f", "h", "q"])

	def test_single_layer_offer_has_none(self):
		self.assertEqual(simulcast.parse_send_rids(SINGLE_OFFER), [])
		self.assertEqual(simulcast.parse_send_rids(None), [])

	def test_recv_rids_are_ignored(self):
		offer = "m=video 9 UDP/TLS/RTP/SAVPF 96\na=rid:f recv\na=rid:h recv\n"
		self.assertEqual(simulcast.parse_send_rids(offer), [])

	def test_field_round_trip(self):
		self.assertEqual(simulcast.rids_to_field(["f", "h", "q"]), "f,h,q")
		self.assertEqual(simulcast.rids_from_field("f, h,q"), ["f", "h", "q"])
		self.assertEqual(simulcast.rids_from_field(None), [])


class TestRidChoice(unittest.TestCase):
	def test_guest_gets_h_signed_in_gets_f(self):
		self.assertEqual(simulcast.preferred_rid("guest", ["f", "h", "q"]), "h")
		self.assertEqual(simulcast.preferred_rid("user", ["f", "h", "q"]), "f")
		self.assertEqual(simulcast.preferred_rid(None, ["f", "h", "q"]), "f")

	def test_single_layer_publisher_has_no_preference(self):
		self.assertIsNone(simulcast.preferred_rid("guest", []))
		self.assertIsNone(simulcast.simulcast_for("guest", []))
		self.assertIsNone(simulcast.simulcast_for("user", None))

	def test_guest_falls_back_to_what_exists(self):
		self.assertEqual(simulcast.preferred_rid("guest", ["f", "q"]), "q")
		self.assertEqual(simulcast.preferred_rid("guest", ["f"]), "f")

	def test_signed_in_falls_back_to_best_layer(self):
		self.assertEqual(simulcast.preferred_rid("user", ["h", "q"]), "h")

	def test_cloudflare_object_shape(self):
		self.assertEqual(
			simulcast.simulcast_for("guest", ["f", "h", "q"]),
			{"preferredRid": "h", "priorityOrdering": "asciibetical", "ridNotAvailable": "asciibetical"},
		)


if __name__ == "__main__":
	unittest.main()
