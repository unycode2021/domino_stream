"""Tests for station feeds (stream_outputs). No live MediaMTX, Redis or Frappe site."""

import shlex
import unittest
from unittest.mock import patch

from domino_stream.api import mediamtx_bridge as bridge
from domino_stream.api import stream_outputs as so


class FakeResponse:
	def __init__(self, ok=True, data=None, status=200):
		self.ok = ok
		self._data = data or {}
		self.status_code = status

	def json(self):
		return self._data


class FakeCache:
	def __init__(self):
		self.store = {}

	def get_value(self, key, expires=False):
		return self.store.get(key)

	def set_value(self, key, value, expires_in_sec=None):
		self.store[key] = value

	def delete_value(self, key):
		self.store.pop(key, None)


class FakeNodes:
	def __init__(self):
		self.routes = {}

	def path_node(self, name):
		return {"id": "local"}

	def remember_path_node(self, name, node):
		self.routes[name] = node.get("id") or "local"

	def forget_path_node(self, name):
		self.routes.pop(name, None)

	def get_node(self, node_id):
		return {"id": node_id or "local"}

	def nodes_or_local(self):
		return [{"id": "local"}]


class FakeMediaMtx:
	"""Records path adds and deletes; answers the global config."""

	def __init__(self, srt=True):
		self.paths = {}
		self.calls = []
		self.srt = srt

	def request(self, method, path, payload=None, node=None):
		self.calls.append((method, path))
		if path == "/v3/config/global/get":
			return FakeResponse(data={"rtspAddress": "127.0.0.1:18554", "srt": self.srt, "srtAddress": ":18890"})
		if path.startswith("/v3/config/paths/add/"):
			self.paths[path.rsplit("/", 1)[1]] = payload
			return FakeResponse()
		if path.startswith("/v3/config/paths/delete/"):
			self.paths.pop(path.rsplit("/", 1)[1], None)
			return FakeResponse()
		if path.startswith("/v3/paths/get/"):
			return FakeResponse(data={"ready": path.rsplit("/", 1)[1] in self.paths})
		return FakeResponse(ok=False, status=404)


