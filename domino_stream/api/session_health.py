"""Cloudflare SFU session liveness helpers shared by reconcile and stop attribution."""

from __future__ import annotations

import frappe

from domino_stream.api import sfu_client

logger = frappe.logger("domino_stream_reconcile", allow_site=True, file_count=20)


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


def session_looks_dead(publisher_session_id: str) -> bool | None:
	"""True=dead, False=alive, None=indeterminate (API error or ambiguous payload)."""
	if not publisher_session_id:
		return True
	try:
		payload = sfu_client.get_session(publisher_session_id)
	except Exception as e:
		logger.info("get_session failed for %s: %s", publisher_session_id, e)
		return None
	return local_tracks_state(payload)


def session_verdict_label(publisher_session_id: str | None) -> str:
	"""Human label for stop attribution / dry-run."""
	if not publisher_session_id:
		return "missing"
	dead = session_looks_dead(publisher_session_id)
	if dead is True:
		return "dead"
	if dead is False:
		return "alive"
	return "indeterminate"
