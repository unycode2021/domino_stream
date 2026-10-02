"""Pure tests for the MediaMTX WHIP → RTMPS command. No live MediaMTX."""

import shlex
import unittest

from domino_stream.api.mediamtx_bridge import (
	audio_needs_aac,
	build_forward_command,
	forward_wrapper_path,
	forward_audio_args,
	program_path_name,
	program_path_prefix,
	rtmps_push_url,
	scrub_rtmp_urls,
	whip_public_url,
)


class TestMediaMtxBridge(unittest.TestCase):
	def test_path_name_hides_a_token(self):
		name = program_path_name("MCH00967", "ab12cd34")
		self.assertEqual(name, "program-MCH00967-ab12cd34")
		self.assertTrue(name.startswith(program_path_prefix("MCH00967")))

	def test_path_name_strips_punctuation(self):
		self.assertEqual(program_path_name("MCH/00967", "aa-bb"), "program-MCH00967-aabb")

	def test_rtmps_push_url_joins_key(self):
		url = rtmps_push_url("rtmps://live.cloudflare.com:443/live/", "abc123")
		self.assertEqual(url, "rtmps://live.cloudflare.com:443/live/abc123")

	def test_rtmps_push_url_does_not_double_the_key(self):
		url = rtmps_push_url("rtmps://live.cloudflare.com:443/live/abc123", "abc123")
		self.assertEqual(url, "rtmps://live.cloudflare.com:443/live/abc123")

	def test_opus_becomes_aac_and_aac_is_copied(self):
		self.assertTrue(audio_needs_aac("opus"))
		self.assertTrue(audio_needs_aac(""))
		self.assertFalse(audio_needs_aac("aac"))
		self.assertEqual(forward_audio_args("opus")[:2], ["-c:a", "aac"])
		self.assertEqual(forward_audio_args("aac"), ["-c:a", "copy"])

	def test_forward_command_copies_video_and_transcodes_opus(self):
		push = "rtmps://live.cloudflare.com:443/live/secret-key"
		command = build_forward_command(push, "/opt/ffmpeg", audio_codec="opus")
		argv = shlex.split(command)
		self.assertEqual(argv[0], forward_wrapper_path())
		self.assertEqual(argv[1], "/opt/ffmpeg")
		self.assertIn("rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH", argv)
		self.assertLess(argv.index("+discardcorrupt"), argv.index("-i"))
		self.assertLess(argv.index("low_delay"), argv.index("-i"))
		self.assertEqual(argv[argv.index("-analyzeduration") + 1], "1000000")
		self.assertEqual(argv[argv.index("-probesize") + 1], "65536")
		self.assertEqual(argv[argv.index("-max_delay") + 1], "500000")
		self.assertLess(argv.index("-max_delay"), argv.index("-i"))
		self.assertEqual(argv[argv.index("-c:v") + 1], "copy")
		self.assertEqual(argv[argv.index("-c:a") + 1], "aac")
		self.assertIn("no_duration_filesize", argv)
		self.assertEqual(argv[argv.index("-muxdelay") + 1], "0")
		self.assertEqual(argv[argv.index("-muxpreload") + 1], "0")
		self.assertEqual(argv[-1], push)
		self.assertNotIn("libx264", argv)
		self.assertNotIn("nobuffer", command)
		self.assertNotIn("genpts", command)
		self.assertNotIn("use_wallclock_as_timestamps", command)

	def test_forward_command_copies_aac(self):
		command = build_forward_command("rtmps://example/live/k", "/opt/ffmpeg", audio_codec="aac")
		argv = shlex.split(command)
		self.assertEqual(argv[argv.index("-c:a") + 1], "copy")

	def test_whip_url_uses_the_public_prefix(self):
		url = whip_public_url("https://www.domino101.com/", "program-MCH00967-ab12")
		self.assertEqual(url, "https://www.domino101.com/mediamtx/program-MCH00967-ab12/whip")

	def test_proxy_allows_only_program_whip_paths(self):
		from domino_stream.api.mediamtx_bridge import mediamtx_upstream_path

		self.assertEqual(
			mediamtx_upstream_path("/mediamtx/program-MCH00967-ab12/whip"),
			"/program-MCH00967-ab12/whip",
		)
		self.assertEqual(
			mediamtx_upstream_path("/mediamtx/program-MCH00967-ab12/whip/session"),
			"/program-MCH00967-ab12/whip/session",
		)
		self.assertIsNone(mediamtx_upstream_path("/mediamtx/v3/config/paths/list"))
		self.assertIsNone(mediamtx_upstream_path("/mediamtx/program-MCH00967-ab12/../whip"))
		self.assertIsNone(mediamtx_upstream_path("/api/method/ping"))

	def test_scrub_hides_the_push_url(self):
		text = scrub_rtmp_urls("push rtmps://live.cloudflare.com:443/live/secret failed")
		self.assertNotIn("secret", text)
		self.assertIn("rtmps://[redacted]", text)


if __name__ == "__main__":
	unittest.main()
