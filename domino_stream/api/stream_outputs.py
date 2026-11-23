"""Extra outputs for a Match Program or a Domino101 table stream: station and partner feeds.

The browser publishes one WHIP stream per path. The YouTube owner's ``program-`` path keeps
its own FFmpeg forward (``mediamtx_bridge.build_forward_command``), unchanged. Each output
here adds paths on the node that holds the source:

    out-<token>   A proxy of the source path, which MediaMTX reads over local RTSP. Its
                  ``runOnReady`` FFmpeg encodes one profile and then either
                  - push: sends it to an RTMP, RTMPS or SRT ingest the station gives us, or
                  - pull: publishes it to ``pull-<token>`` on the same node.
    pull-<token>  What a station reads over SRT. It has its own read passphrase, so each
                  station has its own credential, and deleting the output revokes it.

Sources:
    ``program``  A match's Match Program: the YouTube owner session's path (the composite
                 with overlays).
    ``table``    A Domino101 (Platform) table stream: the clean camera.

Profiles:
    ``broadcast``  1920x1080, 25/30/50/60 fps CFR, H.264 High at 8 Mbit/s CBR, AAC 192k
                   48 kHz stereo, loudness normalised to EBU R128 (-23 LUFS) or ATSC A/85
                   (-24). About 2 CPU cores per output.
    ``youtube``    The owner forward's encode (30 fps baseline 3.5 Mbit/s, AAC 44.1 kHz).

Outputs are kept in Redis for 12 hours per source, like the path routes. A source path
gets a new token on every go-live; ``follow_source`` re-points the outputs, keeping the
``pull-`` name and passphrase, so a station's SRT address does not change. Closing the
source stops the outputs and keeps them, so they come back on the next go-live.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
import shlex
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from domino_stream.api import mediamtx_bridge as bridge

KINDS = ("program", "table")
MODES = ("push", "pull")
PROFILES = ("broadcast", "youtube")
BROADCAST_FPS = (25, 30, 50, 60)
LOUDNESS_TARGETS = (-23, -24)
BROADCAST_VIDEO_BITRATE = "8000k"
BROADCAST_AUDIO_BITRATE = "192k"
MAX_OUTPUTS = 4
STATE_TTL_SECONDS = 12 * 60 * 60
DEFAULT_RTSP_PORT = 8554
DEFAULT_SRT_PORT = 8890
SRT_PACKET_SIZE = "1316"
_TOKEN_BYTES = 16
_PUSH_SCHEMES = ("rtmp", "rtmps", "srt")
_NAME_MAX = 60


class OutputError(ValueError):
	"""A request this module refuses, with a message that is safe to show."""


# --- pure helpers -----------------------------------------------------------------


def new_token() -> str:
	return secrets.token_hex(_TOKEN_BYTES)


def new_passphrase() -> str:
	"""32 URL-safe characters. SRT passphrases must be 10 to 79 characters."""
	return secrets.token_urlsafe(24)


def output_path_name(token: str) -> str:
	safe = re.sub(r"[^0-9a-f]", "", (token or "").lower())
	if len(safe) < 2 * _TOKEN_BYTES:
		raise OutputError("output token is too short")
	return f"out-{safe}"


def pull_path_name(token: str) -> str:
	safe = re.sub(r"[^0-9a-f]", "", (token or "").lower())
	if len(safe) < 2 * _TOKEN_BYTES:
		raise OutputError("output token is too short")
	return f"pull-{safe}"


def clean_name(name: str) -> str:
	return re.sub(r"\s+", " ", (name or "").strip())[:_NAME_MAX]


def parse_port(address: str | None, default: int) -> int:
	"""Port from a MediaMTX listen address (``:8890``, ``127.0.0.1:8554``)."""
	text = (address or "").strip()
	if not text:
		return default
	port = text.rsplit(":", 1)[-1]
	return int(port) if port.isdigit() else default


def _blocked_host(host: str) -> bool:
	"""Loopback, private, link-local and unspecified addresses are not push targets."""
	name = (host or "").strip().strip("[]").lower()
	if not name or name == "localhost" or name.endswith(".localhost"):
		return True
	try:
		ip = ipaddress.ip_address(name)
	except ValueError:
		return False
	return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified or ip.is_multicast


def push_target_url(url: str, stream_key: str = "") -> str:
	"""Validated push address, with the key joined the way FFmpeg expects.

	RTMP / RTMPS: ``<url>/<key>`` (same join as the owner forward).
	SRT: the station's ``srt://host:port?...`` URL. A key with no ``streamid`` in the URL
	becomes the ``streamid``. ``pkt_size=1316`` is added when missing (MPEG-TS over SRT).
	"""
	raw = (url or "").strip()
	parsed = urlparse(raw)
	scheme = (parsed.scheme or "").lower()
	if scheme not in _PUSH_SCHEMES:
		raise OutputError("Use an rtmp://, rtmps:// or srt:// address")
	if not parsed.hostname:
		raise OutputError("The address has no host")
	if _blocked_host(parsed.hostname):
		raise OutputError("That host is not allowed as an output")
	key = (stream_key or "").strip()
	if scheme in ("rtmp", "rtmps"):
		joined = bridge.rtmps_push_url(raw, key) if key else raw.rstrip("/")
		return joined
	if not parsed.port:
		raise OutputError("An SRT address needs a port")
	query = dict(parse_qsl(parsed.query, keep_blank_values=True))
	if key and "streamid" not in query:
		query["streamid"] = key
	query.setdefault("pkt_size", SRT_PACKET_SIZE)
	return urlunparse(parsed._replace(query=urlencode(query, safe=":/")))


def target_label(target: str) -> str:
	"""``scheme://host:port``: what a list shows. Keys and passphrases stay hidden."""
	parsed = urlparse(target or "")
	if not parsed.scheme or not parsed.hostname:
		return ""
	port = f":{parsed.port}" if parsed.port else ""
	return f"{parsed.scheme}://{parsed.hostname}{port}"


