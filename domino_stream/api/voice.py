"""Audio-only multi-publisher rooms for ODP network boards.

State lives in cache. This module does not use Stream Room, room.py, or the match SFU client.
"""

from __future__ import annotations

import hashlib

import frappe
from frappe import _

from domino_stream.api.auth import get_inbound_api_key
from domino_stream.api import voice_sfu

logger = frappe.logger("odp_voice", allow_site=True, file_count=10)

CACHE_TTL = 8 * 60 * 60


def _cache_key(board: str) -> str:
	return f"odp_voice_room:{board}"


def _load(board: str) -> dict:
	data = frappe.cache().get_value(_cache_key(board))
	if not isinstance(data, dict):
		data = {}
	data.setdefault("publishers", {})
	data.setdefault("sessions", {})
	return data


def _save(board: str, data: dict) -> None:
	if not data.get("publishers") and not data.get("sessions"):
		frappe.cache().delete_value(_cache_key(board))
		return
	frappe.cache().set_value(_cache_key(board), data, expires_in_sec=CACHE_TTL)


def _track_name(board: str, participant: str) -> str:
	digest = hashlib.sha1(f"{board}:{participant}".encode()).hexdigest()[:16]
	return f"odp-audio-{digest}"


def _header_key() -> str:
	provided = (frappe.get_request_header("X-Domino-Stream-Key") or "").strip()
	auth = (frappe.get_request_header("Authorization") or "").strip()
	if auth.lower().startswith("bearer "):
		provided = provided or auth[7:].strip()
	return provided


def _actor(actor: str | None = None) -> str:
	user = ""
	if frappe.session:
		user = frappe.session.user or ""
	if user and user != "Guest":
		return user
	expected = get_inbound_api_key()
	provided = _header_key()
	if expected and provided and provided == expected:
		actor = (actor or "").strip()
		if not actor or actor == "Guest":
			frappe.throw(_("Voice user is required"), frappe.PermissionError)
		return actor
	frappe.throw(_("Login required"), frappe.PermissionError)


def _session_description(data: dict | None) -> dict | None:
	if not isinstance(data, dict):
		return None
	desc = data.get("sessionDescription")
	if not isinstance(desc, dict) or not desc.get("sdp"):
		return None
	return {"type": desc.get("type") or "answer", "sdp": desc.get("sdp")}


def _owns(session: dict, actor: str) -> None:
	if not session or session.get("user") != actor:
		frappe.throw(_("Voice session not found"))


def _close_quietly(session_id: str, tracks: list) -> None:
	try:
		voice_sfu.close_tracks(session_id, tracks=tracks, force=True)
	except Exception:
		logger.exception("ODP voice close failed for session %s", session_id)


def _drop_session(data: dict, session_id: str) -> dict | None:
	session = (data.get("sessions") or {}).pop(session_id, None)
	if not session:
		return None
	participant = session.get("participant")
	publishers = data.setdefault("publishers", {})
	current = publishers.get(participant) or {}
	if current.get("session_id") == session_id:
		publishers.pop(participant, None)
	return session


@frappe.whitelist(allow_guest=True)
def prepare(
	board: str,
	participant: str,
	role: str,
	sdp: str,
	sdp_type: str = "offer",
	actor: str = None,
):
	"""Open a voice session. role is publisher or listener."""
	user = _actor(actor)
	board = (board or "").strip()
	participant = (participant or "").strip()
	role = (role or "").strip().lower()
	if not board or not participant:
		frappe.throw(_("Board and participant are required"))
	if role not in ("publisher", "listener"):
		frappe.throw(_("Voice role is invalid"))
	if not sdp:
		frappe.throw(_("A voice offer is required"))

	data = _load(board)
	for session_id, session in list((data.get("sessions") or {}).items()):
		if session.get("participant") == participant and session.get("user") == user:
			tracks = []
			if session.get("track_name"):
				tracks.append({"trackName": session["track_name"]})
			_close_quietly(session_id, tracks)
			_drop_session(data, session_id)
	_save(board, data)

	created = voice_sfu.create_session({"type": sdp_type or "offer", "sdp": sdp})
	session_id = created.get("sessionId")
	if not session_id:
		frappe.throw(_("Voice service did not open a session"))

	track_name = _track_name(board, participant) if role == "publisher" else ""
	data.setdefault("sessions", {})[session_id] = {
		"participant": participant,
		"user": user,
		"role": role,
		"track_name": track_name,
		"pulled": [],
	}
	_save(board, data)

	return {
		"session_id": session_id,
		"sessionDescription": _session_description(created),
		"iceServers": [{"urls": "stun:stun.cloudflare.com:3478"}],
		"role": role,
		"participant": participant,
		"track_name": track_name or None,
	}


