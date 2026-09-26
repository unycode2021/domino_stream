"""Notify www Domino101 Live Now via start_stream / stop_stream (or in-process).

Also posts an optional Ant-parity guest webhook (streamer.hooks) with source=domino_stream.
"""

from __future__ import annotations

import time

import frappe
import requests

logger = frappe.logger("domino_stream_live_now", allow_site=True, file_count=20)

SELF_SENTINEL = "self"
DEFAULT_HOOKS_PATH = "/api/method/domino101.api.streamer.hooks"


def _www_credentials() -> dict:
	settings = frappe.get_single("Stream Settings")
	base = (settings.get("www_site_url") or "").strip().rstrip("/")
	key = ""
	try:
		key = (settings.get_password("www_api_key", raise_exception=False) or "").strip()
	except Exception:
		key = ""
	webhook = (settings.get("www_webhook_url") or "").strip()
	is_self = base.lower() == SELF_SENTINEL
	return {
		"base": base,
		"key": key,
		"webhook": webhook,
		"is_self": is_self,
		"configured": bool(base),
	}


def _notify_live_local(table_id: str, live: bool) -> dict:
	"""Co-located SFU + Domino101: set streaming flag in-process (no HTTP / API key)."""
	from domino101.connect.stream_registration import apply_table_stream_state

	out = apply_table_stream_state(
		table_id,
		1 if live else 0,
		notify_stream_live=bool(live),
		emit_live_now=True,
	)
	logger.info(
		"Live Now notify local mode=%s table_id=%s streaming=%s",
		"start" if live else "stop",
		table_id,
		1 if live else 0,
	)
	return {
		"success": True,
		"mode": "local",
		"live": live,
		"table_id": table_id,
		"response": out,
	}


def _notify_live_remote(table_id: str, live: bool, creds: dict) -> dict:
	"""Remote www: HTTP start_stream / stop_stream with Frappe token auth."""
	key = creds["key"]
	if not key or ":" not in key or key.lower().startswith("bearer"):
		msg = (
			"www_api_key must be a Frappe API key pair api_key:api_secret "
			"when www_site_url is a remote URL"
		)
		logger.error(msg)
		return {
			"success": False,
			"mode": "remote",
			"message": msg,
			"table_id": table_id,
			"live": live,
		}

	method = "domino101.api.dcms.start_stream" if live else "domino101.api.dcms.stop_stream"
	url = f"{creds['base']}/api/method/{method}"
	headers = {
		"Content-Type": "application/json",
		"Authorization": f"token {key}",
	}
	payload = {"stream_id": table_id}
	try:
		response = requests.post(url, headers=headers, json=payload, timeout=20)
		ok = response.ok
		body = {}
		try:
			body = response.json()
		except Exception:
			body = {"text": response.text}
		if not ok:
			logger.error(f"Live Now notify failed {response.status_code}: {body}")
		return {
			"success": ok,
			"mode": "remote",
			"status_code": response.status_code,
			"response": body,
			"live": live,
			"table_id": table_id,
		}
	except requests.RequestException as e:
		logger.error(f"Live Now notify error: {e}")
		return {
			"success": False,
			"mode": "remote",
			"message": str(e),
			"table_id": table_id,
			"live": live,
		}


def notify_live(table_id: str, live: bool) -> dict:
	"""
	Register or clear Live Now for a match table after Domino Stream publish/stop.

	When www_site_url is ``self``, calls apply_table_stream_state in-process
	(co-located SFU). Otherwise HTTP-POSTs www start_stream / stop_stream with
	Authorization: token api_key:api_secret.
	"""
	creds = _www_credentials()
	if not creds["configured"]:
		logger.warning("www_site_url not set; skipping Live Now notify")
		return {"success": False, "skipped": True, "message": "www_site_url not configured"}

	if not table_id:
		return {"success": False, "message": "table_id is required", "live": live}

	if creds["is_self"]:
		try:
			return _notify_live_local(table_id, live)
		except Exception as e:
			logger.exception("Live Now local notify failed for table %s", table_id)
			return {
				"success": False,
				"mode": "local",
				"message": str(e),
				"table_id": table_id,
				"live": live,
			}

	return _notify_live_remote(table_id, live, creds)


def notify_kill_challenge(payload: dict) -> dict:
	"""Fan out kill_challenge on Domino Stream Socket.IO (Redis → stream Node).

	Self-contained: does **not** use Domino101 / dcms publish_dcms_event.
	"""
	if not payload or not payload.get("table_id"):
		return {"success": False, "message": "payload.table_id required"}

	from domino_stream.api.realtime_pub import (
		KILL_CHALLENGE_EVENT,
		get_presence_socket_namespace,
		publisher_room_id,
		publish_stream_event,
	)

	table_id = payload["table_id"]
	room = payload.get("room") or publisher_room_id(table_id)
	payload = {**payload, "room": room}
	try:
		publish_stream_event(KILL_CHALLENGE_EVENT, payload, room=room)
		logger.info(
			"Kill challenge published nsp=%s room=%s table=%s",
			get_presence_socket_namespace(),
			room,
			table_id,
		)
		return {
			"success": True,
			"mode": "stream_socket",
			"room": room,
			"table_id": table_id,
			"namespace": get_presence_socket_namespace(),
		}
	except Exception as e:
		logger.exception("Kill challenge publish failed for table %s", table_id)
		return {"success": False, "mode": "stream_socket", "message": str(e), "table_id": table_id}


def resolve_www_webhook_url() -> str:
	"""Explicit www_webhook_url, or www_site_url + streamer.hooks (not for self)."""
	creds = _www_credentials()
	if creds["webhook"]:
		return creds["webhook"]
	if not creds["configured"] or creds["is_self"]:
		return ""
	return f"{creds['base']}{DEFAULT_HOOKS_PATH}"


def post_www_webhook(table_id: str, *, live: bool, stream_name: str | None = None) -> dict:
	"""POST Ant-shaped payload to www streamer.hooks (guest). Never raises.

	When www is ``self``, hooks are unnecessary — notify_live already updated state.
	"""
	url = resolve_www_webhook_url()
	if not url or not table_id:
		return {"success": False, "skipped": True, "message": "webhook not configured"}

	action = "liveStreamStarted" if live else "liveStreamEnded"
	payload = {
		"id": table_id,
		"action": action,
		"streamName": stream_name or f"Domino Match Table {table_id}",
		"source": "domino_stream",
		"timestamp": int(time.time() * 1000),
	}
	try:
		response = requests.post(url, json=payload, timeout=15)
		ok = response.ok
		if not ok:
			logger.warning(
				"www webhook %s failed %s: %s", action, response.status_code, response.text[:300]
			)
		return {
			"success": ok,
			"status_code": response.status_code,
			"action": action,
			"url": url,
			"table_id": table_id,
		}
	except requests.RequestException as e:
		logger.warning("www webhook error: %s", e)
		return {
			"success": False,
			"message": str(e),
			"action": action,
			"url": url,
			"table_id": table_id,
		}
