"""Redis fan-out for Domino Stream Socket.IO (self-contained; no Domino101)."""

from __future__ import annotations

import hashlib
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


def presence_token_hash(token: str) -> str:
	return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def issue_presence_token(table_id: str, session_id: str) -> str:
	"""Token for Socket.IO presence auth (esp. remote DCMS → stream host).

	Kept in Redis for fast lookups, and its hash on the Stream Room so the socket
	service's offline / keep_alive calls still authenticate if the Redis copy is gone
	(otherwise an abandoned stream can never be challenged and stays Live).
	"""
	token = frappe.generate_hash(length=32)
	frappe.cache().set_value(
		_presence_token_cache_key(token),
		{"table_id": table_id, "session_id": session_id},
		expires_in_sec=PRESENCE_TOKEN_TTL_SECONDS,
	)
	room = frappe.db.get_value("Stream Room", {"table_id": table_id}, "name")
	if room and frappe.db.has_column("Stream Room", "presence_token_hash"):
		frappe.db.set_value(
			"Stream Room", room, "presence_token_hash", presence_token_hash(token), update_modified=False
		)
	return token


def _resolve_presence_token_from_room(token: str) -> dict | None:
	if not frappe.db.has_column("Stream Room", "presence_token_hash"):
		return None
	row = frappe.db.get_value(
		"Stream Room",
		{"presence_token_hash": presence_token_hash(token)},
		["table_id", "publisher_session_id"],
		as_dict=True,
	)
	if not row or not row.table_id:
		return None
	return {"table_id": row.table_id, "session_id": row.publisher_session_id, "source": "room"}


def resolve_presence_token(token: str) -> dict | None:
	"""Identity for a presence token: Redis first, then the Stream Room's stored hash."""
	if not token:
		return None
	data = frappe.cache().get_value(_presence_token_cache_key(token))
	if isinstance(data, dict):
		return data
	return _resolve_presence_token_from_room(token)


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
