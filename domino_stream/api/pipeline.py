"""Modular media pipeline stages for Domino Stream.

MVP: passthrough — publisher tracks are offered to spectators unchanged.

Later: ai_worker stages can subscribe to publisher tracks, process media,
and emit Domino101 events or publish derived tracks.
"""

from __future__ import annotations

import json

import frappe


def list_enabled_stages() -> list[dict]:
	"""Return enabled pipeline stages ordered by sort_order."""
	if not frappe.db.exists("DocType", "Stream Pipeline Stage"):
		return [{"stage_name": "passthrough", "stage_type": "passthrough", "enabled": 1, "sort_order": 100}]

	rows = frappe.get_all(
		"Stream Pipeline Stage",
		filters={"enabled": 1},
		fields=["name", "stage_name", "stage_type", "sort_order", "config_json", "description"],
		order_by="sort_order asc",
	)
	if not rows:
		return [{"stage_name": "passthrough", "stage_type": "passthrough", "enabled": 1, "sort_order": 100}]
	return rows


def run_pipeline(room: dict, tracks: dict) -> dict:
	"""
	Run enabled stages over publisher tracks.

	Args:
	        room: Stream Room as dict (table_id, session/track ids, …)
	        tracks: {"video_track_id", "audio_track_id", "video_track_name", "audio_track_name",
	                 "publisher_session_id"}

	Returns:
	        dict with tracks_for_subscribers (same shape) and side_effects list.
	"""
	current = dict(tracks)
	side_effects = []

	for stage in list_enabled_stages():
		stage_type = stage.get("stage_type") or "passthrough"
		config = {}
		raw = stage.get("config_json")
		if raw:
			try:
				config = json.loads(raw) if isinstance(raw, str) else (raw or {})
			except Exception:
				config = {}

		if stage_type == "passthrough":
			result = _passthrough(room, current, config)
		elif stage_type == "ai_worker":
			result = _ai_worker_stub(room, current, config)
		else:
			result = {"tracks": current, "side_effects": []}

		current = result.get("tracks") or current
		side_effects.extend(result.get("side_effects") or [])

	return {
		"tracks_for_subscribers": current,
		"side_effects": side_effects,
	}


def _passthrough(room: dict, tracks: dict, config: dict) -> dict:
	"""Identity stage — spectators subscribe to the same publisher track IDs."""
	return {"tracks": tracks, "side_effects": []}


def _ai_worker_stub(room: dict, tracks: dict, config: dict) -> dict:
	"""
	Placeholder for future AI subscribers.

	A real implementation would:
	1. Open an SFU session as role=ai
	2. Pull publisher video/audio tracks
	3. Process (Workers AI / external)
	4. Emit events to www and/or publish a derived track
	"""
	frappe.logger("domino_stream_pipeline").info(
		f"ai_worker stub skipped for table={room.get('table_id')} config_keys={list(config.keys())}"
	)
	return {
		"tracks": tracks,
		"side_effects": [
			{
				"type": "ai_worker_noop",
				"message": "AI stage registered but not implemented in MVP",
				"table_id": room.get("table_id"),
			}
		],
	}


def ensure_default_passthrough_stage():
	"""Create default passthrough stage if none exists."""
	if frappe.db.exists("Stream Pipeline Stage", {"stage_name": "passthrough"}):
		return
	doc = frappe.get_doc(
		{
			"doctype": "Stream Pipeline Stage",
			"stage_name": "passthrough",
			"enabled": 1,
			"stage_type": "passthrough",
			"sort_order": 100,
			"description": "MVP: forward publisher tracks to all spectators unchanged",
			"config_json": "{}",
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
