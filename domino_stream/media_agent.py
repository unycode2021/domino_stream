"""domino_media_agent: one per media node, next to MediaMTX.

- Heartbeat: writes ``domino_media_node:{id}`` (JSON, 30 s TTL) to redis_queue
  with this node's MediaMTX, WHIP, agent and chunk addresses plus its load.
  Gunicorn picks nodes from these keys (``domino_stream.api.media_nodes``).
- Relay control: private HTTP API that starts, stops and reports the
  composite relay writer (``domino101/api/youtube_rtmp_relay.py``) on this node.
- Chunk ingest: private TCP port. dominoio and gunicorn send composite chunks
  here; the agent hands each chunk to the relay writer's Unix socket.

Runs without Frappe. Settings come from sites/common_site_config.json with
environment overrides for containers::

    python media_agent.py
"""

from __future__ import annotations

import hmac
import importlib.util
import json
import os
import re
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import redis
import requests

NODE_PREFIX = "domino_media_node:"
TOKEN_HEADER = "X-Domino-Media-Token"
HEARTBEAT_SECONDS = 10
NODE_TTL_MS = 30 * 1000
READY_TIMEOUT_SECONDS = 8
MAX_FRAME_BYTES = 16 * 1024 * 1024
CHUNK_HANDSHAKE = "DMA1"
_RELAY_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,140}$")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def bench_root() -> Path:
	return Path(__file__).resolve().parents[3]


def read_common_config(bench: Path) -> dict:
	try:
		data = json.loads((bench / "sites" / "common_site_config.json").read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}
	return data if isinstance(data, dict) else {}


def load_settings(env: dict | None = None, conf: dict | None = None) -> dict:
	"""Agent settings. Environment wins over common_site_config."""
	env = os.environ if env is None else env
	bench = bench_root()
	conf = read_common_config(bench) if conf is None else conf

	def pick(env_key, conf_key, default):
		value = env.get(env_key)
		if value not in (None, ""):
			return value
		value = conf.get(conf_key)
		return default if value in (None, "") else value

	bind_host = str(pick("DOMINO_MEDIA_BIND_HOST", "domino_media_bind_host", "127.0.0.1"))
	advertise_host = str(pick("DOMINO_MEDIA_ADVERTISE_HOST", "domino_media_advertise_host", bind_host))
	if advertise_host in ("0.0.0.0", "::"):
		advertise_host = socket.gethostname()
	api_port = int(pick("DOMINO_MEDIAMTX_API_PORT", "domino_mediamtx_api_port", 9997))
	whip_port = int(pick("DOMINO_MEDIAMTX_WHIP_PORT", "domino_mediamtx_whip_port", 8889))
	return {
		"node_id": str(pick("DOMINO_MEDIA_NODE_ID", "domino_media_node_id", socket.gethostname())),
		"redis_url": str(pick("DOMINO_MEDIA_REDIS", "redis_queue", "redis://127.0.0.1:11000")),
		"token": str(pick("DOMINO_MEDIA_AGENT_TOKEN", "domino_media_agent_token", "")).strip(),
		"bind_host": bind_host,
		"advertise_host": advertise_host,
		"http_port": int(pick("DOMINO_MEDIA_AGENT_PORT", "domino_media_agent_port", 9010)),
		"chunk_port": int(pick("DOMINO_MEDIA_CHUNK_PORT", "domino_media_chunk_port", 9011)),
		"public_host": str(pick("DOMINO_MEDIA_PUBLIC_HOST", "domino_media_public_host", "")).strip(),
		"mediamtx_local_api": f"http://127.0.0.1:{api_port}",
		"mediamtx_api": f"http://{advertise_host}:{api_port}",
		"whip_upstream": f"http://{advertise_host}:{whip_port}",
		"log_dir": str(pick("DOMINO_MEDIA_LOG_DIR", "domino_media_log_dir", str(bench / "logs"))),
		"relay_script": str(
			pick(
				"DOMINO_RELAY_SCRIPT",
				"domino_relay_script",
				str(bench / "apps" / "domino101" / "domino101" / "api" / "youtube_rtmp_relay.py"),
			)
		),
	}


