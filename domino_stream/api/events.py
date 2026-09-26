"""Structured Stream Event writers for the Domino Stream console."""

from __future__ import annotations

import json

import frappe


def emit_stream_event(
	event_type: str,
	message: str,
	*,
	table_id: str | None = None,
	room: str | None = None,
	severity: str = "Info",
	detail: dict | str | None = None,
):
	"""Insert a Stream Event. Never raises — logging must not break control plane."""
	try:
		detail_text = ""
		if detail is not None:
			if isinstance(detail, (dict, list)):
				detail_text = json.dumps(detail, default=str)[:8000]
			else:
				detail_text = str(detail)[:8000]
		doc = frappe.get_doc(
			{
				"doctype": "Stream Event",
				"event_type": event_type or "console",
				"severity": severity if severity in ("Info", "Warning", "Error") else "Info",
				"table_id": table_id or "",
				"room": room or None,
				"message": (message or "")[:500],
				"detail": detail_text,
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		try:
			frappe.log_error(frappe.get_traceback(), "emit_stream_event")
		except Exception:
			pass