def broadcast_video_args(fps: int) -> list[str]:
	"""1080p on a steady clock. Smaller or portrait pictures are letterboxed, not stretched."""
	if fps not in BROADCAST_FPS:
		raise OutputError("Frame rate must be 25, 30, 50 or 60")
	gop = str(fps * 2)
	return [
		"-vf",
		(
			f"fps={fps},"
			"scale=1920:1080:force_original_aspect_ratio=decrease,"
			"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
		),
		"-r",
		str(fps),
		"-c:v",
		"libx264",
		"-preset",
		"veryfast",
		"-tune",
		"zerolatency",
		"-profile:v",
		"high",
		"-level:v",
		"4.2",
		"-bf",
		"0",
		"-g",
		gop,
		"-keyint_min",
		gop,
		"-sc_threshold",
		"0",
		"-b:v",
		BROADCAST_VIDEO_BITRATE,
		"-minrate",
		BROADCAST_VIDEO_BITRATE,
		"-maxrate",
		BROADCAST_VIDEO_BITRATE,
		"-bufsize",
		BROADCAST_VIDEO_BITRATE,
		"-x264-params",
		"nal-hrd=cbr:force-cfr=1",
	]


def broadcast_audio_args(loudness: int) -> list[str]:
	"""AAC 48 kHz stereo at a broadcast loudness target. Always re-encoded (loudnorm)."""
	if loudness not in LOUDNESS_TARGETS:
		raise OutputError("Loudness must be -23 (EBU R128) or -24 (ATSC A/85)")
	return [
		"-af",
		f"aresample=async=1:min_hard_comp=0.100000,loudnorm=I={loudness}:TP=-1.5:LRA=11,aresample=48000",
		"-c:a",
		"aac",
		"-b:a",
		BROADCAST_AUDIO_BITRATE,
		"-ar",
		"48000",
		"-ac",
		"2",
	]


def profile_args(profile: str, fps: int = 30, loudness: int = -23) -> list[str]:
	if profile == "broadcast":
		return broadcast_video_args(fps) + broadcast_audio_args(loudness)
	if profile == "youtube":
		# The source is the browser's WHIP publish, so its audio is Opus.
		return bridge.youtube_video_args() + bridge.forward_audio_args("opus")
	raise OutputError("Profile must be broadcast or youtube")


def target_output_args(target: str) -> list[str]:
	"""Container for the target: FLV for RTMP(S), MPEG-TS for SRT, RTSP into a pull path."""
	scheme = (urlparse(target or "").scheme or "").lower()
	if scheme in ("rtmp", "rtmps"):
		return [
			"-f",
			"flv",
			"-flvflags",
			"no_duration_filesize",
			"-muxdelay",
			"0",
			"-muxpreload",
			"0",
			"-max_interleave_delta",
			"100000",
			target,
		]
	if scheme == "srt":
		return [
			"-f",
			"mpegts",
			"-mpegts_flags",
			"+resend_headers",
			"-max_interleave_delta",
			"100000",
			target,
		]
	if scheme == "rtsp":
		return ["-f", "rtsp", "-rtsp_transport", "tcp", target]
	raise OutputError("Unsupported output target")