def token_ok(expected: str, given: str | None) -> bool:
	"""An empty token is only allowed when the agent listens on loopback (checked at start)."""
	if not expected:
		return True
	return hmac.compare_digest(expected.encode(), (given or "").encode())


def valid_relay_id(relay_id: str) -> bool:
	return bool(relay_id) and bool(_RELAY_ID_RE.match(relay_id))


def load_relay_module(script_path: str):
	"""Import the relay writer module by file so the domino101 package (and Frappe) is not loaded."""
	spec = importlib.util.spec_from_file_location("domino_youtube_rtmp_relay", script_path)
	if spec is None or spec.loader is None:
		raise RuntimeError(f"relay script not found: {script_path}")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class RelayHost:
	"""Relays on this node. Stateless: liveness comes from the writer's ready file."""

	def __init__(self, settings: dict, relay_module=None):
		self.settings = settings
		self.relay = relay_module or load_relay_module(settings["relay_script"])
		self._locks: dict[str, threading.Lock] = {}
		self._locks_guard = threading.Lock()

	def lock_for(self, relay_id: str) -> threading.Lock:
		with self._locks_guard:
			lock = self._locks.get(relay_id)
			if lock is None:
				lock = threading.Lock()
				self._locks[relay_id] = lock
			return lock

	def paths(self, relay_id: str) -> dict:
		return self.relay.relay_paths(relay_id)

	def record(self, relay_id: str) -> dict | None:
		ready = self.relay._read_ready(self.paths(relay_id)["ready_path"])
		if not ready:
			return None
		if not (self.relay.pid_alive(ready.get("writer_pid")) and self.relay.pid_alive(ready.get("ffmpeg_pid"))):
			return None
		return ready

	def active(self, relay_id: str) -> bool:
		return self.record(relay_id) is not None

	def active_count(self) -> int:
		count = 0
		for ready in Path("/tmp").glob("domino_yt_*.ready"):
			data = self.relay._read_ready(str(ready))
			if data and self.relay.pid_alive(data.get("writer_pid")) and self.relay.pid_alive(data.get("ffmpeg_pid")):
				count += 1
		return count

	def stop(self, relay_id: str) -> None:
		ready = self.relay._read_ready(self.paths(relay_id)["ready_path"]) or {}
		self.relay._kill_pid(ready.get("writer_pid"))
		self.relay._kill_pid(ready.get("ffmpeg_pid"))
		for path in self.paths(relay_id).values():
			if isinstance(path, str) and path.startswith("/tmp/"):
				try:
					os.remove(path)
				except OSError:
					pass

	def start(self, relay_id: str, output_url: str, input_format: str | None) -> dict:
		"""Start the writer (replacing any old one). Success only once FFmpeg is up."""
		if not self.relay.resolve_ffmpeg_bin():
			return {"success": False, "message": "ffmpeg is not installed"}
		input_format = self.relay.normalize_relay_input_format(input_format)
		self.stop(relay_id)
		paths = self.paths(relay_id)
		os.makedirs(self.settings["log_dir"], exist_ok=True)
		log_path = os.path.join(self.settings["log_dir"], f"youtube_rtmp_{paths['token']}.log")
		writer = subprocess.Popen(
			[
				sys.executable,
				self.settings["relay_script"],
				"--pipe",
				paths["pipe_path"],
				"--socket",
				paths["socket_path"],
				"--output-url",
				output_url,
				"--log",
				log_path,
				"--ready",
				paths["ready_path"],
				"--input-format",
				input_format,
			],
			stdin=subprocess.DEVNULL,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			start_new_session=True,
		)
		deadline = time.time() + READY_TIMEOUT_SECONDS
		ready = None
		while time.time() < deadline:
			if writer.poll() is not None:
				break
			ready = self.record(relay_id)
			if ready:
				break
			time.sleep(0.1)
		if not ready:
			self.relay._kill_pid(writer.pid)
			self.stop(relay_id)
			detail = self.relay._log_tail(log_path)
			message = "YouTube relay failed to start"
			if detail:
				message = f"{message}: {detail}"
			return {"success": False, "message": message}
		return {
			"success": True,
			"writer_pid": int(ready["writer_pid"]),
			"ffmpeg_pid": int(ready["ffmpeg_pid"]),
			"input_format": input_format,
			"log_path": log_path,
		}

	def write_chunk(self, relay_id: str, data: bytes) -> bool:
		"""One chunk into the writer's Unix socket. Chunks of one relay never interleave."""
		socket_path = self.paths(relay_id)["socket_path"]
		with self.lock_for(relay_id):
			sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
			sock.settimeout(5)
			try:
				sock.connect(socket_path)
				sock.sendall(data)
			except OSError:
				return False
			finally:
				try:
					sock.close()
				except OSError:
					pass
		return True


