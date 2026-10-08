"""Cloudflare Realtime HTTP client for ODP board voice.

Uses ODP Voice Settings only. Never reads Stream Settings or the match app secret.
"""

from __future__ import annotations

import frappe
import requests
from frappe import _

SFU_API_BASE = "https://rtc.live.cloudflare.com/v1/apps/{app_id}"

logger = frappe.logger("odp_voice_sfu", allow_site=True, file_count=20)


def get_voice_credentials() -> dict:
	try:
		settings = frappe.get_single("ODP Voice Settings")
	except Exception:
		return {"app_id": "", "app_secret": "", "configured": False}
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
	creds = get_voice_credentials()
	if not creds["configured"]:
		frappe.throw(
			_(
				"ODP voice is not configured. "
				"Set the ODP Voice App ID and App Secret on ODP Voice Settings."
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
		logger.error("ODP voice request failed %s %s: %s", method, path, e)
		frappe.throw(_("ODP voice request failed"))

	try:
		data = response.json() if response.content else {}
	except Exception:
		data = {}
	if not isinstance(data, dict):
		data = {}

	if response.status_code >= 400:
		logger.error("ODP voice error %s %s %s: %s", response.status_code, method, path, data)
		frappe.throw(_("ODP voice could not reach the voice service ({0})").format(response.status_code))

	data["_http_status"] = response.status_code
	return data


def create_session(session_description: dict | None = None) -> dict:
	if not session_description or not session_description.get("sdp"):
		frappe.throw(_("A voice offer is required"))
	return _request("POST", "/sessions/new", {"sessionDescription": session_description})


def add_tracks(session_id: str, tracks: list, session_description: dict | None = None) -> dict:
	body = {"tracks": tracks}
	if session_description:
		body["sessionDescription"] = session_description
	return _request("POST", f"/sessions/{session_id}/tracks/new", body)


def renegotiate(session_id: str, session_description: dict) -> dict:
	return _request(
		"PUT",
		f"/sessions/{session_id}/renegotiate",
		{"sessionDescription": session_description},
	)


def close_tracks(session_id: str, tracks: list | None = None, force: bool = True) -> dict:
	body = {"tracks": tracks or [], "force": force}
	return _request("PUT", f"/sessions/{session_id}/tracks/close", body)