def pull_publish_target(pull_path: str) -> str:
	"""Where the output FFmpeg publishes a pull feed. MediaMTX expands ``$RTSP_PORT``."""
	return f"rtsp://127.0.0.1:$RTSP_PORT/{pull_path}"


def build_output_command(ffmpeg_bin: str, profile: str, target: str, fps: int = 30, loudness: int = -23) -> str:
	"""FFmpeg MediaMTX runs on the ``out-`` path: read it, encode the profile, send it on."""
	if not ffmpeg_bin or not target:
		raise OutputError("ffmpeg and a target are required")
	parts = [
		*bridge.forward_input_args(ffmpeg_bin),
		*profile_args(profile, fps, loudness),
		*target_output_args(target),
	]
	return " ".join(shlex.quote(part) for part in parts)


def out_path_config(source_path: str, rtsp_port: int, command: str) -> dict:
	"""Proxy of the source path. FFmpeg starts when the source is ready and restarts on exit."""
	return {
		"source": f"rtsp://127.0.0.1:{int(rtsp_port)}/{source_path}",
		"sourceOnDemand": False,
		"runOnReady": command,
		"runOnReadyRestart": True,
	}


def pull_path_config(passphrase: str) -> dict:
	"""The station's SRT read point. Only the local output FFmpeg can publish (mediamtx.yml)."""
	return {"source": "publisher", "srtReadPassphrase": passphrase}


def pull_read_url(host: str, srt_port: int, pull_path: str, passphrase: str) -> str:
	"""What the station enters in its SRT caller (decoder / IRD / vMix / OBS)."""
	query = urlencode({"streamid": f"read:{pull_path}", "passphrase": passphrase}, safe=":")
	return f"srt://{host}:{int(srt_port)}?{query}"


def new_record(
	mode: str,
	name: str = "",
	target: str = "",
	profile: str = "broadcast",
	fps: int = 30,
	loudness: int = -23,
) -> dict:
	"""A validated output record. Push needs a target from ``push_target_url``."""
	if mode not in MODES:
		raise OutputError("Mode must be push or pull")
	if profile not in PROFILES:
		raise OutputError("Profile must be broadcast or youtube")
	profile_args(profile, fps, loudness)  # validates fps and loudness
	if mode == "push" and not target:
		raise OutputError("A push output needs an address")
	token = new_token()
	record = {
		"id": token[:12],
		"name": clean_name(name),
		"mode": mode,
		"profile": profile,
		"fps": fps,
		"loudness": loudness,
		"target": target if mode == "push" else "",
		"out_path": output_path_name(token),
		"pull_path": "",
		"passphrase": "",
		"source_path": "",
		"node_id": "",
		"error": "",
	}
	if mode == "pull":
		record["pull_path"] = pull_path_name(token)
		record["passphrase"] = new_passphrase()
	return record


def public_view(record: dict, host: str = "", srt_port: int = DEFAULT_SRT_PORT, ready: bool | None = None) -> dict:
	"""What the API returns. Push keys never leave the server; a pull URL does, for the station."""
	view = {
		"id": record.get("id"),
		"name": record.get("name") or "",
		"mode": record.get("mode"),
		"profile": record.get("profile"),
		"fps": record.get("fps"),
		"loudness": record.get("loudness") if record.get("profile") == "broadcast" else None,
		"running": bool(record.get("source_path")),
		"error": record.get("error") or "",
	}
	if ready is not None:
		view["ready"] = bool(ready)
	if record.get("mode") == "push":
		view["target"] = target_label(record.get("target") or "")
	elif host:
		view["srt_url"] = pull_read_url(host, srt_port, record["pull_path"], record["passphrase"])
	return view


# --- state (Redis via frappe.cache) -----------------------------------------------


def _safe_source(kind: str, source_id: str) -> str:
	if kind not in KINDS:
		raise OutputError("Source must be program or table")
	safe = bridge.safe_match_id(source_id)
	if not safe:
		raise OutputError("Source id is required")
	return safe


