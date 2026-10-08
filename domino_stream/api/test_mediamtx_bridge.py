"""Pure tests for the MediaMTX WHIP → RTMPS command. No live MediaMTX."""

import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from domino_stream.api.mediamtx_bridge import (
	app_ffmpeg_path,
	audio_needs_aac,
	build_forward_command,
	ffmpeg_candidate_paths,
	forward_wrapper_path,
	forward_audio_args,
	program_path_config,
	program_path_name,
	program_path_prefix,
	resolve_ffmpeg_bin,
	rtmps_push_url,
	scrub_rtmp_urls,
	session_cache_key,
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

	def test_forward_command_reencodes_video_on_the_wall_clock(self):
		push = "rtmps://live.cloudflare.com:443/live/secret-key"
		command = build_forward_command(push, "/opt/ffmpeg", audio_codec="opus")
		argv = shlex.split(command)
		self.assertEqual(argv[0], forward_wrapper_path())
		self.assertEqual(argv[1], "/opt/ffmpeg")
		self.assertIn("rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH", argv)
		self.assertLess(argv.index("+discardcorrupt"), argv.index("-i"))
		self.assertLess(argv.index("low_delay"), argv.index("-i"))
		self.assertEqual(argv[argv.index("-analyzeduration") + 1], "3000000")
		self.assertEqual(argv[argv.index("-probesize") + 1], "5000000")
		self.assertEqual(argv[argv.index("-max_delay") + 1], "500000")
		self.assertLess(argv.index("-max_delay"), argv.index("-i"))
		self.assertEqual(argv[argv.index("-use_wallclock_as_timestamps") + 1], "1")
		self.assertLess(argv.index("-use_wallclock_as_timestamps"), argv.index("-i"))
		self.assertEqual(
			argv[argv.index("-vf") + 1],
			"fps=30,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
		)
		self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
		self.assertEqual(argv[argv.index("-preset") + 1], "veryfast")
		self.assertEqual(argv[argv.index("-tune") + 1], "zerolatency")
		self.assertEqual(argv[argv.index("-profile:v") + 1], "baseline")
		self.assertEqual(argv[argv.index("-bf") + 1], "0")
		self.assertEqual(argv[argv.index("-g") + 1], "60")
		self.assertEqual(argv[argv.index("-keyint_min") + 1], "60")
		self.assertEqual(argv[argv.index("-r") + 1], "30")
		self.assertEqual(argv[argv.index("-b:v") + 1], "4500k")
		self.assertEqual(argv[argv.index("-maxrate") + 1], "4500k")
		self.assertEqual(argv[argv.index("-bufsize") + 1], "1500k")
		self.assertEqual(argv[argv.index("-c:a") + 1], "aac")
		self.assertEqual(argv[argv.index("-b:a") + 1], "128k")
		self.assertEqual(argv[argv.index("-ar") + 1], "44100")
		self.assertIn("no_duration_filesize", argv)
		self.assertEqual(argv[argv.index("-muxdelay") + 1], "0")
		self.assertEqual(argv[argv.index("-muxpreload") + 1], "0")
		self.assertEqual(argv[-1], push)
		self.assertNotIn("nobuffer", command)
		self.assertNotIn("genpts", command)

	def test_forward_command_copies_aac_and_still_reencodes_video(self):
		command = build_forward_command("rtmps://example/live/k", "/opt/ffmpeg", audio_codec="aac")
		argv = shlex.split(command)
		self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
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

	def test_session_paths_do_not_share_a_cache_key(self):
		first = session_cache_key("MCH00967", "aaa111")
		second = session_cache_key("MCH00967", "bbb222")
		self.assertNotEqual(first, second)
		self.assertTrue(first.endswith(":aaa111"))

	def test_angle_path_has_no_youtube_forward(self):
		angle = program_path_config(None)
		owner = program_path_config("ffmpeg push")
		self.assertEqual(angle, {"source": "publisher"})
		self.assertNotIn("runOnAvailable", angle)
		self.assertEqual(owner["runOnAvailable"], "ffmpeg push")
		self.assertTrue(owner["runOnAvailableRestart"])

	def test_scrub_hides_the_push_url(self):
		text = scrub_rtmp_urls("push rtmps://live.cloudflare.com:443/live/secret failed")
		self.assertNotIn("secret", text)
		self.assertIn("rtmps://[redacted]", text)

	def test_candidate_order_starts_with_the_app_binary(self):
		self.assertEqual(ffmpeg_candidate_paths()[0], app_ffmpeg_path())

	def test_app_binary_with_libx264_wins(self):
		with tempfile.TemporaryDirectory() as tmp:
			app_bin = Path(tmp) / "app-ffmpeg"
			system_bin = Path(tmp) / "system-ffmpeg"
			_write_fake_ffmpeg(app_bin, "libx264")
			_write_fake_ffmpeg(system_bin, "libx264")
			with patch(
				"domino_stream.api.mediamtx_bridge.ffmpeg_candidate_paths",
				return_value=[str(app_bin), str(system_bin)],
			):
				self.assertEqual(resolve_ffmpeg_bin(), str(app_bin))

	def test_binary_without_libx264_is_skipped(self):
		with tempfile.TemporaryDirectory() as tmp:
			app_bin = Path(tmp) / "app-ffmpeg"
			system_bin = Path(tmp) / "system-ffmpeg"
			_write_fake_ffmpeg(app_bin, "aac only")
			_write_fake_ffmpeg(system_bin, "libx264")
			with patch(
				"domino_stream.api.mediamtx_bridge.ffmpeg_candidate_paths",
				return_value=[str(app_bin), str(system_bin)],
			):
				self.assertEqual(resolve_ffmpeg_bin(), str(system_bin))


def _write_fake_ffmpeg(path: Path, text: str) -> None:
	path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{text}'\n", encoding="utf-8")
	path.chmod(0o755)


if __name__ == "__main__":
	unittest.main()
