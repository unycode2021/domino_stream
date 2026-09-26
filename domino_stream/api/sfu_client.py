"""Cloudflare Realtime SFU HTTP client.

API base: https://rtc.live.cloudflare.com/v1/apps/{appId}
Auth: Authorization: Bearer {appSecret}

Secrets come from Stream Settings on this site — never returned to clients.
"""

from __future__ import annotations

import frappe
import requests
from frappe import _

SFU_API_BASE = "https://rtc.live.cloudflare.com/v1/apps/{app_id}"

logger = frappe.logger("domino_stream_sfu", allow_site=True, file_count=20)


def get_sfu_credentials() -> dict:
	settings = frappe.get_single("Stream Settings")
	app_id = (settings.get("cloudflare_realtime_app_id") or "").strip()
	secret = ""
	try:
		secret = (settings.get_password("cloudflare_realtime_app_secret", raise_exception=False) or "").strip()
	except Exception:
		secret = ""
	return {
		"app_id": app_id,
		"app_secret": secret,
		"configured": bool(app_id and secret),
	}


def _headers(secret: str) -> dict:
	return {
		"Authorization": f"Bearer {secret}",
		"Content-Type": "application/json",
	}


def _url(app_id: str, path: str) -> str:
	return f"{SFU_API_BASE.format(app_id=app_id)}{path}"


def _request(method: str, path: str, json_body: dict | None = None) -> dict:
	creds = get_sfu_credentials()
	if not creds["configured"]:
		frappe.throw(
			_(
				"Cloudflare Realtime SFU is not configured. "
				"Set App ID and App Secret on Stream Settings."
			)
		)
	url = _url(creds["app_id"], path)
	kwargs = {
		"headers": _headers(creds["app_secret"]),
		"timeout": 30,
	}
	if method.upper() != "GET":
		kwargs["json"] = json_body if json_body is not None else {}
	try:
		response = requests.request(method, url, **kwargs)
	except requests.RequestException as e:
		logger.error(f"SFU request failed {method} {path}: {e}")
		frappe.throw(_("SFU request failed: {0}").format(str(e)))

	try:
		data = response.json() if response.content else {}
	except Exception:
		data = {"raw": response.text}

	if response.status_code >= 400:
		logger.error(f"SFU error {response.status_code} {method} {path}: {data}")
		try:
			from domino_stream.api.events import emit_stream_event

			emit_stream_event(
				"sfu_error",
				f"SFU {response.status_code} {method} {path}",
				severity="Error",
				detail={"status": response.status_code, "method": method, "path": path, "body": data},
			)
		except Exception:
			pass
		frappe.throw(
			_("SFU API error {0}: {1}").format(response.status_code, data)
		)

	data["_http_status"] = response.status_code
	return data


def create_session(session_description: dict | None = None) -> dict:
	"""POST /sessions/new — requires offer sessionDescription; returns sessionId + answer."""
	if not session_description or not session_description.get("sdp"):
		frappe.throw(_("sessionDescription with sdp is required for SFU sessions/new"))
	return _request(
		"POST",
		"/sessions/new",
		{"sessionDescription": session_description},
	)


def add_tracks(session_id: str, tracks: list, session_description: dict | None = None) -> dict:
	"""POST /sessions/{id}/tracks/new"""
	body = {"tracks": tracks}
	if session_description:
		body["sessionDescription"] = session_description
	return _request("POST", f"/sessions/{session_id}/tracks/new", body)


def renegotiate(session_id: str, session_description: dict) -> dict:
	"""PUT /sessions/{id}/renegotiate"""
	return _request(
		"PUT",
		f"/sessions/{session_id}/renegotiate",
		{"sessionDescription": session_description},
	)


def close_tracks(session_id: str, tracks: list | None = None, force: bool = True) -> dict:
	"""PUT /sessions/{id}/tracks/close — Cloudflare requires a tracks array."""
	body = {"tracks": tracks or [], "force": force}
	return _request("PUT", f"/sessions/{session_id}/tracks/close", body)


def get_session(session_id: str) -> dict:
	"""GET /sessions/{id}. Missing/disconnected sessions return ``_missing``.

	Cloudflare returns **404** when unknown and **410** when the PeerConnection
	has disconnected — both mean the publisher session is gone for stop health.
	"""
	creds = get_sfu_credentials()
	if not creds["configured"]:
		frappe.throw(
			_(
				"Cloudflare Realtime SFU is not configured. "
				"Set App ID and App Secret on Stream Settings."
			)
		)
	url = _url(creds["app_id"], f"/sessions/{session_id}")
	try:
		response = requests.get(
			url, headers=_headers(creds["app_secret"]), timeout=30
		)
	except requests.RequestException as e:
		logger.error(f"SFU get_session failed {session_id}: {e}")
		frappe.throw(_("SFU request failed: {0}").format(str(e)))

	try:
		data = response.json() if response.content else {}
	except Exception:
		data = {"raw": response.text}

	if response.status_code in (404, 410):
		return {
			"_missing": True,
			"tracks": [],
			"_http_status": response.status_code,
			"_error": data,
		}
	if response.status_code >= 400:
		logger.error(f"SFU error {response.status_code} GET session {session_id}: {data}")
		frappe.throw(
			_("SFU API error {0}: {1}").format(response.status_code, data)
		)
	data["_http_status"] = response.status_code
	return data



def check_configured() -> dict:
	creds = get_sfu_credentials()
	if not creds["configured"]:
		return {
			"ready": False,
			"status": "not_configured",
			"message": "Stream Settings missing Realtime App ID/Secret",
		}
	# Credential presence is enough; do not probe sessions/new (requires a real offer SDP)
	return {
		"ready": True,
		"status": "configured",
		"message": "Cloudflare Realtime SFU credentials present",
	}
