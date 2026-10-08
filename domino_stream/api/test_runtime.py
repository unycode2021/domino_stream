"""Pure tests for MediaMTX/ffmpeg setup. No download and no live MediaMTX."""

import hashlib
import tarfile
import tempfile
import unittest
from pathlib import Path

from domino_stream.runtime import (
	LOCATIONS_BEGIN,
	PROCFILE_BEGIN,
	UPSTREAM_BEGIN,
	ChecksumError,
	apply_nginx_config,
	apply_procfile,
	extract_archive_member,
	render_procfile_block,
	render_supervisor_config,
	verify_sha256,
)

FORWARD_SCRIPT = Path(__file__).resolve().parents[2] / "mediamtx-forward.sh"

NGINX = """\
upstream demo-frappe {
	server 127.0.0.1:8000 fail_timeout=0;
}

server {
	location /socket.io {
		proxy_pass http://demo-socketio-server;
	}

	location / {
		try_files $uri @webserver;
	}
}

server {
	listen 80;
	return 301 https://$host$request_uri;
}
"""

HAND_EDITED_NGINX = """\
upstream frappe-bench-mediamtx {
	server 127.0.0.1:8889 fail_timeout=0;
}
upstream frappe-bench-domino-stream-io-server {
	server 127.0.0.1:9003 fail_timeout=0;
}

server {
	location /mediamtx/ {
		proxy_buffering off;
		proxy_pass http://frappe-bench-mediamtx/;
	}

	location /domino-stream-socket.io {
		proxy_pass http://frappe-bench-domino-stream-io-server;
	}

	location / {
		try_files $uri @webserver;
	}
}
"""

PROCFILE = """\
web: bench serve --port 8000

domino_stream_io: /old/node apps/domino_stream/socketio.js
domino_stream_mediamtx: /old/mediamtx /old/mediamtx.yml
# Promote RQ enqueue_at jobs (kill-challenge force-stop) onto worker queues
domino_stream_rq_scheduler: /old/python apps/domino_stream/rq_scheduler.py

watch: bench watch
"""


class TestRuntimeConfig(unittest.TestCase):
	def test_checksum_mismatch_removes_the_archive(self):
		with tempfile.TemporaryDirectory() as tmp:
			archive = Path(tmp) / "mediamtx.tar.gz"
			archive.write_bytes(b"not-the-pinned-bytes")
			with self.assertRaises(ChecksumError):
				verify_sha256(archive, "0" * 64)
			self.assertFalse(archive.exists())

	def test_checksum_match_keeps_the_archive(self):
		payload = b"mediamtx-bytes"
		expect = hashlib.sha256(payload).hexdigest()
		with tempfile.TemporaryDirectory() as tmp:
			archive = Path(tmp) / "mediamtx.tar.gz"
			archive.write_bytes(payload)
			verify_sha256(archive, expect)
			self.assertEqual(archive.read_bytes(), payload)

	def test_extract_takes_the_binary_and_leaves_config(self):
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			archive = root / "mediamtx.tar.gz"
			binary = root / "src-mediamtx"
			shipped = root / "src-mediamtx.yml"
			binary.write_text("#!/bin/sh\necho v1.21.1\n", encoding="utf-8")
			shipped.write_text("replaced\n", encoding="utf-8")
			with tarfile.open(archive, "w:gz") as tar:
				tar.add(binary, arcname="mediamtx")
				tar.add(shipped, arcname="mediamtx.yml")
			dest_dir = root / "out"
			dest_dir.mkdir()
			config = dest_dir / "mediamtx.yml"
			config.write_text("keep\n", encoding="utf-8")
			extract_archive_member(archive, "mediamtx", dest_dir / "mediamtx")
			self.assertIn("v1.21.1", (dest_dir / "mediamtx").read_text(encoding="utf-8"))
			self.assertEqual(config.read_text(encoding="utf-8"), "keep\n")
			self.assertFalse((dest_dir / "mediamtx.partial").exists())

	def test_nginx_markers_are_idempotent(self):
		once = apply_nginx_config(NGINX, "demo", io_port=9003)
		twice = apply_nginx_config(once, "demo", io_port=9003)
		self.assertEqual(once, twice)
		self.assertEqual(once.count("location /mediamtx/"), 1)
		self.assertEqual(once.count("location /domino-stream-socket.io"), 1)
		self.assertIn("proxy_buffering off;", once)
		self.assertIn(UPSTREAM_BEGIN, once)
		self.assertIn(LOCATIONS_BEGIN, once)
		self.assertIn("127.0.0.1:9003", once)
		redirect = once.rsplit("\nserver {", 1)[1]
		self.assertNotIn("location /mediamtx/", redirect)
		self.assertIn("return 301", redirect)

	def test_hand_edited_nginx_is_left_in_place(self):
		self.assertEqual(apply_nginx_config(HAND_EDITED_NGINX, "frappe-bench"), HAND_EDITED_NGINX)

	def test_procfile_markers_are_idempotent(self):
		block = render_procfile_block(
			node="/usr/bin/node",
			app_dir="/opt/bench/apps/domino_stream",
			mediamtx="/opt/bench/apps/domino_stream/bin/mediamtx",
			python="/opt/bench/env/bin/python",
		)
		once = apply_procfile(PROCFILE, block)
		twice = apply_procfile(once, block)
		self.assertEqual(once, twice)
		self.assertEqual(once.count(PROCFILE_BEGIN), 1)
		self.assertEqual(once.count("domino_stream_mediamtx:"), 1)
		self.assertNotIn("/old/mediamtx", once)
		self.assertNotIn("Promote RQ", once)
		self.assertIn("web: bench serve --port 8000", once)
		self.assertIn("watch: bench watch", once)
		self.assertIn("/opt/bench/apps/domino_stream/bin/mediamtx", once)

	def test_supervisor_config_names_the_three_programs(self):
		text = render_supervisor_config(
			bench_name="frappe-bench",
			bench_dir="/opt/bench",
			app_dir="/opt/bench/apps/domino_stream",
			user="adowie",
			node="/usr/bin/node",
			site="www.example.com",
		)
		self.assertIn("[program:frappe-bench-domino-stream-mediamtx]", text)
		self.assertIn("[program:frappe-bench-domino-stream-io]", text)
		self.assertIn("[program:frappe-bench-domino-stream-rq-scheduler]", text)
		self.assertIn("/opt/bench/apps/domino_stream/bin/mediamtx /opt/bench/apps/domino_stream/mediamtx.yml", text)
		self.assertIn('FRAPPE_SITE="www.example.com"', text)
		without_node = render_supervisor_config(
			bench_name="frappe-bench",
			bench_dir="/opt/bench",
			app_dir="/opt/bench/apps/domino_stream",
			user="adowie",
			node=None,
			site="",
		)
		self.assertNotIn("[program:frappe-bench-domino-stream-io]", without_node)
		self.assertNotIn("FRAPPE_SITE", without_node)
		self.assertIn("[program:frappe-bench-domino-stream-mediamtx]", without_node)

	def test_forward_log_follows_the_bench(self):
		text = FORWARD_SCRIPT.read_text(encoding="utf-8")
		self.assertNotIn("/home/adowie", text)
		self.assertIn('log="$bench_dir/logs/mediamtx-forward.log"', text)


if __name__ == "__main__":
	unittest.main()
