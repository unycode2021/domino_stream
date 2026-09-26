"""Inbound API key checks for DCMS / Connect / www calling stream site."""

from __future__ import annotations

import frappe
from frappe import _

CONSOLE_ROLES = ("System Manager", "Domino Manager")


def get_inbound_api_key() -> str:
	settings = frappe.get_single("Stream Settings")
	try:
		return (settings.get_password("inbound_api_key", raise_exception=False) or "").strip()
	except Exception:
		return ""


def require_stream_access():
	"""
	Allow logged-in users, inbound API key, or a valid presence token.

	Clients may send:
	- Header X-Domino-Stream-Key: <key>
	- Authorization: Bearer <key>
	- Header X-Domino-Stream-Presence-Token: <token from prepare_publish>
	"""
	if frappe.session.user and frappe.session.user not in ("Guest",):
		# Authenticated Frappe session (Desk or JWT user)
		return

	presence_token = (
		frappe.get_request_header("X-Domino-Stream-Presence-Token") or ""
	).strip()
	if presence_token:
		from domino_stream.api.realtime_pub import resolve_presence_token

		if resolve_presence_token(presence_token):
			return
		frappe.throw(_("Invalid Domino Stream presence token"), frappe.AuthenticationError)

	expected = get_inbound_api_key()
	if not expected:
		# No inbound key configured — require logged-in user
		frappe.throw(_("Login required"), frappe.PermissionError)

	provided = (
		frappe.get_request_header("X-Domino-Stream-Key")
		or ""
	).strip()
	auth = (frappe.get_request_header("Authorization") or "").strip()
	if auth.lower().startswith("bearer "):
		provided = provided or auth[7:].strip()

	if not provided or provided != expected:
		frappe.throw(_("Invalid Domino Stream API key"), frappe.AuthenticationError)


def require_console_access():
	"""Desk/DCMS session only — System Manager or Domino Manager. No API key."""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(_("Login required"), frappe.PermissionError)
	roles = set(frappe.get_roles(frappe.session.user))
	if not roles.intersection(CONSOLE_ROLES):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
