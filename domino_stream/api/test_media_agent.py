"""Pure tests for domino_media_agent. No Redis, no MediaMTX, no FFmpeg."""

import socket
import struct
import threading
import unittest

from domino_stream import media_agent


class FakeRelays:
	def __init__(self, active=True):
		self._active = active
		self.chunks = []

	def active(self, relay_id):
		return self._active

	def write_chunk(self, relay_id, data):
		self.chunks.append((relay_id, data))
		return True


def _serve(settings, relays):
	server = media_agent._ThreadingTCPServer(("127.0.0.1", 0), media_agent.make_chunk_handler(settings, relays))
	threading.Thread(target=server.serve_forever, daemon=True).start()
	return server


def _handshake(port, line):
	sock = socket.create_connection(("127.0.0.1", port), timeout=5)
	sock.sendall(line.encode())
	reply = b""
	while not reply.endswith(b"\n"):
		part = sock.recv(64)
		if not part:
			break
		reply += part
	return sock, reply.strip()


class TestMediaAgent(unittest.TestCase):
	def test_settings_prefer_environment_over_common_config(self):
		settings = media_agent.load_settings(
			env={"DOMINO_MEDIA_BIND_HOST": "10.0.0.5", "DOMINO_MEDIA_PUBLIC_HOST": "203.0.113.9"},
			conf={"domino_media_bind_host": "127.0.0.1", "domino_media_agent_token": "abc", "redis_queue": "redis://q:1"},
		)
		self.assertEqual(settings["bind_host"], "10.0.0.5")
		self.assertEqual(settings["advertise_host"], "10.0.0.5")
		self.assertEqual(settings["mediamtx_api"], "http://10.0.0.5:9997")
		self.assertEqual(settings["whip_upstream"], "http://10.0.0.5:8889")
		self.assertEqual(settings["public_host"], "203.0.113.9")
		self.assertEqual(settings["token"], "abc")
		self.assertEqual(settings["redis_url"], "redis://q:1")

	def test_single_bench_defaults_stay_on_loopback(self):
		settings = media_agent.load_settings(env={}, conf={})
		self.assertEqual(settings["bind_host"], "127.0.0.1")
		self.assertEqual(settings["mediamtx_api"], "http://127.0.0.1:9997")
		self.assertEqual(settings["http_port"], 9010)
		self.assertEqual(settings["chunk_port"], 9011)

	def test_token_check(self):
		self.assertTrue(media_agent.token_ok("", None))
		self.assertTrue(media_agent.token_ok("s3cret", "s3cret"))
		self.assertFalse(media_agent.token_ok("s3cret", "nope"))
		self.assertFalse(media_agent.token_ok("s3cret", None))

	def test_relay_ids_reject_paths(self):
		self.assertTrue(media_agent.valid_relay_id("program-MCH0001"))
		self.assertTrue(media_agent.valid_relay_id("TBL-0001"))
		self.assertFalse(media_agent.valid_relay_id("../etc"))
		self.assertFalse(media_agent.valid_relay_id("a b"))
		self.assertFalse(media_agent.valid_relay_id(""))

	def test_chunks_reach_the_relay_in_order(self):
		relays = FakeRelays()
		server = _serve({"token": "tok"}, relays)
		try:
			sock, reply = _handshake(server.server_address[1], "DMA1 TBL tok\n")
			self.assertEqual(reply, b"OK")
			for payload in (b"one", b"two"):
				sock.sendall(struct.pack(">I", len(payload)) + payload)
			sock.close()
			for _ in range(50):
				if len(relays.chunks) == 2:
					break
				threading.Event().wait(0.02)
			self.assertEqual(relays.chunks, [("TBL", b"one"), ("TBL", b"two")])
		finally:
			server.shutdown()

	def test_wrong_token_is_refused(self):
		relays = FakeRelays()
		server = _serve({"token": "tok"}, relays)
		try:
			sock, reply = _handshake(server.server_address[1], "DMA1 TBL wrong\n")
			sock.close()
			self.assertEqual(reply, b"ERR forbidden")
			self.assertEqual(relays.chunks, [])
		finally:
			server.shutdown()

	def test_missing_relay_is_reported(self):
		server = _serve({"token": ""}, FakeRelays(active=False))
		try:
			sock, reply = _handshake(server.server_address[1], "DMA1 TBL\n")
			sock.close()
			self.assertEqual(reply, b"ERR missing")
		finally:
			server.shutdown()