def state_key(kind: str, source_id: str) -> str:
	return f"domino_stream_outputs:{kind}:{_safe_source(kind, source_id)}"


def owner_key(match_id: str) -> str:
	return f"mediamtx_program_owner:{_safe_source('program', match_id)}"


def _load(kind: str, source_id: str) -> list[dict]:
	try:
		raw = bridge._cache().get_value(state_key(kind, source_id), expires=True)
	except Exception:
		raw = None
	outputs = raw.get("outputs") if isinstance(raw, dict) else None
	return [o for o in outputs if isinstance(o, dict) and o.get("id")] if isinstance(outputs, list) else []


def _save(kind: str, source_id: str, outputs: list[dict]) -> None:
	key = state_key(kind, source_id)
	if outputs:
		bridge._cache().set_value(key, {"outputs": outputs}, expires_in_sec=STATE_TTL_SECONDS)
	else:
		bridge._cache().delete_value(key)


def _owner_path(match_id: str) -> str:
	try:
		raw = bridge._cache().get_value(owner_key(match_id), expires=True)
	except Exception:
		raw = None
	return (raw.get("path_name") or "").strip() if isinstance(raw, dict) else ""


def _set_owner_path(match_id: str, path_name: str) -> None:
	if path_name:
		bridge._cache().set_value(owner_key(match_id), {"path_name": path_name}, expires_in_sec=STATE_TTL_SECONDS)
	else:
		bridge._cache().delete_value(owner_key(match_id))


def current_source(kind: str, source_id: str) -> tuple[str, dict | None] | None:
	"""(path name, node) the outputs read now, or None when the source is not live."""
	_safe_source(kind, source_id)
	path = _owner_path(source_id) if kind == "program" else bridge.cached_table_path_name(source_id)
	if not path:
		return None
	return path, bridge._nodes().path_node(path)


# --- MediaMTX ---------------------------------------------------------------------


def _node_listeners(node: dict | None) -> tuple[int, bool, int]:
	"""(RTSP port, SRT on, SRT port) from the node's running config."""
	response = bridge._request("GET", "/v3/config/global/get", node=node)
	data = {}
	if response is not None and response.ok:
		try:
			data = response.json() or {}
		except ValueError:
			data = {}
	return (
		parse_port(data.get("rtspAddress"), DEFAULT_RTSP_PORT),
		bool(data.get("srt")),
		parse_port(data.get("srtAddress"), DEFAULT_SRT_PORT),
	)


def _put_path(name: str, config: dict, node: dict | None) -> None:
	"""Create (or re-create) one path on the node and route it there."""
	quoted = quote(name, safe="")
	bridge._request("DELETE", f"/v3/config/paths/delete/{quoted}", node=node)
	response = bridge._request("POST", f"/v3/config/paths/add/{quoted}", config, node=node)
	if response is None:
		raise OutputError("The live bridge is not running")
	if not response.ok:
		raise OutputError("The live bridge rejected the output path")
	bridge._nodes().remember_path_node(name, node or {})


def _path_ready(name: str, node: dict | None) -> bool | None:
	if not name:
		return False
	response = bridge._request("GET", f"/v3/paths/get/{quote(name, safe='')}", node=node)
	if response is None:
		return None
	if not response.ok:
		return False
	try:
		return bool((response.json() or {}).get("ready"))
	except ValueError:
		return None


def _open(record: dict, source_path: str, node: dict | None) -> None:
	"""Start one output against ``source_path``. Sets ``source_path`` or ``error``."""
	ffmpeg_bin = bridge.resolve_ffmpeg_bin()
	if not ffmpeg_bin:
		raise OutputError("ffmpeg is not installed")
	rtsp_port, srt_on, _srt_port = _node_listeners(node)
	if record["mode"] == "pull":
		if not srt_on:
			raise OutputError("SRT is off on this media node (srt: true in mediamtx.yml)")
		_put_path(record["pull_path"], pull_path_config(record["passphrase"]), node)
		target = pull_publish_target(record["pull_path"])
	else:
		target = record["target"]
	command = build_output_command(
		ffmpeg_bin,
		record["profile"],
		target,
		fps=int(record.get("fps") or 30),
		loudness=int(record.get("loudness") or -23),
	)
	_put_path(record["out_path"], out_path_config(source_path, rtsp_port, command), node)
	record["source_path"] = source_path
	record["node_id"] = (node or {}).get("id") or "local"
	record["error"] = ""