class TestPureHelpers(unittest.TestCase):
	def test_rtmp_push_joins_the_key(self):
		self.assertEqual(
			so.push_target_url("rtmps://ingest.tv.example/live/", "abc123"),
			"rtmps://ingest.tv.example/live/abc123",
		)

	def test_srt_push_takes_the_key_as_streamid_and_sets_packet_size(self):
		url = so.push_target_url("srt://ingest.tv.example:9000?latency=2000000", "K1")
		self.assertTrue(url.startswith("srt://ingest.tv.example:9000?"))
		self.assertIn("streamid=K1", url)
		self.assertIn("latency=2000000", url)
		self.assertIn("pkt_size=1316", url)

	def test_srt_push_needs_a_port(self):
		with self.assertRaises(so.OutputError):
			so.push_target_url("srt://ingest.tv.example", "")

	def test_push_refuses_other_schemes_and_local_hosts(self):
		for bad in ("http://tv.example/live", "rtmp://localhost/live", "rtmp://127.0.0.1/live",
				"srt://10.0.0.5:9000", "rtmp://[::1]/live", "rtmp://192.168.1.4/x", "rtmp:///nohost"):
			with self.assertRaises(so.OutputError, msg=bad):
				so.push_target_url(bad, "k")

	def test_label_hides_keys_and_passphrases(self):
		self.assertEqual(so.target_label("rtmps://ingest.tv.example:443/live/secret"), "rtmps://ingest.tv.example:443")
		self.assertEqual(so.target_label("srt://ingest.tv.example:9000?passphrase=xyz"), "srt://ingest.tv.example:9000")

	def test_broadcast_profile(self):
		argv = so.broadcast_video_args(25) + so.broadcast_audio_args(-23)
		self.assertIn("scale=1920:1080:force_original_aspect_ratio=decrease", argv[argv.index("-vf") + 1])
		self.assertEqual(argv[argv.index("-profile:v") + 1], "high")
		self.assertEqual(argv[argv.index("-g") + 1], "50")
		self.assertEqual(argv[argv.index("-minrate") + 1], "8000k")
		self.assertIn("loudnorm=I=-23", argv[argv.index("-af") + 1])
		self.assertEqual(argv[argv.index("-ar") + 1], "48000")
		self.assertEqual(argv[argv.index("-ac") + 1], "2")

	def test_profile_values_are_checked(self):
		with self.assertRaises(so.OutputError):
			so.broadcast_video_args(24)
		with self.assertRaises(so.OutputError):
			so.broadcast_audio_args(-16)
		with self.assertRaises(so.OutputError):
			so.profile_args("4k")

	def test_youtube_profile_reuses_the_owner_encode(self):
		argv = so.profile_args("youtube")
		self.assertEqual(argv[argv.index("-profile:v") + 1], "baseline")
		self.assertEqual(argv[argv.index("-c:a") + 1], "aac")
		self.assertEqual(argv[argv.index("-ar") + 1], "44100")

	def test_output_command_reads_its_own_path_and_picks_the_container(self):
		push = so.build_output_command("/opt/ffmpeg", "broadcast", "rtmps://tv.example/live/k")
		argv = shlex.split(push)
		self.assertEqual(argv[0], bridge.forward_wrapper_path())
		self.assertIn("rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH", argv)
		self.assertEqual(argv[argv.index("-f") + 1], "flv")
		self.assertEqual(argv[-1], "rtmps://tv.example/live/k")

		srt = shlex.split(so.build_output_command("/opt/ffmpeg", "broadcast", "srt://tv.example:9000?pkt_size=1316"))
		self.assertEqual(srt[srt.index("-f") + 1], "mpegts")

		pull = shlex.split(so.build_output_command("/opt/ffmpeg", "broadcast", so.pull_publish_target("pull-" + "a" * 32)))
		self.assertEqual(pull[-1], "rtsp://127.0.0.1:$RTSP_PORT/pull-" + "a" * 32)
		self.assertEqual(pull[len(pull) - 1 - pull[::-1].index("-f") + 1], "rtsp")

	def test_owner_forward_is_unchanged(self):
		argv = shlex.split(bridge.build_forward_command("rtmps://a.example/live/k", "/opt/ffmpeg"))
		self.assertEqual(argv[argv.index("-profile:v") + 1], "baseline")
		self.assertEqual(argv[argv.index("-b:v") + 1], "3500k")
		self.assertEqual(argv[argv.index("-i") + 1], "rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH")
		self.assertEqual(argv[-1], "rtmps://a.example/live/k")

	def test_path_configs(self):
		out = so.out_path_config("program-M1-" + "b" * 32, 18554, "cmd")
		self.assertEqual(out["source"], "rtsp://127.0.0.1:18554/program-M1-" + "b" * 32)
		self.assertFalse(out["sourceOnDemand"])
		self.assertTrue(out["runOnReadyRestart"])
		self.assertEqual(so.pull_path_config("p" * 32), {"source": "publisher", "srtReadPassphrase": "p" * 32})

	def test_pull_url_and_passphrase_length(self):
		passphrase = so.new_passphrase()
		self.assertTrue(10 <= len(passphrase) <= 79)
		url = so.pull_read_url("stream.example.com", 8890, "pull-" + "c" * 32, passphrase)
		self.assertTrue(url.startswith("srt://stream.example.com:8890?streamid=read:pull-" + "c" * 32))
		self.assertIn("passphrase=", url)

	def test_pull_path_names_match_the_mediamtx_rule(self):
		import re

		record = so.new_record("pull")
		self.assertRegex(record["pull_path"], re.compile(r"^pull-[0-9a-f]{32}$"))
		self.assertTrue(record["out_path"].startswith("out-"))

	def test_public_view_never_returns_the_push_key(self):
		record = so.new_record("push", name="TV One", target="rtmps://tv.example/live/SECRET")
		view = so.public_view(record)
		self.assertEqual(view["target"], "rtmps://tv.example")
		self.assertNotIn("SECRET", repr(view))

	def test_scrub_hides_srt_urls(self):
		text = bridge.scrub_rtmp_urls("push srt://tv.example:9000?passphrase=abc failed")
		self.assertNotIn("abc", text)
		self.assertIn("srt://[redacted]", text)

	def test_port_parsing(self):
		self.assertEqual(so.parse_port(":8890", 1), 8890)
		self.assertEqual(so.parse_port("127.0.0.1:18554", 1), 18554)
		self.assertEqual(so.parse_port("", 7), 7)


