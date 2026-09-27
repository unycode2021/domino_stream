"""Redis fan-out for Domino Stream Socket.IO (self-contained; no Domino101)."""

from __future__ import annotations

from contextlib import suppress

import frappe
import redis
from frappe import _
from frappe.utils.background_jobs import get_redis_connection_without_auth

DEFAULT_PRESENCE_NAMESPACE = "domino-stream"
DEFAULT_PRESENCE_SOCKET_PATH = "/domino-stream-socket.io"
KILL_CHALLENGE_EVENT = "kill_challenge"
PUBLISHER_ROOM_PREFIX = "publisher:"
PRESENCE_TOKEN_TTL_SECONDS = 60 * 60 * 24


def get_presence_socket_namespace() -> str:
	"""Namespace the Domino Stream socket process actually serves."""
	return DEFAULT_PRESENCE_NAMESPACE


def get_presence_socket_path() -> str:
	"""Engine path the Domino Stream socket process actually serves."""
	return DEFAULT_PRESENCE_SOCKET_PATH


def publisher_room_id(table_id: str) -> str:
	return f"{PUBLISHER_ROOM_PREFIX}{table_id}"


def _presence_token_cache_key(token: str) -> str:
	return f"domino_stream_presence:{token}"


def issue_presence_token(table_id: str, session_id: str) -> str:
	"""Short-lived token for Socket.IO presence auth (esp. remote DCMS → stream host)."""
	token = frappe.generate_hash(length=32)
	frappe.cache().set_value(
		_presence_token_cache_key(token),
		{"table_id": table_id, "session_id": session_id},
		expires_in_sec=PRESENCE_TOKEN_TTL_SECONDS,
	)
	return token


def resolve_presence_token(token: str) -> dict | None:
	if not token:
		return None
	data = frappe.cache().get_value(_presence_token_cache_key(token))
	return data if isinstance(data, dict) else None


@frappe.whitelist(allow_guest=True)
def validate_presence_token(token: str = None):
	"""Socket auth helper — returns identity when token is valid."""
	data = resolve_presence_token(token)
	if not data:
		frappe.throw(_("Invalid or expired presence token"), frappe.AuthenticationError)
	return {
		"user": "Publisher",
		"user_type": "Website User",
		"table_id": data.get("table_id"),
		"session_id": data.get("session_id"),
	}


def publish_stream_event(event: str, message, room: str | None = None, namespace: str | None = None):
	"""Publish to Redis ``events`` for the Domino Stream Socket.IO process.

	Payload shape matches Domino101/Frappe realtime: ``{event, message, room, namespace}``.
	"""
	nsp = (namespace or get_presence_socket_namespace()).lstrip("/")
	with suppress(redis.exceptions.ConnectionError):
		r = get_redis_connection_without_auth()
		r.publish(
			"events",
			frappe.as_json(
				{
					"event": event,
					"message": message,
					"room": room,
					"namespace": nsp,
				}
			),
		)


def presence_socket_client_info() -> dict:
	"""Public connection hints for DCMS / Connect clients."""
	return {
		"namespace": get_presence_socket_namespace(),
		"path": get_presence_socket_path(),
		"room_prefix": PUBLISHER_ROOM_PREFIX,
	}