def mediamtx_path_count(settings: dict) -> int:
	try:
		response = requests.get(f"{settings['mediamtx_local_api']}/v3/paths/list?itemsPerPage=1", timeout=3)
		data = response.json() if response.ok else {}
	except (requests.RequestException, ValueError):
		return 0
	return int(data.get("itemCount") or 0) if isinstance(data, dict) else 0


def advertise_public_host(settings: dict) -> None:
	"""Point WebRTC ICE at this node's public address. Patch only when it differs."""
	host = settings.get("public_host")
	if not host:
		return
	base = settings["mediamtx_local_api"]
	try:
		current = requests.get(f"{base}/v3/config/global/get", timeout=3)
		hosts = (current.json() or {}).get("webrtcAdditionalHosts") if current.ok else None
		if hosts == [host]:
			return
		requests.patch(f"{base}/v3/config/global/patch", json={"webrtcAdditionalHosts": [host]}, timeout=3)
	except (requests.RequestException, ValueError):
		pass


def node_record(settings: dict, relays: RelayHost) -> dict:
	agent_host = settings["advertise_host"]
	return {
		"id": settings["node_id"],
		"mediamtx_api": settings["mediamtx_api"],
		"whip_upstream": settings["whip_upstream"],
		"agent_url": f"http://{agent_host}:{settings['http_port']}",
		"chunk_host": agent_host,
		"chunk_port": settings["chunk_port"],
		"public_host": settings["public_host"],
		"paths": mediamtx_path_count(settings),
		"relays": relays.active_count(),
		"updated_at": int(time.time()),
	}


def heartbeat_loop(settings: dict, relays: RelayHost, stop: threading.Event) -> None:
	client = redis.Redis.from_url(settings["redis_url"])
	key = f"{NODE_PREFIX}{settings['node_id']}"
	while not stop.is_set():
		try:
			advertise_public_host(settings)
			client.set(key, json.dumps(node_record(settings, relays)), px=NODE_TTL_MS)
		except Exception as exc:
			print(f"[media-agent] heartbeat failed: {exc}", flush=True)
		stop.wait(HEARTBEAT_SECONDS)
	try:
		client.delete(key)
	except Exception:
		pass


def make_http_handler(settings: dict, relays: RelayHost):
	class Handler(BaseHTTPRequestHandler):
		def log_message(self, fmt, *args):  # request lines can carry relay ids only; keep quiet
			return

		def _send(self, status: int, body: dict) -> None:
			raw = json.dumps(body).encode()
			self.send_response(status)
			self.send_header("Content-Type", "application/json")
			self.send_header("Content-Length", str(len(raw)))
			self.end_headers()
			self.wfile.write(raw)

		def _authorized(self) -> bool:
			if token_ok(settings["token"], self.headers.get(TOKEN_HEADER)):
				return True
			self._send(403, {"ok": False, "message": "forbidden"})
			return False

		def _relay_route(self):
			parts = [unquote(p) for p in self.path.split("?", 1)[0].strip("/").split("/")]
			if len(parts) >= 2 and parts[0] == "relays" and valid_relay_id(parts[1]):
				return parts[1], (parts[2] if len(parts) > 2 else "")
			return None, None

		def _json_body(self) -> dict:
			length = int(self.headers.get("Content-Length") or 0)
			if length <= 0 or length > 64 * 1024:
				return {}
			try:
				data = json.loads(self.rfile.read(length))
			except ValueError:
				return {}
			return data if isinstance(data, dict) else {}

		def do_GET(self):
			if not self._authorized():
				return
			if self.path.rstrip("/") == "/health":
				self._send(200, {"ok": True, "node_id": settings["node_id"]})
				return
			relay_id, action = self._relay_route()
			if relay_id and not action:
				self._send(200, {"ok": True, "active": relays.active(relay_id)})
				return
			self._send(404, {"ok": False, "message": "not found"})

		def do_POST(self):
			if not self._authorized():
				return
			relay_id, action = self._relay_route()
			if not relay_id:
				self._send(404, {"ok": False, "message": "not found"})
				return
			if action == "start":
				body = self._json_body()
				output_url = (body.get("output_url") or "").strip()
				if not output_url.lower().startswith("rtmp"):
					self._send(400, {"ok": False, "success": False, "message": "output_url is required"})
					return
				result = relays.start(relay_id, output_url, body.get("input_format"))
				self._send(200, {"ok": True, **result})
				return
			if action == "stop":
				relays.stop(relay_id)
				self._send(200, {"ok": True, "success": True})
				return
			self._send(404, {"ok": False, "message": "not found"})

	return Handler


