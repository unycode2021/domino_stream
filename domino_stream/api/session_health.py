"""Cloudflare SFU session observations shared by console, kill challenge, and reconcile.

Classification follows Cloudflare Realtime error codes:
https://developers.cloudflare.com/realtime/sfu/observability/error-codes/
"""

from __future__ import annotations

import frappe

from domino_stream.api import sfu_client

logger = frappe.logger("domino_stream_reconcile", allow_site=True, file_count=20)

# These codes are not proof the publisher session is gone.
_INDETERMINATE_CODES = {
	"retryable_transient_error",
	"temporarily_unavailable_error",
	"transport_unavailable_error",
	"backend_error",
	"internal_error",
}

_ARM_CLASSIFICATIONS = {"terminal", "tracks_inactive"}


def local_tracks_state(session_payload: dict) -> bool | None:
	"""SFU track verdict from GET /sessions/{id} payload.

	Returns:
		True — session missing or all known local tracks inactive (dead)
		False — at least one active track (alive)
		None — indeterminate (empty / no location=local); do **not** treat as dead
	"""
	if not session_payload or session_payload.get("_missing"):
		return True
	tracks = session_payload.get("tracks") or []
	if not tracks:
		return None

	local = [t for t in tracks if (t.get("location") or "").lower() == "local"]
	if not local:
		# Cloudflare sometimes omits location — any active track ⇒ alive; else unknown
		if any((t.get("status") or "").lower() == "active" for t in tracks):
			return False
		return None

	if any((t.get("status") or "").lower() == "active" for t in local):
		return False
	return True


def _track_rows(payload: dict) -> list[dict]:
	"""Per-track fields only. Never copy SDP or ICE."""
	rows = []
	for track in payload.get("tracks") or []:
		if not isinstance(track, dict):
			continue
		rows.append(
			{
				"mid": track.get("mid"),
				"location": track.get("location"),
				"status": track.get("status"),
				"error_code": track.get("errorCode") or None,
				"error_description": track.get("errorDescription") or None,
			}
		)
	return rows


def _error_text(payload: dict) -> tuple[str, str]:
	err = payload.get("_error") if isinstance(payload.get("_error"), dict) else {}
	code = payload.get("errorCode") or err.get("errorCode") or ""
	desc = payload.get("errorDescription") or err.get("errorDescription") or ""
	return str(code or "").strip(), str(desc or "").strip()


def classify_session_payload(payload: dict | None, session_id: str | None = None) -> dict:
	"""Turn one GET /sessions/{id} body into a structured observation.

	Classifications:
		terminal — HTTP 410 session_error (or 410 with no code), or HTTP 404
		setting_up — HTTP 425 session_error
		alive — HTTP 200 and at least one local (or unscoped) track active
		tracks_inactive — HTTP 200, local tracks present, none active
		indeterminate — timeout, 5xx, transient codes, or tracks with no location
	"""
	payload = payload if isinstance(payload, dict) else {}
	http = payload.get("_http_status")
	try:
		http_status = int(http) if http is not None and http != "" else None
	except (TypeError, ValueError):
		http_status = None
	error_code, error_description = _error_text(payload)
	tracks = _track_rows(payload)
	code_l = error_code.lower()

	if payload.get("_transport_error"):
		classification = "indeterminate"
	elif http_status == 404:
		classification = "terminal"
	elif http_status == 410 and code_l in ("", "session_error"):
		classification = "terminal"
	elif http_status == 425 and code_l in ("", "session_error"):
		classification = "setting_up"
	elif http_status is not None and http_status >= 500:
		classification = "indeterminate"
	elif code_l in _INDETERMINATE_CODES:
		classification = "indeterminate"
	elif http_status is not None and http_status >= 400:
		classification = "indeterminate"
	else:
		classification = _classify_tracks(tracks)

	return {
		"http_status": http_status,
		"error_code": error_code or None,
		"error_description": error_description or None,
		"session_id": session_id or None,
		"tracks": tracks,
		"classification": classification,
	}


def _classify_tracks(tracks: list[dict]) -> str:
	local = [t for t in tracks if (t.get("location") or "").lower() == "local"]
	if local:
		if any((t.get("status") or "").lower() == "active" for t in local):
			return "alive"
		return "tracks_inactive"
	if any((t.get("status") or "").lower() == "active" for t in tracks):
		return "alive"
	return "indeterminate"


def observe_session(session_id: str | None, timeout: int = 30) -> dict:
	"""GET the publisher session and classify it. Never raises."""
	if not session_id:
		return classify_session_payload(
			{
				"_http_status": 404,
				"errorCode": "not_found",
				"errorDescription": "No publisher session id",
			},
			None,
		)
	try:
		payload = sfu_client.get_session(session_id, timeout=timeout)
	except Exception:
		logger.info("get_session failed for %s", session_id)
		return classify_session_payload(
			{
				"_transport_error": True,
				"errorDescription": "SFU request failed",
			},
			session_id,
		)
	return classify_session_payload(payload, session_id)


def kill_reason_for_observation(observation: dict | None) -> str | None:
	"""Reason string when this observation should open a kill challenge."""
	if not observation:
		return None
	classification = observation.get("classification")
	if classification == "tracks_inactive":
		return "sfu_tracks_inactive"
	if classification != "terminal":
		return None
	if observation.get("http_status") == 404:
		return "sfu_session_not_found"
	return "sfu_session_disconnected"


def observation_arms_kill(observation: dict | None) -> bool:
	return (observation or {}).get("classification") in _ARM_CLASSIFICATIONS


def observation_action(
	observation: dict | None,
	*,
	challenge_opened: bool = False,
	challenge_pending: bool = False,
) -> str:
	classification = (observation or {}).get("classification") or "indeterminate"
	if classification in _ARM_CLASSIFICATIONS:
		if challenge_opened or challenge_pending:
			return "kill challenge armed"
		return "not armed: room is not Live or challenge was not opened"
	if classification == "alive":
		return "not armed: session alive"
	if classification == "setting_up":
		return "not armed: session still setting up"
	return "not armed: observation indeterminate"


def session_looks_dead(publisher_session_id: str) -> bool | None:
	"""True=terminal or tracks inactive, False=alive, None=do not kill."""
	if not publisher_session_id:
		return True
	observation = observe_session(publisher_session_id)
	classification = observation.get("classification")
	if classification in _ARM_CLASSIFICATIONS:
		return True
	if classification == "alive":
		return False
	return None


def session_verdict_label(publisher_session_id: str | None) -> str:
	"""Classification label for stop attribution."""
	if not publisher_session_id:
		return "missing"
	return observe_session(publisher_session_id).get("classification") or "indeterminate"
