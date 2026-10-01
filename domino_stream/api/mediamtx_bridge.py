"""Per-match MediaMTX path: browser WHIP in, RTMPS to a Cloudflare live input.

The browser publishes constrained-baseline H.264. MediaMTX remuxes that to
RTSP, and FFmpeg copies the video and transcodes Opus to AAC.
"""

from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
from urllib.parse import quote, urlparse

import requests

API_BASE = "http://127.0.0.1:9997"
WHIP_UPSTREAM = "http://127.0.0.1:8889"
_WHIP_PROXY_RE = re.compile(r"^/mediamtx/(program-[A-Za-z0-9-]+)(/whip(?:/.*)?)?$")
_PATH_TOKEN_BYTES = 4
_REQUEST_TIMEOUT = 10
_RTMP_URL_RE = re.compile(r"rtmps?://\S+", re.IGNORECASE)


def safe_match_id(match_id: str) -> str:
	return re.sub(r"[^A-Za-z0-9]", "", match_id or "")


def program_path_name(match_id: str, token: str) -> str:
	"""Path MediaMTX will publish. The token keeps the WHIP URL unguessable."""
	safe = safe_match_id(match_id)
	safe_token = re.sub(r"[^A-Za-z0-9]", "", token or "")
	if not safe or not safe_token:
		raise ValueError("match id and token are required")
	return f"program-{safe}-{safe_token}"


def program_path_prefix(match_id: str) -> str:
	safe = safe_match_id(match_id)
	if not safe:
		raise ValueError("match id is required")
	return f"program-{safe}-"


def rtmps_push_url(rtmps_url: str, stream_key: str) -> str:
	"""Join the live-input RTMPS address and key the way FFmpeg expects."""
	base = (rtmps_url or "").strip().rstrip("/")
	key = (stream_key or "").strip().strip("/")
	if not base or not key:
		return ""
	if base.endswith("/" + key):
		return base
	return f"{base}/{key}"


def audio_needs_aac(codec: str | None) -> bool:
	"""Opus (and an unknown codec) must be transcoded. AAC can be copied."""
	name = (codec or "").strip().lower()
	return name in ("", "opus")