@frappe.whitelist(allow_guest=True)
def publish(
	board: str,
	participant: str,
	session_id: str,
	sdp: str,
	tracks: list | str = None,
	actor: str = None,
):
	"""Attach one local audio track to a publisher session."""
	user = _actor(actor)
	board = (board or "").strip()
	session_id = (session_id or "").strip()
	if not board or not session_id or not sdp:
		frappe.throw(_("Voice publish is missing a session"))

	data = _load(board)
	session = (data.get("sessions") or {}).get(session_id)
	_owns(session, user)
	if session.get("role") != "publisher":
		frappe.throw(_("Only a seated player can publish voice"))
	if session.get("participant") != (participant or "").strip():
		frappe.throw(_("Voice session not found"))

	client_tracks = tracks
	if isinstance(client_tracks, str):
		client_tracks = frappe.parse_json(client_tracks)
	if not isinstance(client_tracks, list) or len(client_tracks) != 1:
		frappe.throw(_("Voice publish needs one audio track"))
	mid = (client_tracks[0] or {}).get("mid")
	kind = ((client_tracks[0] or {}).get("kind") or "audio").lower()
	if not mid or kind != "audio":
		frappe.throw(_("Voice publish needs one audio track"))

	track_name = session.get("track_name") or _track_name(board, session["participant"])
	local_track = {
		"location": "local",
		"mid": str(mid),
		"trackName": track_name,
		"kind": "audio",
	}
	added = voice_sfu.add_tracks(
		session_id,
		[local_track],
		{"type": "offer", "sdp": sdp},
	)
	session["track_name"] = track_name
	data.setdefault("publishers", {})[session["participant"]] = {
		"session_id": session_id,
		"track_name": track_name,
		"user": user,
		"muted": False,
	}
	_save(board, data)
	return {
		"session_id": session_id,
		"participant": session["participant"],
		"track_name": track_name,
		"sessionDescription": _session_description(added),
		"tracks": added.get("tracks") or [],
	}


