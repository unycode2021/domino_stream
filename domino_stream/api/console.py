"""Domino Stream ops console APIs (Desk/DCMS session auth)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from domino_stream.api.auth import require_console_access
from domino_stream.api.events import emit_stream_event
from domino_stream.api.room import (
	_challenge_is_pending,
	_emit_sfu_observation,
	_heartbeat_age_seconds,
	_stop_room_internal,
	get_heartbeat_grace_seconds,
	get_kill_challenge_grace_seconds,
	maybe_open_kill_from_observation,
	publisher_heartbeat_fresh,
)
from domino_stream.api import sfu_client
from domino_stream.api.session_health import observation_action, observe_session
from domino_stream.install import RECONCILE_METHOD


def _room_row_payload(row) -> dict:
	grace = get_heartbeat_grace_seconds()
	hb_age = _heartbeat_age_seconds(row)
	return {
		"name": row.name,
		"table_id": row.table_id,
		"status": row.status,
		"publisher_session_id": row.publisher_session_id,
		"publisher_user": row.publisher_user,
		"last_publisher_heartbeat": str(row.last_publisher_heartbeat)
		if row.last_publisher_heartbeat
		else None,
		"hb_age_s": hb_age,
		"heartbeat_fresh": publisher_heartbeat_fresh(row, grace) if row.status == "Live" else False,
		"video_track_name": row.video_track_name,
		"audio_track_name": row.audio_track_name,
		"video_mid": getattr(row, "video_mid", None),
		"audio_mid": getattr(row, "audio_mid", None),
		"last_error": row.last_error,
		"modified": str(row.modified) if row.modified else None,
		"tracks_inactive_since": str(row.tracks_inactive_since)
		if row.tracks_inactive_since
		else None,
		"sfu_observation": None,
		"observation_action": None,
	}


def _probe_live_session(row, payload, *, timeout: int = 5) -> dict:
	"""Classify a Live publisher session and arm a kill challenge when terminal."""
	if row.status != "Live" or not row.publisher_session_id:
		return payload
	observation = observe_session(row.publisher_session_id, timeout=timeout)
	pending = _challenge_is_pending(row)
	challenge = maybe_open_kill_from_observation(row, observation)
	if challenge:
		_emit_sfu_observation(row.table_id, row.name, observation)
	payload["sfu_observation"] = observation
	payload["session_verdict"] = observation.get("classification")
	payload["observation_action"] = observation_action(
		observation,
		challenge_opened=bool(challenge),
		challenge_pending=pending or bool(challenge),
	)
	return payload


@frappe.whitelist()
def list_rooms(status: str = None, limit: int = 50):
	"""List Stream Rooms for the console."""
	require_console_access()
	limit = max(1, min(cint(limit) or 50, 200))
	filters = {}
	if status:
		filters["status"] = status
	rows = frappe.get_all(
		"Stream Room",
		filters=filters,
		fields=[
			"name",
			"table_id",
			"status",
			"publisher_session_id",
			"publisher_user",
			"last_publisher_heartbeat",
			"video_track_name",
			"audio_track_name",
			"video_mid",
			"audio_mid",
			"last_error",
			"modified",
			"tracks_inactive_since",
			"kill_challenge_id",
			"kill_challenge_expires_at",
		],
		order_by="modified desc",
		limit_page_length=limit,
	)
	rooms = []
	for row in rows:
		payload = _room_row_payload(row)
		_probe_live_session(row, payload)
		rooms.append(payload)
	return {
		"rooms": rooms,
		"grace_seconds": get_heartbeat_grace_seconds(),
	}


@frappe.whitelist()
def get_room(table_id: str = None, name: str = None):
	"""Room detail + SFU session probe."""
	require_console_access()
	if not table_id and not name:
		frappe.throw(_("table_id or name is required"))
	filters = {"table_id": table_id} if table_id else {"name": name}
	row = frappe.db.get_value(
		"Stream Room",
		filters,
		[
			"name",
			"table_id",
			"status",
			"publisher_session_id",
			"publisher_user",
			"last_publisher_heartbeat",
			"video_track_name",
			"audio_track_name",
			"video_mid",
			"audio_mid",
			"video_track_id",
			"audio_track_id",
			"last_error",
			"modified",
			"tracks_inactive_since",
			"www_notified_live",
			"kill_challenge_id",
			"kill_challenge_expires_at",
		],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Stream Room not found"))

	payload = _room_row_payload(row)
	payload["video_track_id"] = row.video_track_id
	payload["audio_track_id"] = row.audio_track_id
	payload["www_notified_live"] = cint(row.www_notified_live)

	participants = frappe.get_all(
		"Stream Participant",
		filters={"room": row.name},
		fields=["name", "role", "session_id", "user", "status", "modified"],
		order_by="modified desc",
		limit_page_length=50,
	)
	payload["participants"] = participants

	_probe_live_session(row, payload, timeout=15)
	payload["sfu_session"] = payload.get("sfu_observation")
	return payload


@frappe.whitelist()
def force_stop(table_id: str):
	"""Operator force-stop a room and clear Live Now."""
	require_console_access()
	if not table_id:
		frappe.throw(_("table_id is required"))
	room_name = frappe.db.get_value("Stream Room", {"table_id": table_id}, "name")
	if not room_name:
		return {"success": True, "message": "No room", "table_id": table_id}
	room = frappe.get_doc("Stream Room", room_name)
	notify = _stop_room_internal(
		room,
		reason=f"console_force_stop by={frappe.session.user}",
	)
	emit_stream_event(
		"console",
		f"Force stop {table_id} by {frappe.session.user}",
		table_id=table_id,
		room=room.name,
		severity="Warning",
	)
	return {
		"success": True,
		"table_id": table_id,
		"last_error": room.last_error,
		"live_now": notify,
	}


@frappe.whitelist()
def list_events(
	table_id: str = None,
	event_type: str = None,
	severity: str = None,
	limit: int = 100,
	offset: int = 0,
):
	"""Paginated Stream Event log for the console."""
	require_console_access()
	limit = max(1, min(cint(limit) or 100, 500))
	offset = max(0, cint(offset) or 0)
	filters = {}
	if table_id:
		filters["table_id"] = table_id
	if event_type:
		filters["event_type"] = event_type
	if severity:
		filters["severity"] = severity
	rows = frappe.get_all(
		"Stream Event",
		filters=filters,
		fields=[
			"name",
			"event_type",
			"severity",
			"table_id",
			"room",
			"message",
			"detail",
			"creation",
			"owner",
		],
		order_by="creation desc",
		limit_page_length=limit,
		limit_start=offset,
	)
	total = frappe.db.count("Stream Event", filters=filters)
	return {"events": rows, "total": total, "limit": limit, "offset": offset}


@frappe.whitelist()
def get_health():
	"""SFU + kill-challenge + optional legacy reconcile summary for the console."""
	require_console_access()
	creds = sfu_client.get_sfu_credentials()
	grace = get_heartbeat_grace_seconds()
	kill_grace = get_kill_challenge_grace_seconds()
	job = frappe.db.get_value(
		"Scheduled Job Type",
		{"method": RECONCILE_METHOD},
		["name", "stopped", "last_execution", "frequency", "cron_format"],
		as_dict=True,
	)
	paused_conf = cint(frappe.conf.get("domino_stream_reconcile_paused"))
	live_count = frappe.db.count("Stream Room", {"status": "Live"})
	return {
		"sfu_configured": bool(creds.get("configured")),
		"grace_seconds": grace,
		"kill_challenge_grace_seconds": kill_grace,
		"force_kill": "kill_challenge_rq",
		"reconcile_job": job,
		"reconcile_paused_config": paused_conf or (job and cint(job.get("stopped"))),
		"reconcile_required": False,
		"live_room_count": live_count,
		"user": frappe.session.user,
		"stream_settings_url": "/app/stream-settings",
	}
