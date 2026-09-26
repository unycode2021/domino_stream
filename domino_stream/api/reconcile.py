"""Reconcile Live Domino Stream rooms (legacy / diagnostic).

Publisher force-kill for abandoned streams is owned by **kill_challenge** +
RQ delayed ``force_stop_if_challenge_expired`` (see ``room.py``). This module
remains for dry-run / console diagnostics and is **not** scheduled by default.

When used manually, detection still requires **both** a stale publisher
heartbeat **and** an SFU session that looks dead (or missing), so an active
Cloudflare session is not cleared on heartbeat lag alone.

With a fresh heartbeat, inactive SFU tracks still accumulate ``tracks_inactive_since``
and stop after grace (crashed-but-still-pinging edge case).

When a publisher socket is present (``publisher_socket_present``), reconcile opens a
**kill challenge** instead of stopping immediately - the client gets ~150s to
``keep_alive``. If no socket is present, stop immediately.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, get_datetime, now_datetime, time_diff_in_seconds

from domino_stream.api.room import (
	_heartbeat_age_seconds,
	_stop_room_internal,
	get_heartbeat_grace_seconds,
	open_kill_challenge,
	publisher_heartbeat_fresh,
)
from domino_stream.api.session_health import session_looks_dead

logger = frappe.logger("domino_stream_reconcile", allow_site=True, file_count=20)


def _clear_tracks_inactive(room_name: str, *, dry_run: bool = False):
	if dry_run:
		return
	frappe.db.set_value(
		"Stream Room",
		room_name,
		"tracks_inactive_since",
		None,
		update_modified=False,
	)


def _mark_tracks_inactive(room_name: str, *, dry_run: bool = False):
	if dry_run:
		return
	frappe.db.set_value(
		"Stream Room",
		room_name,
		"tracks_inactive_since",
		now_datetime(),
		update_modified=False,
	)


def room_should_stop(room: dict, grace: int, *, dry_run: bool = False) -> tuple[bool, str]:
	"""Decide whether a Live room is a stop *candidate*. Returns (stop, reason).

	Policy: never clear Live Now on stale heartbeat alone while SFU still looks alive.
	Kill-challenge / expire handling lives in ``reconcile_live_rooms``.
	"""
	hb_fresh = publisher_heartbeat_fresh(room, grace)
	hb_age = _heartbeat_age_seconds(room)

	if not room.get("publisher_session_id"):
		if not hb_fresh:
			return True, f"missing_publisher_session hb_age={hb_age}s"
		return False, "missing_publisher_session_hb_fresh"

	dead = session_looks_dead(room.publisher_session_id)

	if dead is None:
		# Ambiguous SFU — do not stop on heartbeat alone
		if not hb_fresh:
			return False, f"stale_heartbeat_session_indeterminate age={hb_age}s grace={grace}s"
		return False, "session_check_indeterminate"

	if dead is False:
		if room.get("tracks_inactive_since"):
			_clear_tracks_inactive(room.name, dry_run=dry_run)
		if not hb_fresh:
			return False, f"stale_heartbeat_but_sfu_alive age={hb_age}s grace={grace}s"
		return False, "healthy"

	# SFU looks dead
	if not hb_fresh:
		return (
			True,
			f"stale_heartbeat_and_sfu_dead age={hb_age}s grace={grace}s",
		)

	# Heartbeat fresh but SFU dead — require grace window of inactivity
	inactive_since = room.get("tracks_inactive_since")
	if not inactive_since:
		_mark_tracks_inactive(room.name, dry_run=dry_run)
		return False, "tracks_inactive_marked"
	inactive_age = time_diff_in_seconds(now_datetime(), get_datetime(inactive_since))
	if inactive_age > grace:
		return True, f"tracks_inactive age={inactive_age}s grace={grace}s"
	return False, "tracks_inactive_within_grace"


def _challenge_status(row: dict) -> tuple[str, str]:
	"""Return (status, reason) for an active/expired/none kill challenge.

	status: none | pending | expired
	"""
	challenge_id = row.get("kill_challenge_id")
	expires_at = row.get("kill_challenge_expires_at")
	if not challenge_id or not expires_at:
		return "none", ""
	expires = get_datetime(expires_at)
	if now_datetime() < expires:
		remain = time_diff_in_seconds(expires, now_datetime())
		return "pending", f"challenge_pending id={challenge_id} remain={remain}s"
	return "expired", f"kill_challenge_expired id={challenge_id} reason={row.get('kill_challenge_reason') or ''}"


def _room_decision(row: dict, grace: int, *, dry_run: bool = False) -> dict:
	"""Build reconcile decision including challenge phase."""
	chal_status, chal_reason = _challenge_status(row)
	dead = None
	if row.get("publisher_session_id"):
		dead = session_looks_dead(row.publisher_session_id)

	base = {
		"name": row.name,
		"table_id": row.table_id,
		"hb_age_s": _heartbeat_age_seconds(row),
		"heartbeat_fresh": publisher_heartbeat_fresh(row, grace),
		"session_dead": dead,
		"publisher_session_id": row.publisher_session_id,
		"publisher_socket_present": cint(row.get("publisher_socket_present")),
		"challenge_status": chal_status,
	}

	if chal_status == "pending":
		return {
			**base,
			"should_stop": False,
			"open_challenge": False,
			"reason": chal_reason,
		}

	if chal_status == "expired":
		return {
			**base,
			"should_stop": True,
			"open_challenge": False,
			"reason": chal_reason,
		}

	should_stop, reason = room_should_stop(row, grace, dry_run=dry_run)
	if not should_stop:
		return {
			**base,
			"should_stop": False,
			"open_challenge": False,
			"reason": reason,
		}

	# Candidate to stop — challenge if publisher socket present, else stop now
	if cint(row.get("publisher_socket_present")):
		return {
			**base,
			"should_stop": False,
			"open_challenge": True,
			"reason": f"challenge_candidate: {reason}",
			"candidate_reason": reason,
		}

	return {
		**base,
		"should_stop": True,
		"open_challenge": False,
		"reason": reason,
	}


@frappe.whitelist(allow_guest=True)
def reconcile_live_rooms(dry_run: int | str | bool = 0):
	"""Scheduler entry: stop abandoned Live rooms and clear www streaming flag.

	Pass ``dry_run=1`` to evaluate decisions without stopping (whitelist / bench).
	"""
	from domino_stream.api.auth import require_stream_access

	dry = bool(cint(dry_run))
	if dry:
		require_stream_access()

	grace = get_heartbeat_grace_seconds()
	rooms = frappe.get_all(
		"Stream Room",
		filters={"status": "Live"},
		fields=[
			"name",
			"table_id",
			"publisher_session_id",
			"last_publisher_heartbeat",
			"tracks_inactive_since",
			"video_track_name",
			"audio_track_name",
			"status",
			"publisher_socket_present",
			"kill_challenge_id",
			"kill_challenge_expires_at",
			"kill_challenge_reason",
		],
	)
	stopped = []
	challenged = []
	decisions = []
	for row in rooms:
		try:
			decision = _room_decision(row, grace, dry_run=dry)
			decisions.append(decision)
			logger.info(
				"Reconcile %s table %s should_stop=%s open_challenge=%s reason=%s hb_age=%s session_dead=%s socket=%s",
				"dry-run" if dry else "tick",
				row.table_id,
				decision["should_stop"],
				decision.get("open_challenge"),
				decision["reason"],
				decision["hb_age_s"],
				decision["session_dead"],
				decision.get("publisher_socket_present"),
			)

			if dry:
				continue

			if decision.get("open_challenge"):
				payload = open_kill_challenge(
					row,
					decision.get("candidate_reason") or decision["reason"],
				)
				challenged.append(
					{"table_id": row.table_id, "challenge_id": payload.get("challenge_id")}
				)
				continue

			if not decision["should_stop"]:
				continue

			doc = frappe.get_doc("Stream Room", row.name)
			if doc.status != "Live":
				continue
			_stop_room_internal(doc, reason=f"reconcile: {decision['reason']}")
			stopped.append({"table_id": row.table_id, "reason": decision["reason"]})
			logger.info("Reconcile stopped table %s (%s)", row.table_id, decision["reason"])
			try:
				from domino_stream.api.events import emit_stream_event

				emit_stream_event(
					"reconcile",
					f"Reconcile stopped {row.table_id}: {decision['reason']}",
					table_id=row.table_id,
					room=row.name,
					severity="Warning",
					detail=decision,
				)
			except Exception:
				pass
		except Exception:
			logger.exception("Reconcile failed for room %s", row.get("name"))

	result = {
		"checked": len(rooms),
		"stopped": stopped,
		"challenged": challenged,
		"grace_seconds": grace,
		"dry_run": dry,
	}
	if dry:
		result["decisions"] = decisions
	return result