def read_exact(conn: socket.socket, size: int) -> bytes | None:
	buf = bytearray()
	while len(buf) < size:
		chunk = conn.recv(size - len(buf))
		if not chunk:
			return None
		buf.extend(chunk)
	return bytes(buf)


def read_line(conn: socket.socket, limit: int = 512) -> str | None:
	buf = bytearray()
	while len(buf) < limit:
		ch = conn.recv(1)
		if not ch:
			return None
		if ch == b"\n":
			return buf.decode("utf-8", errors="replace")
		buf.extend(ch)
	return None


def make_chunk_handler(settings: dict, relays: RelayHost):
	"""``DMA1 <relay_id> <token>\\n`` then frames of 4-byte big-endian length + bytes.

	The agent answers ``OK`` or ``ERR <reason>``. When the relay is gone the
	connection closes, which tells the sender to restart the relay.
	"""

	class ChunkHandler(socketserver.BaseRequestHandler):
		def handle(self):
			conn = self.request
			conn.settimeout(120)
			try:
				line = read_line(conn)
				parts = (line or "").split(" ")
				if len(parts) < 2 or parts[0] != CHUNK_HANDSHAKE:
					conn.sendall(b"ERR handshake\n")
					return
				relay_id = parts[1]
				given = parts[2] if len(parts) > 2 else ""
				if not token_ok(settings["token"], given):
					conn.sendall(b"ERR forbidden\n")
					return
				if not valid_relay_id(relay_id) or not relays.active(relay_id):
					conn.sendall(b"ERR missing\n")
					return
				conn.sendall(b"OK\n")
				while True:
					header = read_exact(conn, 4)
					if header is None:
						return
					(size,) = struct.unpack(">I", header)
					if size <= 0 or size > MAX_FRAME_BYTES:
						return
					data = read_exact(conn, size)
					if data is None:
						return
					if not relays.write_chunk(relay_id, data):
						return
			except OSError:
				return

	return ChunkHandler


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
	daemon_threads = True
	allow_reuse_address = True


def _raise_interrupt(_signum, _frame):
	raise KeyboardInterrupt


def main() -> int:
	signal.signal(signal.SIGTERM, _raise_interrupt)
	settings = load_settings()
	if not settings["token"] and settings["bind_host"] not in _LOOPBACK:
		print(
			"[media-agent] domino_media_agent_token is required when binding beyond loopback",
			flush=True,
		)
		return 1
	relays = RelayHost(settings)
	stop = threading.Event()

	chunk_server = _ThreadingTCPServer(
		(settings["bind_host"], settings["chunk_port"]), make_chunk_handler(settings, relays)
	)
	threading.Thread(target=chunk_server.serve_forever, daemon=True).start()
	threading.Thread(target=heartbeat_loop, args=(settings, relays, stop), daemon=True).start()

	http_server = ThreadingHTTPServer(
		(settings["bind_host"], settings["http_port"]), make_http_handler(settings, relays)
	)
	print(
		f"[media-agent] node {settings['node_id']} http :{settings['http_port']} "
		f"chunks :{settings['chunk_port']} on {settings['bind_host']}",
		flush=True,
	)
	try:
		http_server.serve_forever()
	except KeyboardInterrupt:
		pass
	finally:
		stop.set()
		chunk_server.shutdown()
		http_server.server_close()
	return 0


if __name__ == "__main__":
	sys.exit(main())
