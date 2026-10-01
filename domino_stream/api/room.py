"""Room APIs: prepare publish/play, register tracks, stop, publisher heartbeat.

MVP ingest→deliver over Cloudflare Realtime SFU with passthrough pipeline.
"""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime, time_diff_in_seconds

from domino_stream.api.auth import require_stream_access
from domino_stream.api import live_now, pipeline, sfu_client
from domino_stream.api.session_health import (
	kill_reason_for_observation,
	observation_action,
	observe_session,
	session_verdict_label,
)
from domino_stream.api.realtime_pub import (
	issue_presence_token,
	presence_socket_client_info,
)

DEFAULT_HEARTBEAT_GRACE_SECONDS = 35
DEFAULT_KILL_CHALLENGE_GRACE_SECONDS = 20

stop_logger = frappe.logger("domino_stream_stop", allow_site=True, file_count=20)


def _get_or_create_room(table_id: str):
	name = frappe.db.get_value("Stream Room", {"table_id": table_id}, "name")
	if name:
		return frappe.get_doc("Stream Room", name)
	doc = frappe.get_doc(
		{
			"doctype": "Stream Room",
			"table_id": table_id,
			"status": "Idle",
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


def _upsert_participant(room_name: str, role: str, session_id: str, user: str | None = None):
	existing = frappe.db.get_value(
		"Stream Participant",
		{"room": room_name, "session_id": session_id},
		"name",
	)
	if existing:
		frappe.db.set_value("Stream Participant", existing, {"status": "Active", "role": role})
		return existing
	doc = frappe.get_doc(
		{
			"doctype": "Stream Participant",
			"room": room_name,
			"role": role,
			"session_id": session_id,
			"user": user or frappe.session.user,
			"status": "Active",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _settings_int(fieldname: str, default: int) -> int:
	"""Saved Stream Settings value. Empty falls back to ``default``. No code floor."""
	try:
		settings = frappe.get_single("Stream Settings")
		raw = settings.get(fieldname)
		if raw is None or raw == "":
			return default
		return cint(raw)
	except Exception:
		return default


def get_heartbeat_grace_seconds() -> int:
	"""Seconds without a publisher heartbeat before it counts as stale."""
	return _settings_int("publisher_heartbeat_grace_seconds", DEFAULT_HEARTBEAT_GRACE_SECONDS)


def get_kill_challenge_grace_seconds() -> int:
	"""Seconds to wait for keep_alive after a kill challenge opens."""
	return _settings_int("kill_challenge_grace_seconds", DEFAULT_KILL_CHALLENGE_GRACE_SECONDS)


def publisher_room_id(table_id: str) -> str:
	from domino_stream.api.realtime_pub import publisher_room_id as _room_id

	return _room_id(table_id)


def _clear_kill_challenge(room):
	room.kill_challenge_id = None
	room.kill_challenge_expires_at = None
	room.kill_challenge_reason = None


def _heartbeat_age_seconds(room) -> float | None:
	hb = room.get("last_publisher_heartbeat") if isinstance(room, dict) else room.last_publisher_heartbeat
	if not hb:
		return None
	return float(time_diff_in_seconds(now_datetime(), get_datetime(hb)))


def publisher_heartbeat_fresh(room, grace: int | None = None) -> bool:
	"""True when last_publisher_heartbeat is within the grace window."""
	grace = grace if grace is not None else get_heartbeat_grace_seconds()
	age = _heartbeat_age_seconds(room)
	if age is None:
		return False
	return age <= grace


def _touch_publisher_heartbeat(room, *, via_socket: bool = False):
	room.last_publisher_heartbeat = now_datetime()
	room.tracks_inactive_since = None
	if via_socket:
		room.publisher_socket_present = 1
	if frappe.session.user and frappe.session.user != "Guest":
		room.publisher_user = frappe.session.user


def _infer_stop_caller() -> str:
	"""Best-effort caller fingerprint for stop attribution."""
	try:
		form = getattr(frappe.local, "form_dict", None) or {}
		cmd = form.get("cmd") or ""
		path = ""
		req = getattr(frappe.local, "request", None)
		if req is not None:
			path = getattr(req, "path", "") or ""
		user = ""
		if frappe.session:
			user = frappe.session.user or ""
		parts = []
		if user:
			parts.append(f"user={user}")
		if cmd:
			parts.append(f"cmd={cmd}")
		elif path:
			parts.append(f"path={path}")
		return " ".join(parts) if parts else "unknown"
	except Exception:
		return "unknown"


def _build_close_track_entries(room) -> list:
	"""Build Cloudflare tracks/close payloads (trackName + mid when known)."""
	close_list = []
	if room.video_track_name:
		entry = {"trackName": room.video_track_name}
		mid = getattr(room, "video_mid", None)
		if mid not in (None, ""):
			entry["mid"] = str(mid)
		close_list.append(entry)
	if room.audio_track_name:
		entry = {"trackName": room.audio_track_name}
		mid = getattr(room, "audio_mid", None)
		if mid not in (None, ""):
			entry["mid"] = str(mid)
		close_list.append(entry)
	return close_list


def _stop_room_internal(room, *, reason: str = ""):
	"""Close SFU tracks, mark Stopped, notify www. Caller owns permissions.

	Always writes a non-blank ``last_error`` with reason, heartbeat age, SFU
	verdict, and caller so kills are attributable (never anonymous empty string).
	"""
	table_id = room.table_id
	hb_age = _heartbeat_age_seconds(room)
	session_verdict = session_verdict_label(getattr(room, "publisher_session_id", None))
	caller = _infer_stop_caller()
	base_reason = (reason or "").strip() or "client_stop"
	attribution = (
		f"{base_reason} | hb_age_s={hb_age} | session={session_verdict} | {caller}"
	)

	close_error = None
	if room.publisher_session_id:
		close_list = _build_close_track_entries(room)
		try:
			sfu_client.close_tracks(room.publisher_session_id, tracks=close_list, force=True)
		except Exception as e:
			close_error = str(e)

	for p in frappe.get_all(
		"Stream Participant", filters={"room": room.name, "status": "Active"}, pluck="name"
	):
		frappe.db.set_value("Stream Participant", p, "status", "Closed")

	room.status = "Stopped"
	room.www_notified_live = 0
	room.tracks_inactive_since = None
	room.publisher_socket_present = 0
	_clear_kill_challenge(room)
	if close_error:
		room.last_error = f"{attribution} | close_error={close_error}"
	else:
		room.last_error = attribution
	# Small Text — keep readable
	if len(room.last_error) > 1400:
		room.last_error = room.last_error[:1400]
	room.save(ignore_permissions=True)
	frappe.db.commit()

	stop_logger.info("Stop table %s: %s", table_id, room.last_error)

	try:
		from domino_stream.api.events import emit_stream_event

		emit_stream_event(
			"stop",
			room.last_error or f"Stopped {table_id}",
			table_id=table_id,
			room=room.name,
			severity="Warning" if close_error else "Info",
			detail={
				"attribution": attribution,
				"close_error": close_error,
				"session_verdict": session_verdict,
				"hb_age_s": hb_age,
			},
		)
	except Exception:
		pass

	notify = live_now.notify_live(table_id, False)
	live_now.post_www_webhook(
		table_id,
		live=False,
		stream_name=f"Domino Match Table {table_id}",
	)
	return notify


def _guard_healthy_live_publisher(room):
	"""Reject prepare_publish when another publisher is still healthy."""
	if room.status != "Live" or not room.publisher_session_id:
		return
	if publisher_heartbeat_fresh(room):
		frappe.throw(
			_(
				"Domino Stream is already live for this table "
				"(publisher session {0}). Stop the current stream or wait for liveness grace."
			).format(room.publisher_session_id),
			frappe.ValidationError,
		)
	# Stale Live room — clear before new prepare so we do not orphan the pointer
	_stop_room_internal(room, reason="Replaced stale publisher on prepare_publish")


@frappe.whitelist(allow_guest=True)
def health():
	"""Public-ish health: SFU credentials present (does not create sessions)."""
	require_stream_access()
	creds = sfu_client.get_sfu_credentials()
	return {
		"ready": creds["configured"],
		"platform": "Domino Stream",
		"sfu": "cloudflare_realtime",
		"message": "OK" if creds["configured"] else "Configure Stream Settings",
	}


@frappe.whitelist(allow_guest=True)
def prepare_publish(table_id: str, sdp: str = None, sdp_type: str = "offer"):
	"""
	Create an SFU session with the publisher's first offer SDP (Cloudflare two-step).

	Client: createOffer → prepare_publish(sdp) → setRemote(answer) → wait ICE →
	then publish_tracks with a second offer + local track mid/trackName.
	"""
	require_stream_access()
	if not table_id:
		frappe.throw(_("table_id is required"))
	if not sdp:
		frappe.throw(_("sdp is required for prepare_publish (Cloudflare sessions/new offer)"))

	room = _get_or_create_room(table_id)
	_guard_healthy_live_publisher(room)
	# Reload after possible stale stop
	room = _get_or_create_room(table_id)

	room.status = "Preparing"
	room.publisher_user = frappe.session.user if frappe.session.user != "Guest" else room.publisher_user
	room.save(ignore_permissions=True)

	session = sfu_client.create_session({"type": sdp_type or "offer", "sdp": sdp})
	session_id = session.get("sessionId")
	if not session_id:
		frappe.throw(_("SFU did not return sessionId"))

	room.publisher_session_id = session_id
	room.last_publisher_heartbeat = None
	room.tracks_inactive_since = None
	room.video_mid = None
	room.audio_mid = None
	room.save(ignore_permissions=True)
	_upsert_participant(room.name, "publisher", session_id)
	frappe.db.commit()

	pipeline.ensure_default_passthrough_stage()

	presence_token = issue_presence_token(table_id, session_id)
	presence_info = presence_socket_client_info()
	presence_info["token"] = presence_token
	presence_info["host"] = frappe.utils.get_url().rstrip("/")

	return {
		"ready": True,
		"platform": "Domino Stream",
		"table_id": table_id,
		"room": room.name,
		"session_id": session_id,
		"sessionDescription": session.get("sessionDescription"),
		"iceServers": [{"urls": "stun:stun.cloudflare.com:3478"}],
		"presence_socket": presence_info,
		"message": "Apply sessionDescription answer, wait ICE connected, then publish_tracks",
	}


@frappe.whitelist(allow_guest=True)
def publish_tracks(
	table_id: str,
	session_id: str = None,
	sdp: str = None,
	sdp_type: str = "offer",
	tracks: str | list | None = None,
):
	"""
	Push local tracks from the publisher PeerConnection.

	``tracks`` JSON list items should include location=local, trackName, mid (and kind).
	Example track entry (client builds mids from its transceiver order):
	  {"location": "local", "trackName": "table-video", "mid": "0"}
	"""
	require_stream_access()
	if not table_id:
		frappe.throw(_("table_id is required"))

	room = _get_or_create_room(table_id)
	session_id = session_id or room.publisher_session_id
	if not session_id:
		frappe.throw(_("No publisher session; call prepare_publish first"))

	if isinstance(tracks, str):
		tracks = frappe.parse_json(tracks)
	tracks = tracks or []
	if not tracks:
		frappe.throw(_("tracks list is required"))

	body_sdp = None
	if sdp:
		body_sdp = {"type": sdp_type or "offer", "sdp": sdp}

	result = sfu_client.add_tracks(session_id, tracks, body_sdp)

	# Extract track ids / mids from response + client metadata
	video_track_id = None
	audio_track_id = None
	video_track_name = None
	audio_track_name = None
	video_mid = None
	audio_mid = None
	returned_tracks = result.get("tracks") or []
	for i, t in enumerate(returned_tracks):
		tid = t.get("trackName") or t.get("mid") or (tracks[i].get("trackName") if i < len(tracks) else None)
		kind = (t.get("kind") or "").lower()
		# Prefer client-provided names / mids
		client = tracks[i] if i < len(tracks) else {}
		name = client.get("trackName") or tid
		mid = client.get("mid") if client.get("mid") not in (None, "") else t.get("mid")
		if kind == "audio" or (not kind and "audio" in (name or "").lower()):
			audio_track_id = t.get("sessionId") and f"{session_id}/{name}" or name
			audio_track_name = name
			audio_mid = mid
		else:
			video_track_id = name
			video_track_name = name
			video_mid = mid

	# Fallback: first track video, second audio by convention
	if not video_track_name and tracks:
		video_track_name = tracks[0].get("trackName")
		video_track_id = video_track_name
		if video_mid is None:
			video_mid = tracks[0].get("mid")
	if not audio_track_name and len(tracks) > 1:
		audio_track_name = tracks[1].get("trackName")
		audio_track_id = audio_track_name
		if audio_mid is None:
			audio_mid = tracks[1].get("mid")
	# Client-only mid pass when response omitted tracks
	if video_mid is None or audio_mid is None:
		for client in tracks:
			kind = (client.get("kind") or "").lower()
			name = client.get("trackName") or ""
			mid = client.get("mid")
			if mid in (None, ""):
				continue
			if kind == "audio" or "audio" in name.lower():
				if audio_mid is None:
					audio_mid = mid
			elif video_mid is None:
				video_mid = mid

	room.publisher_session_id = session_id
	room.video_track_id = video_track_id or video_track_name
	room.audio_track_id = audio_track_id or audio_track_name
	room.video_track_name = video_track_name
	room.audio_track_name = audio_track_name
	room.video_mid = str(video_mid) if video_mid not in (None, "") else None
	room.audio_mid = str(audio_mid) if audio_mid not in (None, "") else None
	room.status = "Live"
	room.last_error = ""
	_touch_publisher_heartbeat(room)
	room.save(ignore_permissions=True)
	frappe.db.commit()

	# Pipeline: passthrough → tracks for subscribers
	pipe = pipeline.run_pipeline(
		room.as_dict(),
		{
			"publisher_session_id": session_id,
			"video_track_id": room.video_track_id,
			"audio_track_id": room.audio_track_id,
			"video_track_name": room.video_track_name,
			"audio_track_name": room.audio_track_name,
		},
	)

	notify = live_now.notify_live(table_id, True)
	live_now.post_www_webhook(
		table_id,
		live=True,
		stream_name=f"Domino Match Table {table_id}",
	)
	if notify.get("success"):
		room.www_notified_live = 1
		room.save(ignore_permissions=True)
		frappe.db.commit()

	try:
		from domino_stream.api.events import emit_stream_event

		emit_stream_event(
			"publish",
			f"Published Live session for {table_id}",
			table_id=table_id,
			room=room.name,
			severity="Info",
			detail={
				"session_id": session_id,
				"video_track_name": room.video_track_name,
				"audio_track_name": room.audio_track_name,
				"video_mid": getattr(room, "video_mid", None),
				"audio_mid": getattr(room, "audio_mid", None),
			},
		)
	except Exception:
		pass

	answer = result.get("sessionDescription") or {}

	return {
		"success": True,
		"platform": "Domino Stream",
		"table_id": table_id,
		"session_id": session_id,
		"sessionDescription": answer,
		"tracks": returned_tracks,
		"tracks_for_subscribers": pipe.get("tracks_for_subscribers"),
		"pipeline_side_effects": pipe.get("side_effects"),
		"live_now": notify,
	}


@frappe.whitelist(allow_guest=True)
def publisher_heartbeat(table_id: str, session_id: str = None, via: str = None):
	"""Keep Live room alive while the publisher PeerConnection is healthy.

	Clients should call every ~10s while publishing (HTTP fallback). Socket
	presence pings also call this with ``via=socket`` so liveness knows a
	publisher is in ``publisher:{table_id}``.
	"""
	require_stream_access()
	if not table_id:
		frappe.throw(_("table_id is required"))

	room = frappe.db.get_value(
		"Stream Room",
		{"table_id": table_id},
		["name", "status", "publisher_session_id"],
		as_dict=True,
	)
	if not room:
		frappe.throw(_("No Domino Stream room for this table"))
	if room.status != "Live":
		frappe.throw(_("Room is not Live"))
	if session_id and room.publisher_session_id and session_id != room.publisher_session_id:
		frappe.throw(_("session_id does not match the current publisher session"))

	doc = frappe.get_doc("Stream Room", room.name)
	_touch_publisher_heartbeat(doc, via_socket=(via or "").lower() == "socket")
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"success": True,
		"table_id": table_id,
		"session_id": doc.publisher_session_id,
		"last_publisher_heartbeat": str(doc.last_publisher_heartbeat),
		"grace_seconds": get_heartbeat_grace_seconds(),
		"publisher_socket_present": cint(doc.publisher_socket_present),
	}


def _challenge_is_pending(room) -> bool:
	if isinstance(room, dict):
		challenge_id = room.get("kill_challenge_id")
		expires_at = room.get("kill_challenge_expires_at")
	else:
		challenge_id = getattr(room, "kill_challenge_id", None)
		expires_at = getattr(room, "kill_challenge_expires_at", None)
	if not challenge_id or not expires_at:
		return False
	return now_datetime() < get_datetime(expires_at)


def _emit_sfu_observation(table_id: str, room_name: str | None, observation: dict, connection_state: str | None = None):
	"""Stream Event with status, code, and description. No SDP or secrets."""
	try:
		from domino_stream.api.events import emit_stream_event

		detail = {
			"http_status": observation.get("http_status"),
			"error_code": observation.get("error_code"),
			"error_description": observation.get("error_description"),
			"session_id": observation.get("session_id"),
			"classification": observation.get("classification"),
			"table_id": table_id,
		}
		if connection_state:
			detail["connection_state"] = str(connection_state)[:40]
		classification = observation.get("classification") or "indeterminate"
		severity = "Warning" if classification in ("terminal", "tracks_inactive") else "Info"
		code = observation.get("error_code") or ""
		emit_stream_event(
			"sfu_observation",
			f"SFU {classification} {table_id} HTTP {observation.get('http_status')} {code}".strip(),
			table_id=table_id,
			room=room_name,
			severity=severity,
			detail=detail,
		)
	except Exception:
		pass


def maybe_open_kill_from_observation(room, observation: dict):
	"""Open a kill challenge for a terminal or tracks-inactive observation.

	Does not reset a challenge that is already pending. Alive, setting_up,
	and indeterminate observations do not arm.
	"""
	reason = kill_reason_for_observation(observation)
	if not reason:
		return None
	status = room.get("status") if isinstance(room, dict) else getattr(room, "status", None)
	if status != "Live":
		return None
	if _challenge_is_pending(room):
		return None
	return open_kill_challenge(room, reason)


def _load_room_for_observe(table_id: str):
	return frappe.db.get_value(
		"Stream Room",
		{"table_id": table_id},
		[
			"name",
			"status",
			"table_id",
			"publisher_session_id",
			"kill_challenge_id",
			"kill_challenge_expires_at",
		],
		as_dict=True,
	)


@frappe.whitelist(allow_guest=True)
def observe_publisher_session(table_id: str, connection_state: str = None):
	"""Classify the publisher SFU session and arm a kill challenge when terminal.

	Called when the publisher PeerConnection reports failed or disconnected.
	A brief disconnect that Cloudflare still shows as alive does not arm.
	"""
	require_stream_access()
	if not table_id:
		frappe.throw(_("table_id is required"))

	room = _load_room_for_observe(table_id)
	if not room:
		return {
			"success": True,
			"table_id": table_id,
			"observation": None,
			"challenge_opened": False,
			"action": "not armed: no room",
		}

	observation = observe_session(room.publisher_session_id)
	_emit_sfu_observation(table_id, room.name, observation, connection_state)
	pending = _challenge_is_pending(room)
	challenge = maybe_open_kill_from_observation(room, observation)
	return {
		"success": True,
		"table_id": table_id,
		"connection_state": connection_state,
		"observation": observation,
		"challenge_opened": bool(challenge),
		"challenge_pending": pending,
		"challenge": challenge,
		"action": observation_action(
			observation,
			challenge_opened=bool(challenge),
			challenge_pending=pending or bool(challenge),
		),
	}


@frappe.whitelist(allow_guest=True)
def publisher_presence_offline(table_id: str):
	"""Clear socket-present on leave/disconnect and arm force-kill if Live.

	Does not stop immediately. Opens a kill challenge (unless one is already
	pending) so an unanswered grace → delayed RQ job force-stops the room.
	"""
	require_stream_access()
	if not table_id:
		frappe.throw(_("table_id is required"))

	room = _load_room_for_observe(table_id)
	if not room:
		return {"success": True, "table_id": table_id, "cleared": False}

	frappe.db.set_value(
		"Stream Room",
		room.name,
		"publisher_socket_present",
		0,
		update_modified=False,
	)

	challenge_opened = False
	challenge_payload = None
	if room.status == "Live":
		pending = False
		if room.kill_challenge_id and room.kill_challenge_expires_at:
			pending = now_datetime() < get_datetime(room.kill_challenge_expires_at)
		if pending:
			# Do not reset the clock on duplicate offline
			frappe.db.commit()
		else:
			reason = "publisher_socket_gone"
			if room.publisher_session_id:
				observation = observe_session(room.publisher_session_id)
				_emit_sfu_observation(table_id, room.name, observation)
				# Terminal confirms the reason. Indeterminate does not cancel
				# the challenge — the publisher socket really left.
				if observation.get("classification") == "terminal":
					reason = kill_reason_for_observation(observation) or reason
			challenge_payload = open_kill_challenge(room, reason)
			challenge_opened = True
	else:
		frappe.db.commit()

	return {
		"success": True,
		"table_id": table_id,
		"cleared": True,
		"challenge_opened": challenge_opened,
		"challenge": challenge_payload,
	}


@frappe.whitelist(allow_guest=True)
def publisher_keep_alive(table_id: str, challenge_id: str, session_id: str = None):
	"""Answer a kill challenge: clear challenge fields and touch heartbeat."""
	require_stream_access()
	if not table_id:
		frappe.throw(_("table_id is required"))
	if not challenge_id:
		frappe.throw(_("challenge_id is required"))

	room = frappe.db.get_value(
		"Stream Room",
		{"table_id": table_id},
		[
			"name",
			"status",
			"publisher_session_id",
			"kill_challenge_id",
			"kill_challenge_expires_at",
		],
		as_dict=True,
	)
	if not room or room.status != "Live":
		frappe.throw(_("No live Domino Stream for this table"))
	if session_id and room.publisher_session_id and session_id != room.publisher_session_id:
		frappe.throw(_("session_id does not match the current publisher session"))
	if not room.kill_challenge_id or room.kill_challenge_id != challenge_id:
		frappe.throw(_("Invalid or expired kill challenge"))

	doc = frappe.get_doc("Stream Room", room.name)
	_clear_kill_challenge(doc)
	_touch_publisher_heartbeat(doc, via_socket=True)
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	try:
		from domino_stream.api.events import emit_stream_event

		emit_stream_event(
			"keep_alive",
			f"Publisher kept stream alive for {table_id}",
			table_id=table_id,
			room=doc.name,
			severity="Info",
			detail={"challenge_id": challenge_id},
		)
	except Exception:
		pass

	return {
		"success": True,
		"table_id": table_id,
		"session_id": doc.publisher_session_id,
		"last_publisher_heartbeat": str(doc.last_publisher_heartbeat),
		"challenge_cleared": True,
	}


def force_stop_if_challenge_expired(table_id: str, challenge_id: str):
	"""RQ delayed job: force-stop if this kill challenge is still pending.

	``keep_alive`` clears ``kill_challenge_id`` → this becomes a no-op.
	"""
	if not table_id or not challenge_id:
		return {"success": False, "stopped": False, "reason": "missing_args"}

	room = frappe.db.get_value(
		"Stream Room",
		{"table_id": table_id},
		["name", "status", "kill_challenge_id"],
		as_dict=True,
	)
	if not room:
		return {"success": True, "stopped": False, "reason": "no_room"}
	if room.status != "Live":
		return {"success": True, "stopped": False, "reason": "not_live"}
	if room.kill_challenge_id != challenge_id:
		return {"success": True, "stopped": False, "reason": "challenge_cleared"}

	doc = frappe.get_doc("Stream Room", room.name)
	_stop_room_internal(doc, reason="kill_challenge_expired")
	return {
		"success": True,
		"stopped": True,
		"table_id": table_id,
		"challenge_id": challenge_id,
	}


def _schedule_kill_challenge_expiry(table_id: str, challenge_id: str, expires_at) -> None:
	"""Queue a non-blocking RQ delayed job (``enqueue_at``) for challenge expiry.

	Job sits in Redis ScheduledJobRegistry until due — does not sleep on a
	worker. Requires ``domino_stream_rq_scheduler`` (RQScheduler) so delayed
	jobs are promoted onto the worker queue; Frappe ``bench worker`` alone
	does not run ``with_scheduler``.
	"""
	method = "domino_stream.api.room.force_stop_if_challenge_expired"
	kwargs = {"table_id": table_id, "challenge_id": challenge_id}
	job_key = f"domino_stream_kill:{table_id}:{challenge_id}"

	if frappe.flags.in_test:
		jobs = list(getattr(frappe.flags, "domino_stream_kill_jobs", None) or [])
		jobs.append(
			{
				"table_id": table_id,
				"challenge_id": challenge_id,
				"expires_at": expires_at,
				"method": method,
			}
		)
		frappe.flags.domino_stream_kill_jobs = jobs
		return

	from redis.exceptions import ConnectionError as RedisConnectionError
	from rq import Callback
	from rq.exceptions import NoSuchJobError
	from rq.job import Job

	from frappe.utils.background_jobs import (
		RQ_JOB_FAILURE_TTL,
		RQ_RESULTS_TTL,
		create_job_id,
		execute_job,
		get_queue,
		get_queues_timeout,
		truncate_failed_registry,
	)

	queue_args = {
		"site": frappe.local.site,
		"user": (frappe.session.user if frappe.session else None) or "Administrator",
		"method": method,
		"event": None,
		"job_name": method,
		"is_async": True,
		"kwargs": kwargs,
	}
	job_id = create_job_id(job_key)
	timeout = get_queues_timeout().get("default") or 300

	def enqueue_call():
		try:
			q = get_queue("default")
			try:
				existing = Job.fetch(job_id, connection=q.connection)
				existing.delete()
			except (NoSuchJobError, Exception):
				pass
			q.enqueue_at(
				expires_at,
				execute_job,
				kwargs=queue_args,
				timeout=timeout,
				failure_ttl=frappe.conf.get("rq_job_failure_ttl") or RQ_JOB_FAILURE_TTL,
				result_ttl=frappe.conf.get("rq_results_ttl") or RQ_RESULTS_TTL,
				job_id=job_id,
				on_failure=Callback(func=truncate_failed_registry),
			)
		except RedisConnectionError as e:
			stop_logger.error(
				"Kill challenge expiry schedule failed (redis) table=%s: %s",
				table_id,
				e,
			)
		except Exception:
			stop_logger.exception(
				"Kill challenge expiry schedule failed table=%s challenge=%s",
				table_id,
				challenge_id,
			)

	frappe.db.after_commit.add(enqueue_call)


def open_kill_challenge(room, reason: str, *, dry_run: bool = False) -> dict:
	"""Open a kill challenge, notify the publisher socket room, schedule force-stop.

	Schedules a per-challenge RQ delayed job at ``expires_at``. If the publisher
	answers ``keep_alive``, challenge fields clear and the job no-ops.
	``room`` may be a Stream Room doc or dict with name/table_id.
	"""
	grace = get_kill_challenge_grace_seconds()
	challenge_id = frappe.generate_hash(length=12)
	expires_at = now_datetime() + timedelta(seconds=grace)
	table_id = room.get("table_id") if isinstance(room, dict) else room.table_id
	room_name = room.get("name") if isinstance(room, dict) else room.name

	payload = {
		"table_id": table_id,
		"challenge_id": challenge_id,
		"reason": reason,
		"expires_at": str(expires_at),
		"grace_seconds": grace,
		"room": publisher_room_id(table_id),
	}

	if dry_run:
		return payload

	frappe.db.set_value(
		"Stream Room",
		room_name,
		{
			"kill_challenge_id": challenge_id,
			"kill_challenge_expires_at": expires_at,
			"kill_challenge_reason": (reason or "")[:140],
		},
		update_modified=False,
	)
	_schedule_kill_challenge_expiry(table_id, challenge_id, expires_at)
	frappe.db.commit()

	from domino_stream.api.live_now import notify_kill_challenge

	notify_kill_challenge(payload)

	try:
		from domino_stream.api.events import emit_stream_event

		emit_stream_event(
			"kill_challenge",
			f"Kill challenge opened for {table_id}: {reason}",
			table_id=table_id,
			room=room_name,
			severity="Warning",
			detail=payload,
		)
	except Exception:
		pass

	return payload


@frappe.whitelist(allow_guest=True)
def prepare_play(table_id: str, sdp: str = None, sdp_type: str = "offer"):
	"""
	Create a spectator SFU session with the client's first offer SDP (sessions/new only).

	Client: recvonly PeerConnection → createOffer → prepare_play(sdp) → setRemote(answer) →
	wait ICE connected → pull_play_tracks(session_id).
	"""
	require_stream_access()
	if not table_id:
		frappe.throw(_("table_id is required"))
	if not sdp:
		frappe.throw(_("sdp is required for prepare_play (Cloudflare sessions/new offer)"))

	room = frappe.db.get_value(
		"Stream Room",
		{"table_id": table_id},
		[
			"name",
			"status",
			"publisher_session_id",
		],
		as_dict=True,
	)
	if not room or room.status != "Live" or not room.publisher_session_id:
		frappe.throw(_("No live Domino Stream for this table"))

	session = sfu_client.create_session({"type": sdp_type or "offer", "sdp": sdp})
	session_id = session.get("sessionId")
	if not session_id:
		frappe.throw(_("SFU did not return spectator sessionId"))

	_upsert_participant(room.name, "spectator", session_id)
	frappe.db.commit()

	return {
		"ready": True,
		"platform": "Domino Stream",
		"table_id": table_id,
		"session_id": session_id,
		"sessionDescription": session.get("sessionDescription"),
		"iceServers": [{"urls": "stun:stun.cloudflare.com:3478"}],
		"message": "Apply sessionDescription answer, wait ICE connected, then pull_play_tracks",
	}


@frappe.whitelist(allow_guest=True)
def pull_play_tracks(table_id: str, session_id: str = None):
	"""
	Pull publisher remote tracks onto an ICE-connected spectator session (tracks/new).

	Must be called after prepare_play answer is applied and PeerConnection ICE is connected.
	"""
	require_stream_access()
	if not table_id:
		frappe.throw(_("table_id is required"))
	if not session_id:
		frappe.throw(_("session_id is required"))

	room = frappe.db.get_value(
		"Stream Room",
		{"table_id": table_id},
		[
			"name",
			"status",
			"publisher_session_id",
			"video_track_name",
			"audio_track_name",
			"video_track_id",
			"audio_track_id",
		],
		as_dict=True,
	)
	if not room or room.status != "Live" or not room.publisher_session_id:
		frappe.throw(_("No live Domino Stream for this table"))

	pipe = pipeline.run_pipeline(
		{"table_id": table_id, **room},
		{
			"publisher_session_id": room.publisher_session_id,
			"video_track_id": room.video_track_id,
			"audio_track_id": room.audio_track_id,
			"video_track_name": room.video_track_name,
			"audio_track_name": room.audio_track_name,
		},
	)
	sub_tracks = pipe.get("tracks_for_subscribers") or {}

	# Build remote track pull list (Cloudflare: location=remote + sessionId + trackName only)
	remote_tracks = []
	vname = sub_tracks.get("video_track_name") or room.video_track_name
	aname = sub_tracks.get("audio_track_name") or room.audio_track_name
	pub_session = sub_tracks.get("publisher_session_id") or room.publisher_session_id
	if vname:
		remote_tracks.append(
			{
				"location": "remote",
				"sessionId": pub_session,
				"trackName": vname,
			}
		)
	if aname:
		remote_tracks.append(
			{
				"location": "remote",
				"sessionId": pub_session,
				"trackName": aname,
			}
		)
	if not remote_tracks:
		frappe.throw(_("No publisher tracks available to pull"))

	pull = sfu_client.add_tracks(session_id, remote_tracks)
	session_description = pull.get("sessionDescription")
	requires_reneg = pull.get("requiresImmediateRenegotiation")
	if requires_reneg is None and session_description and session_description.get("type") == "offer":
		requires_reneg = True

	return {
		"ready": True,
		"platform": "Domino Stream",
		"table_id": table_id,
		"session_id": session_id,
		"sessionDescription": session_description,
		"requiresImmediateRenegotiation": requires_reneg,
		"tracks": pull.get("tracks"),
		"remote_tracks": remote_tracks,
		"message": "Apply sessionDescription; renegotiate with answer when type is offer",
	}


@frappe.whitelist(allow_guest=True)
def renegotiate_play(table_id: str, session_id: str, sdp: str, sdp_type: str = "answer"):
	"""Complete spectator negotiation after pull_play_tracks returns an SFU offer."""
	require_stream_access()
	if not session_id or not sdp:
		frappe.throw(_("session_id and sdp are required"))
	result = sfu_client.renegotiate(session_id, {"type": sdp_type or "answer", "sdp": sdp})
	return {"success": True, "table_id": table_id, "session_id": session_id, "result": result}


@frappe.whitelist(allow_guest=True)
def stop(table_id: str):
	"""Close publisher tracks / mark room stopped and notify www Live Now."""
	require_stream_access()
	if not table_id:
		frappe.throw(_("table_id is required"))

	room_name = frappe.db.get_value("Stream Room", {"table_id": table_id}, "name")
	if not room_name:
		return {"success": True, "message": "No room", "table_id": table_id}

	room = frappe.get_doc("Stream Room", room_name)
	notify = _stop_room_internal(room, reason="client_stop")
	return {
		"success": True,
		"platform": "Domino Stream",
		"table_id": table_id,
		"live_now": notify,
		"last_error": room.last_error,
	}


@frappe.whitelist(allow_guest=True)
def get_room_status(table_id: str):
	"""Spectator/debug: is this table live on Domino Stream?"""
	require_stream_access()
	room = frappe.db.get_value(
		"Stream Room",
		{"table_id": table_id},
		[
			"status",
			"publisher_session_id",
			"video_track_name",
			"audio_track_name",
			"last_publisher_heartbeat",
		],
		as_dict=True,
	)
	if not room:
		return {"table_id": table_id, "status": "Idle", "live": False}
	return {
		"table_id": table_id,
		"status": room.status,
		"live": room.status == "Live",
		"has_publisher": bool(room.publisher_session_id),
		"last_publisher_heartbeat": str(room.last_publisher_heartbeat)
		if room.last_publisher_heartbeat
		else None,
		"heartbeat_fresh": publisher_heartbeat_fresh(room) if room.status == "Live" else False,
		"grace_seconds": get_heartbeat_grace_seconds(),
	}


@frappe.whitelist()
def open_match_program_bridge(match_id, rtmps_url, stream_key, public_origin=None):
	"""WHIP path that pushes the match composite to a Cloudflare live input."""
	require_stream_access()
	from domino_stream.api.mediamtx_bridge import open_program_path

	return open_program_path(
		match_id,
		rtmps_url,
		stream_key,
		public_origin or "",
	)


@frappe.whitelist()
def close_match_program_bridge(match_id):
	"""Drop the MediaMTX path for this match."""
	require_stream_access()
	from domino_stream.api.mediamtx_bridge import close_program_path

	return close_program_path(match_id)