def forward_audio_args(codec: str | None = "opus") -> list[str]:
	if audio_needs_aac(codec):
		return ["-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
	return ["-c:a", "copy"]


def forward_wrapper_path() -> str:
	"""Shell wrapper MediaMTX launches so a signaled ffmpeg still has a wait status."""
	return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mediamtx-forward.sh"))


def resolve_ffmpeg_bin() -> str | None:
	"""Absolute ffmpeg. The MediaMTX process does not inherit a login PATH."""
	override = (os.environ.get("FFMPEG_BIN") or "").strip()
	bench_bin = os.path.abspath(
		os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "bin", "ffmpeg")
	)
	candidates = [override, shutil.which("ffmpeg") or "", bench_bin, "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
	for candidate in candidates:
		if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
			return candidate
	return None


def build_forward_command(push_url: str, ffmpeg_bin: str, audio_codec: str | None = "opus") -> str:
	"""FFmpeg argv MediaMTX runs when the WHIP publisher is readable.

	``$RTSP_PORT`` and ``$MTX_PATH`` are expanded by MediaMTX, not by a shell.
	The first token is the wrapper, which logs FFmpeg's wait status. Video is
	copied. Audio is AAC when the publisher codec is Opus.
	"""
	if not push_url or not ffmpeg_bin:
		raise ValueError("push url and ffmpeg are required")
	parts = [
		forward_wrapper_path(),
		ffmpeg_bin,
		"-hide_banner",
		"-loglevel",
		"warning",
		"-fflags",
		"+discardcorrupt",
		"-rtsp_transport",
		"tcp",
		"-i",
		"rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH",
		"-c:v",
		"copy",
		*forward_audio_args(audio_codec),
		"-f",
		"flv",
		"-flvflags",
		"no_duration_filesize",
		push_url,
	]
	return " ".join(shlex.quote(part) for part in parts)


def whip_public_url(origin: str, path_name: str) -> str:
	base = (origin or "").strip().rstrip("/")
	name = (path_name or "").strip().strip("/")
	if not base or not name:
		raise ValueError("origin and path are required")
	return f"{base}/mediamtx/{name}/whip"


def mediamtx_upstream_path(request_path: str) -> str | None:
	"""Public /mediamtx/<path>/whip path, or None when the request must not be proxied."""
	path = (request_path or "").split("?", 1)[0]
	if not path or ".." in path:
		return None
	match = _WHIP_PROXY_RE.match(path)
	if not match or not match.group(2):
		return None
	return f"/{match.group(1)}{match.group(2)}"


def scrub_rtmp_urls(text: str) -> str:
	return _RTMP_URL_RE.sub("rtmps://[redacted]", text or "")


def _cache_key(match_id: str) -> str:
	return f"mediamtx_program:{safe_match_id(match_id)}"


def _cache():
	import frappe

	return frappe.cache()


def _request(method: str, path: str, payload: dict | None = None):
	"""Call the local MediaMTX control API. None when the process is down."""
	try:
		return requests.request(
			method,
			f"{API_BASE}{path}",
			json=payload,
			timeout=_REQUEST_TIMEOUT,
		)
	except requests.RequestException:
		return None


def _throw_bridge_down():
	import frappe
	from frappe import _

	frappe.throw(_("The live bridge is not running."))


def _throw_rejected():
	import frappe
	from frappe import _

	frappe.throw(_("The live bridge rejected the stream path."))


def _path_names_for_match(match_id: str) -> list[str]:
	prefix = program_path_prefix(match_id)
	response = _request("GET", "/v3/config/paths/list?itemsPerPage=100")
	if response is None or not response.ok:
		return []
	try:
		data = response.json()
	except Exception:
		return []
	items = data.get("items") if isinstance(data, dict) else None
	if not isinstance(items, list):
		return []
	names = []
	for item in items:
		if not isinstance(item, dict):
			continue
		name = (item.get("name") or "").strip()
		if name.startswith(prefix):
			names.append(name)
	return names


def _delete_path(path_name: str) -> None:
	if not path_name:
		return
	_request("DELETE", f"/v3/config/paths/delete/{quote(path_name, safe='')}")


def _store_path(match_id: str, path_name: str) -> None:
	_cache().set_value(_cache_key(match_id), {"path_name": path_name}, expires_in_sec=12 * 60 * 60)


def _drop_path(match_id: str) -> None:
	try:
		_cache().delete_value(_cache_key(match_id))
	except Exception:
		pass


def _advertise_origin(origin: str) -> None:
	host = (urlparse(origin or "").hostname or "").strip()
	if not host or host in ("localhost", "127.0.0.1", "::1"):
		return
	_request("PATCH", "/v3/config/global/patch", {"webrtcAdditionalHosts": [host]})


def open_program_path(match_id: str, rtmps_url: str, stream_key: str, public_origin: str) -> dict:
	"""Create the WHIP path that pushes this match to the live input."""
	import frappe
	from frappe import _

	if not safe_match_id(match_id):
		frappe.throw(_("Match is not loaded"))
	ffmpeg_bin = resolve_ffmpeg_bin()
	wrapper = forward_wrapper_path()
	if not ffmpeg_bin:
		frappe.throw(_("ffmpeg is not installed"))
	if not os.access(wrapper, os.X_OK):
		frappe.throw(_("The live forward is not installed."))
	push_url = rtmps_push_url(rtmps_url, stream_key)
	if not push_url.startswith("rtmp"):
		frappe.throw(_("Live ingest address is missing"))

	close_program_path(match_id)
	path_name = program_path_name(match_id, secrets.token_hex(_PATH_TOKEN_BYTES))
	command = build_forward_command(push_url, ffmpeg_bin, audio_codec="opus")
	response = _request(
		"POST",
		f"/v3/config/paths/add/{quote(path_name, safe='')}",
		{
			"source": "publisher",
			"runOnAvailable": command,
			"runOnAvailableRestart": True,
		},
	)
	if response is None:
		_throw_bridge_down()
	if not response.ok:
		_delete_path(path_name)
		_throw_rejected()

	_store_path(match_id, path_name)
	_advertise_origin(public_origin)
	return {
		"success": True,
		"match_id": match_id,
		"path_name": path_name,
		"whip_url": whip_public_url(public_origin, path_name),
	}


def proxy_whip_request() -> None:
	"""Forward browser WHIP to local MediaMTX. No-op for every other request.

	Registered as a before_request hook so /mediamtx/ works on the existing
	site proxy. Nginx can also send /mediamtx/ straight to MediaMTX.
	"""
	import frappe
	from werkzeug.exceptions import HTTPException
	from werkzeug.wrappers import Response

	request = getattr(frappe, "request", None)
	if request is None:
		return
	upstream_path = mediamtx_upstream_path(getattr(request, "path", "") or "")
	if not upstream_path:
		return
	query = request.query_string.decode() if request.query_string else ""
	url = f"{WHIP_UPSTREAM}{upstream_path}"
	if query:
		url = f"{url}?{query}"
	headers = {}
	content_type = request.headers.get("Content-Type")
	if content_type:
		headers["Content-Type"] = content_type
	try:
		upstream = requests.request(
			request.method,
			url,
			data=request.get_data() or None,
			headers=headers,
			timeout=20,
		)
	except requests.RequestException:
		response = Response("The live bridge is not running.", status=502, mimetype="text/plain")
	else:
		response_headers = {}
		if upstream.headers.get("Content-Type"):
			response_headers["Content-Type"] = upstream.headers["Content-Type"]
		location = upstream.headers.get("Location")
		if location:
			response_headers["Location"] = _public_whip_location(location, request)
		response = Response(upstream.content, status=upstream.status_code, headers=response_headers)
	exc = HTTPException()
	exc.response = response
	raise exc


def _public_whip_location(location: str, request) -> str:
	"""Put MediaMTX's session URL back on the public /mediamtx/ prefix."""
	origin = f"{request.scheme}://{request.host}".rstrip("/")
	try:
		parsed = urlparse(location)
	except Exception:
		return location
	path = parsed.path or ""
	if path.startswith("/mediamtx/"):
		return f"{origin}{path}"
	if not path.startswith("/"):
		path = f"/{path}"
	public = f"{origin}/mediamtx{path}"
	if parsed.query:
		public = f"{public}?{parsed.query}"
	return public


def close_program_path(match_id: str) -> dict:
	"""Remove every MediaMTX path for this match. Safe when none exist."""
	if not safe_match_id(match_id):
		return {"success": True, "closed": False}
	names = []
	try:
		raw = _cache().get_value(_cache_key(match_id), expires=True)
	except Exception:
		raw = None
	if isinstance(raw, dict) and raw.get("path_name"):
		names.append(raw["path_name"])
	try:
		names.extend(_path_names_for_match(match_id))
	except Exception:
		pass
	seen = []
	for name in names:
		if name and name not in seen:
			seen.append(name)
			_delete_path(name)
	_drop_path(match_id)
	return {"success": True, "closed": bool(seen), "match_id": match_id}