def _close(record: dict) -> None:
	"""Stop one output: the FFmpeg path first, then the station's pull path."""
	for name in (record.get("out_path"), record.get("pull_path")):
		if name:
			bridge._delete_path(name)
	record["source_path"] = ""
	record["node_id"] = ""


def _start(record: dict, source_path: str, node: dict | None) -> None:
	try:
		_close(record)
		_open(record, source_path, node)
	except OutputError as exc:
		_close(record)
		record["error"] = str(exc)
	except Exception as exc:
		_close(record)
		record["error"] = bridge.scrub_rtmp_urls(str(exc))[:200] or "Output failed to start"


# --- called by mediamtx_bridge ----------------------------------------------------


def follow_source(kind: str, source_id: str, path_name: str, node: dict | None) -> None:
	"""The source has a new path (go-live). Re-point every output at it."""
	if kind == "program":
		_set_owner_path(source_id, path_name)
	outputs = _load(kind, source_id)
	if not outputs:
		return
	for record in outputs:
		_start(record, path_name, node)
	_save(kind, source_id, outputs)


def stop_if_source(kind: str, source_id: str, closed_paths: list[str]) -> None:
	"""The source path was closed. Stop the outputs and keep them for the next go-live."""
	if kind == "program":
		owner = _owner_path(source_id)
		if not owner or owner not in closed_paths:
			return
		_set_owner_path(source_id, "")
	outputs = _load(kind, source_id)
	if not outputs:
		return
	for record in outputs:
		_close(record)
	_save(kind, source_id, outputs)


# --- API (wrapped by room.py) -----------------------------------------------------


def _public_host(node: dict | None, public_origin: str = "") -> str:
	host = (node or {}).get("public_host") or urlparse(public_origin or "").hostname or ""
	if not host:
		try:
			from frappe.utils import get_url

			host = urlparse(get_url()).hostname or ""
		except Exception:
			host = ""
	return host


def _views(kind: str, source_id: str, outputs: list[dict], public_origin: str = "", with_status: bool = False) -> list[dict]:
	views = []
	ports: dict[str, tuple[int, bool, int]] = {}
	for record in outputs:
		node = bridge._nodes().get_node(record.get("node_id")) if record.get("node_id") else None
		if node is None:
			source = current_source(kind, source_id)
			node = source[1] if source else None
		node_id = (node or {}).get("id") or "local"
		if node_id not in ports:
			ports[node_id] = _node_listeners(node)
		ready = None
		if with_status:
			ready = _path_ready(record.get("out_path") or "", node) if record.get("source_path") else False
		views.append(public_view(record, _public_host(node, public_origin), ports[node_id][2], ready=ready))
	return views


def add_output(
	kind: str,
	source_id: str,
	mode: str,
	name: str = "",
	url: str = "",
	stream_key: str = "",
	profile: str = "broadcast",
	fps: int = 30,
	loudness: int = -23,
	public_origin: str = "",
) -> dict:
	"""Add an output. It starts now if the source is live, otherwise on the next go-live."""
	_safe_source(kind, source_id)
	outputs = _load(kind, source_id)
	if len(outputs) >= MAX_OUTPUTS:
		raise OutputError(f"A source can have at most {MAX_OUTPUTS} extra outputs")
	target = push_target_url(url, stream_key) if mode == "push" else ""
	record = new_record(mode, name=name, target=target, profile=profile, fps=int(fps), loudness=int(loudness))
	source = current_source(kind, source_id)
	if source:
		_start(record, source[0], source[1])
	outputs.append(record)
	_save(kind, source_id, outputs)
	return {"success": True, "output": _views(kind, source_id, [record], public_origin)[0]}


def remove_output(kind: str, source_id: str, output_id: str) -> dict:
	outputs = _load(kind, source_id)
	keep = []
	removed = False
	for record in outputs:
		if record.get("id") == (output_id or "").strip():
			_close(record)
			removed = True
		else:
			keep.append(record)
	_save(kind, source_id, keep)
	return {"success": True, "removed": removed}


def list_outputs(kind: str, source_id: str, public_origin: str = "") -> dict:
	outputs = _load(kind, source_id)
	return {
		"success": True,
		"source_live": current_source(kind, source_id) is not None,
		"outputs": _views(kind, source_id, outputs, public_origin, with_status=True),
	}
