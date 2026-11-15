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
	upstream_path_name,
	whip_public_url,
)


class TestMediaMtxBridge(unittest.TestCase):
	def test_path_name_hides_a_token(self):
		name = program_path_name("MCH00967", "ab12cd34")
		self.assertEqual(name, "program-MCH00967-ab12cd34")
		self.assertTrue(name.startswith(program_path_prefix("MCH00967")))

	def test_path_token_is_not_guessable(self):
		from domino_stream.api.mediamtx_bridge import _PATH_TOKEN_BYTES

		self.assertGreaterEqual(_PATH_TOKEN_BYTES, 16)

	def test_path_name_strips_punctuation(self):
		self.assertEqual(program_path_name("MCH/00967", "aa-bb"), "program-MCH00967-aabb")

	def test_upstream_path_name_finds_the_node_key(self):
		self.assertEqual(upstream_path_name("/program-MCH1-ab12/whip"), "program-MCH1-ab12")
		self.assertEqual(upstream_path_name("/program-MCH1-ab12/whip/sess-9"), "program-MCH1-ab12")

	def test_no_node_uses_this_bench_mediamtx_from_site_config(self):
		from domino_stream.api import mediamtx_bridge as bridge

		class Nodes:
			@staticmethod
			def local_node():
				return {"mediamtx_api": "http://127.0.0.1:19997/", "whip_upstream": "http://127.0.0.1:18889"}

		with patch.object(bridge, "_nodes", return_value=Nodes):
			self.assertEqual(bridge._api_base(None), "http://127.0.0.1:19997")
			self.assertEqual(bridge._api_base({"mediamtx_api": "http://b:9997"}), "http://b:9997")

	def test_no_node_falls_back_to_the_default_port(self):
		from domino_stream.api import mediamtx_bridge as bridge

		with patch.object(bridge, "_nodes", side_effect=RuntimeError("no frappe")):
			self.assertEqual(bridge._api_base(None), "http://127.0.0.1:9997")

	def test_forward_status_reads_every_media_node(self):
		from domino_stream.api import mediamtx_bridge as bridge

		node_a = {"id": "a", "mediamtx_api": "http://a:9997"}
		node_b = {"id": "b", "mediamtx_api": "http://b:9997"}
		seen = []

		def fake_forwarding(prefix, node):
			seen.append(node["id"])
			return node["id"] == "b"

		class Nodes:
			@staticmethod
			def nodes_or_local():
				return [node_a, node_b]

		with patch.object(bridge, "_nodes", return_value=Nodes), patch.object(
			bridge, "_node_forwarding", side_effect=fake_forwarding
		):
			status = bridge.program_forward_status("MCH1")
		self.assertTrue(status["forwarding"])
		self.assertEqual(seen, ["a", "b"])

	def test_forward_status_unknown_when_a_node_cannot_be_read(self):
		from domino_stream.api import mediamtx_bridge as bridge

		class Nodes:
			@staticmethod
			def nodes_or_local():
				return [{"id": "a"}, {"id": "b"}]

		with patch.object(bridge, "_nodes", return_value=Nodes), patch.object(
			bridge, "_node_forwarding", side_effect=[False, None]
		):
			status = bridge.program_forward_status("MCH1")
		self.assertFalse(status["forwarding"])
		self.assertFalse(status["known"])

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
		self.assertEqual(
			forward_audio_args("opus")[:4],
			["-af", "aresample=async=1:min_hard_comp=0.100000", "-c:a", "aac"],
		)
		self.assertNotIn("first_pts", " ".join(forward_audio_args("opus")))
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
		self.assertNotIn("-thread_queue_size", argv)
		self.assertEqual(argv[argv.index("-b:v") + 1], "3500k")
		self.assertEqual(argv[argv.index("-minrate") + 1], "3500k")
		self.assertEqual(argv[argv.index("-maxrate") + 1], "3500k")
		self.assertEqual(argv[argv.index("-bufsize") + 1], "3500k")
		self.assertEqual(argv[argv.index("-x264-params") + 1], "nal-hrd=cbr:force-cfr=1")
		self.assertEqual(
			argv[argv.index("-af") + 1],
			"aresample=async=1:min_hard_comp=0.100000",
		)
		self.assertLess(argv.index("-af"), argv.index("-c:a"))
		self.assertEqual(argv[argv.index("-c:a") + 1], "aac")
		self.assertEqual(argv[argv.index("-b:a") + 1], "128k")
		self.assertEqual(argv[argv.index("-ar") + 1], "44100")
		self.assertIn("no_duration_filesize", argv)
		self.assertEqual(argv[argv.index("-muxdelay") + 1], "0")
		self.assertEqual(argv[argv.index("-muxpreload") + 1], "0")
		self.assertEqual(argv[argv.index("-max_interleave_delta") + 1], "100000")
		self.assertLess(argv.index("-max_interleave_delta"), argv.index(push))
		self.assertEqual(argv[-1], push)
		self.assertNotIn("nobuffer", command)
		self.assertNotIn("genpts", command)
		self.assertNotIn("first_pts", command)

	def test_forward_command_copies_aac_and_still_reencodes_video(self):
		command = build_forward_command("rtmps://example/live/k", "/opt/ffmpeg", audio_codec="aac")
		argv = shlex.split(command)
		self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
		self.assertEqual(argv[argv.index("-c:a") + 1], "copy")
		self.assertNotIn("-af", argv)
		self.assertEqual(argv[argv.index("-minrate") + 1], "3500k")
		self.assertEqual(argv[argv.index("-max_interleave_delta") + 1], "100000")

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

	def test_proxy_allows_table_paths_and_whep(self):
		from domino_stream.api.mediamtx_bridge import mediamtx_upstream_path

		self.assertEqual(
			mediamtx_upstream_path("/mediamtx/table-TBL123-ab12/whip"),
			"/table-TBL123-ab12/whip",
		)
		self.assertEqual(
			mediamtx_upstream_path("/mediamtx/table-TBL123-ab12/whep/sess"),
			"/table-TBL123-ab12/whep/sess",
		)
		self.assertEqual(
			mediamtx_upstream_path("/mediamtx/program-MCH1-ab12/whep"),
			"/program-MCH1-ab12/whep",
		)
		self.assertIsNone(mediamtx_upstream_path("/mediamtx/other-TBL123-ab12/whip"))
		self.assertIsNone(mediamtx_upstream_path("/mediamtx/table-TBL123-ab12"))

	def test_table_path_names(self):
		from domino_stream.api.mediamtx_bridge import (
			table_cache_key,
			table_path_name,
			table_path_prefix,
			whep_public_url,
		)

		self.assertEqual(table_path_name("TBL-59/59", "ab12"), "table-TBL5959-ab12")
		self.assertEqual(table_path_prefix("TBL-5959"), "table-TBL5959-")
		self.assertEqual(table_cache_key("TBL-5959"), "mediamtx_table:TBL5959")
		self.assertEqual(
			whep_public_url("https://dev.domino101.com/", "table-TBL5959-ab12"),
			"https://dev.domino101.com/mediamtx/table-TBL5959-ab12/whep",
		)
		with self.assertRaises(ValueError):
			table_path_name("", "ab12")

	def test_table_path_status_ready_and_unknown(self):
		from domino_stream.api import mediamtx_bridge as bridge

		class Nodes:
			@staticmethod
			def nodes_or_local():
				return [{"mediamtx_api": "http://n1"}]

		with patch.object(bridge, "_nodes", return_value=Nodes), patch.object(
			bridge, "cached_table_path_name", return_value="table-T1-ab"
		):
			with patch.object(bridge, "_node_forwarding", return_value=True):
				self.assertTrue(bridge.table_path_status("T1")["ready"])
			with patch.object(bridge, "_node_forwarding", return_value=None):
				state = bridge.table_path_status("T1")
				self.assertFalse(state["ready"])
				self.assertFalse(state["known"])
			with patch.object(bridge, "_node_forwarding", return_value=False):
				state = bridge.table_path_status("T1")
				self.assertEqual((state["ready"], state["known"]), (False, True))

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

	def test_advertise_origin_skips_patch_when_host_already_set(self):
		"""A PATCH restarts MediaMTX WebRTC and drops every live session."""
		from domino_stream.api import mediamtx_bridge as bridge

		class Resp:
			def __init__(self, data):
				self.ok = True
				self._data = data

			def json(self):
				return self._data

		calls = []

		def fake_request(method, path, payload=None, node=None):
			calls.append(method)
			if method == "GET":
				return Resp({"webrtcAdditionalHosts": ["dev.domino101.com"]})
			return Resp({})

		with patch.object(bridge, "_request", side_effect=fake_request):
			bridge._advertise_origin("https://dev.domino101.com", None)
		self.assertEqual(calls, ["GET"])

		calls.clear()

		def fake_request_empty(method, path, payload=None, node=None):
			calls.append((method, payload))
			return Resp({"webrtcAdditionalHosts": []} if method == "GET" else {})

		with patch.object(bridge, "_request", side_effect=fake_request_empty):
			bridge._advertise_origin("https://dev.domino101.com", None)
		self.assertEqual(calls[-1], ("PATCH", {"webrtcAdditionalHosts": ["dev.domino101.com"]}))


def _write_fake_ffmpeg(path: Path, text: str) -> None:
	path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{text}'\n", encoding="utf-8")
	path.chmod(0o755)


if __name__ == "__main__":
	unittest.main()