@frappe.whitelist(allow_guest=True)
def pull(board: str, session_id: str, actor: str = None):
	"""Pull other publishers' audio onto this session."""
	user = _actor(actor)
	board = (board or "").strip()
	session_id = (session_id or "").strip()
	if not board or not session_id:
		frappe.throw(_("Voice listen is missing a session"))

	data = _load(board)
	session = (data.get("sessions") or {}).get(session_id)
	_owns(session, user)

	already = {item.get("trackName") for item in (session.get("pulled") or [])}
	remote_tracks = []
	cf_tracks = []
	for participant, publisher in (data.get("publishers") or {}).items():
		if participant == session.get("participant"):
			continue
		track_name = publisher.get("track_name")
		pub_session = publisher.get("session_id")
		if not track_name or not pub_session or track_name in already:
			continue
		remote_tracks.append(
			{
				"participant": participant,
				"sessionId": pub_session,
				"trackName": track_name,
				"muted": bool(publisher.get("muted")),
			}
		)
		cf_tracks.append(
			{
				"location": "remote",
				"sessionId": pub_session,
				"trackName": track_name,
			}
		)

	if not cf_tracks:
		return {
			"session_id": session_id,
			"sessionDescription": None,
			"requiresImmediateRenegotiation": False,
			"tracks": [],
			"remote_tracks": [],
		}

	pulled = voice_sfu.add_tracks(session_id, cf_tracks)
	description = _session_description(pulled)
	requires = pulled.get("requiresImmediateRenegotiation")
	if requires is None and description and description.get("type") == "offer":
		requires = True

	response_tracks = [track for track in (pulled.get("tracks") or []) if isinstance(track, dict)]
	errored = {track.get("trackName") for track in response_tracks if track.get("errorCode")}
	mids = {
		track.get("trackName"): track.get("mid")
		for track in response_tracks
		if track.get("trackName") and not track.get("errorCode")
	}
	for item in remote_tracks:
		if item["trackName"] in errored:
			continue
		session.setdefault("pulled", []).append(
			{
				"trackName": item["trackName"],
				"participant": item["participant"],
				"mid": mids.get(item["trackName"]),
			}
		)
	_save(board, data)
	return {
		"session_id": session_id,
		"sessionDescription": description,
		"requiresImmediateRenegotiation": bool(requires),
		"tracks": pulled.get("tracks") or [],
		"remote_tracks": remote_tracks,
	}


@frappe.whitelist(allow_guest=True)
def renegotiate(board: str, session_id: str, sdp: str, sdp_type: str = "answer", actor: str = None):
	"""Answer an SFU offer after pull."""
	user = _actor(actor)
	board = (board or "").strip()
	session_id = (session_id or "").strip()
	if not board or not session_id or not sdp:
		frappe.throw(_("Voice renegotiate is missing a session"))
	data = _load(board)
	session = (data.get("sessions") or {}).get(session_id)
	_owns(session, user)
	voice_sfu.renegotiate(session_id, {"type": sdp_type or "answer", "sdp": sdp})
	return {"success": True, "session_id": session_id}


@frappe.whitelist(allow_guest=True)
def set_muted(board: str, participant: str, muted: int | str | bool = 1, actor: str = None):
	"""Remember mute for late listeners. Does not close the session."""
	user = _actor(actor)
	board = (board or "").strip()
	participant = (participant or "").strip()
	data = _load(board)
	publisher = (data.get("publishers") or {}).get(participant)
	if not publisher or publisher.get("user") != user:
		return {"success": True, "muted": False}
	flag = str(muted).lower() not in ("0", "false", "none", "")
	if muted is False:
		flag = False
	publisher["muted"] = bool(flag)
	_save(board, data)
	return {"success": True, "participant": participant, "muted": publisher["muted"]}


@frappe.whitelist(allow_guest=True)
def leave(board: str, participant: str = None, session_id: str = None, actor: str = None):
	"""Close this participant's voice session only."""
	user = _actor(actor)
	board = (board or "").strip()
	session_id = (session_id or "").strip()
	participant = (participant or "").strip()
	if not board:
		frappe.throw(_("Board is required"))

	data = _load(board)
	targets = []
	if session_id and session_id in (data.get("sessions") or {}):
		targets.append(session_id)
	elif participant:
		for sid, session in (data.get("sessions") or {}).items():
			if session.get("participant") == participant and session.get("user") == user:
				targets.append(sid)

	closed = []
	for sid in targets:
		session = data["sessions"].get(sid) or {}
		if session.get("user") != user:
			continue
		tracks = []
		if session.get("track_name"):
			tracks.append({"trackName": session["track_name"]})
		for item in session.get("pulled") or []:
			entry = {"trackName": item.get("trackName")}
			if item.get("mid"):
				entry["mid"] = str(item["mid"])
			if entry.get("trackName"):
				tracks.append(entry)
		_close_quietly(sid, tracks)
		_drop_session(data, sid)
		closed.append(sid)
	_save(board, data)
	return {"success": True, "closed": closed}