class TestLifecycle(unittest.TestCase):
	def setUp(self):
		self.cache = FakeCache()
		self.nodes = FakeNodes()
		self.mtx = FakeMediaMtx()
		patches = [
			patch.object(bridge, "_cache", lambda: self.cache),
			patch.object(bridge, "_nodes", lambda: self.nodes),
			patch.object(bridge, "_request", self.mtx.request),
			patch.object(bridge, "resolve_ffmpeg_bin", lambda: "/opt/ffmpeg"),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def test_output_added_before_go_live_starts_on_go_live_and_follows_new_paths(self):
		added = so.add_output("program", "MCH1", "push", name="TV", url="rtmps://tv.example/live", stream_key="K")
		self.assertFalse(added["output"]["running"])
		self.assertEqual(self.mtx.paths, {})

		first = "program-MCH1-" + "1" * 32
		so.follow_source("program", "MCH1", first, {"id": "local"})
		out_paths = [n for n in self.mtx.paths if n.startswith("out-")]
		self.assertEqual(len(out_paths), 1)
		self.assertTrue(self.mtx.paths[out_paths[0]]["source"].endswith("/" + first))
		self.assertIn("rtmps://tv.example/live/K", self.mtx.paths[out_paths[0]]["runOnReady"])

		second = "program-MCH1-" + "2" * 32
		so.follow_source("program", "MCH1", second, {"id": "local"})
		self.assertEqual([n for n in self.mtx.paths if n.startswith("out-")], out_paths)
		self.assertTrue(self.mtx.paths[out_paths[0]]["source"].endswith("/" + second))

	def test_pull_output_keeps_its_address_across_go_lives(self):
		so.follow_source("table", "TBL9", "table-TBL9-" + "3" * 32, {"id": "local"})
		with patch.object(bridge, "cached_table_path_name", lambda t: "table-TBL9-" + "3" * 32):
			added = so.add_output("table", "TBL9", "pull", name="Station", public_origin="https://stream.example.com")
		url = added["output"]["srt_url"]
		self.assertTrue(url.startswith("srt://stream.example.com:18890?streamid=read:pull-"))
		pull = next(n for n in self.mtx.paths if n.startswith("pull-"))
		self.assertIn("srtReadPassphrase", self.mtx.paths[pull])

		so.stop_if_source("table", "TBL9", ["table-TBL9-" + "3" * 32])
		self.assertEqual(self.mtx.paths, {})
		so.follow_source("table", "TBL9", "table-TBL9-" + "4" * 32, {"id": "local"})
		self.assertIn(pull, self.mtx.paths)
		with patch.object(bridge, "cached_table_path_name", lambda t: "table-TBL9-" + "4" * 32):
			listed = so.list_outputs("table", "TBL9", public_origin="https://stream.example.com")
		self.assertEqual(listed["outputs"][0]["srt_url"], url)
		self.assertTrue(listed["outputs"][0]["ready"])

	def test_pull_needs_srt_on_the_node(self):
		self.mtx.srt = False
		so.follow_source("table", "TBL2", "table-TBL2-" + "5" * 32, {"id": "local"})
		with patch.object(bridge, "cached_table_path_name", lambda t: "table-TBL2-" + "5" * 32):
			added = so.add_output("table", "TBL2", "pull")
		self.assertIn("SRT is off", added["output"]["error"])
		self.assertEqual(self.mtx.paths, {})

	def test_closing_another_session_leaves_the_outputs_running(self):
		owner = "program-MCH3-" + "6" * 32
		so.follow_source("program", "MCH3", owner, {"id": "local"})
		so.add_output("program", "MCH3", "push", url="rtmp://tv.example/live", stream_key="k")
		self.assertTrue(any(n.startswith("out-") for n in self.mtx.paths))
		so.stop_if_source("program", "MCH3", ["program-MCH3-" + "7" * 32])
		self.assertTrue(any(n.startswith("out-") for n in self.mtx.paths))
		so.stop_if_source("program", "MCH3", [owner])
		self.assertFalse(any(n.startswith("out-") for n in self.mtx.paths))

	def test_remove_and_limit(self):
		for i in range(so.MAX_OUTPUTS):
			so.add_output("table", "TBL5", "push", url=f"rtmp://tv{i}.example/live", stream_key="k")
		with self.assertRaises(so.OutputError):
			so.add_output("table", "TBL5", "push", url="rtmp://tv9.example/live", stream_key="k")
		first = so._load("table", "TBL5")[0]["id"]
		self.assertTrue(so.remove_output("table", "TBL5", first)["removed"])
		self.assertEqual(len(so._load("table", "TBL5")), so.MAX_OUTPUTS - 1)

	def test_bridge_go_live_hook_never_raises(self):
		with patch.object(so, "follow_source", side_effect=RuntimeError("boom")), patch.object(
			bridge, "_log_output_error"
		) as logged:
			bridge._outputs_follow("program", "MCH4", "program-MCH4-x", {"id": "local"})
		logged.assert_called_once()


if __name__ == "__main__":
	unittest.main()
