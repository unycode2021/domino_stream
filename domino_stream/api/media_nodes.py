"""Media node registry: which MediaMTX / relay host serves a path or relay.

``domino_media_agent`` (see ``domino_stream/media_agent.py``) writes one
heartbeat key per media node. Gunicorn picks the least-loaded node when it
opens a WHIP path or a chunk relay and stores the choice, so later proxy,
status, close and chunk writes reach the same node.

All keys are plain JSON in redis_queue. dominoio reads ``domino_media_relay:*``
to stream chunks, so these keys must stay unprefixed and unpickled.

With no agent running the registry is empty and every call falls back to the
local MediaMTX on 127.0.0.1, which is how a single bench has always worked.
"""

from __future__ import annotations

import json

import frappe
import requests

NODE_PREFIX = "domino_media_node:"
PATH_ROUTE_PREFIX = "domino_media_path:"
RELAY_ROUTE_PREFIX = "domino_media_relay:"
LOCAL_NODE_ID = "local"
ROUTE_TTL_SECONDS = 12 * 60 * 60
AGENT_TOKEN_HEADER = "X-Domino-Media-Token"
_AGENT_TIMEOUT = 10


def _redis():
	from frappe.utils.background_jobs import get_redis_conn

	return get_redis_conn()


def _loads(raw) -> dict | None:
	if not raw:
		return None
	try:
		data = json.loads(raw)
	except (TypeError, ValueError):
		return None
	return data if isinstance(data, dict) else None


def local_node() -> dict:
	"""MediaMTX on this host, used when no agent has registered."""
	conf = frappe.conf
	return {
		"id": LOCAL_NODE_ID,
		"mediamtx_api": (conf.get("domino_mediamtx_api") or "http://127.0.0.1:9997").rstrip("/"),
		"whip_upstream": (conf.get("domino_mediamtx_whip") or "http://127.0.0.1:8889").rstrip("/"),
		"agent_url": "",
		"chunk_host": "",
		"chunk_port": 0,
		"public_host": "",
		"paths": 0,
		"relays": 0,
	}


def list_nodes() -> list[dict]:
	"""Live nodes. A node whose heartbeat expired is not listed."""
	try:
		conn = _redis()
		keys = list(conn.scan_iter(match=f"{NODE_PREFIX}*", count=100))
		values = conn.mget(keys) if keys else []
	except Exception:
		return []
	nodes = []
	for raw in values:
		node = _loads(raw)
		if node and node.get("id"):
			nodes.append(node)
	return nodes


def nodes_or_local() -> list[dict]:
	return list_nodes() or [local_node()]


def pick_node() -> dict:
	"""Least-loaded live node, or the local MediaMTX when none registered."""
	nodes = list_nodes()
	if not nodes:
		return local_node()
	return min(nodes, key=lambda n: (int(n.get("paths") or 0) + int(n.get("relays") or 0), str(n["id"])))


def get_node(node_id: str | None) -> dict | None:
	"""Node by id. ``local`` (or empty) is the 127.0.0.1 fallback. None when gone."""
	if not node_id or node_id == LOCAL_NODE_ID:
		return local_node()
	try:
		return _loads(_redis().get(f"{NODE_PREFIX}{node_id}"))
	except Exception:
		return None


def _set_route(prefix: str, name: str, payload: dict) -> None:
	_redis().set(f"{prefix}{name}", json.dumps(payload), ex=ROUTE_TTL_SECONDS)


def _get_route(prefix: str, name: str) -> dict | None:
	try:
		return _loads(_redis().get(f"{prefix}{name}"))
	except Exception:
		return None


def _drop_route(prefix: str, name: str) -> None:
	try:
		_redis().delete(f"{prefix}{name}")
	except Exception:
		pass


def remember_path_node(path_name: str, node: dict) -> None:
	_set_route(PATH_ROUTE_PREFIX, path_name, {"node_id": node.get("id") or LOCAL_NODE_ID})


def path_node(path_name: str) -> dict | None:
	"""Node that holds this MediaMTX path. Paths opened before nodes existed are local."""
	route = _get_route(PATH_ROUTE_PREFIX, path_name)
	if not route:
		return local_node()
	return get_node(route.get("node_id"))


def forget_path_node(path_name: str) -> None:
	_drop_route(PATH_ROUTE_PREFIX, path_name)


def remember_relay_node(relay_id: str, node: dict) -> None:
	"""dominoio reads this route to send chunks straight to the node's agent."""
	_set_route(
		RELAY_ROUTE_PREFIX,
		relay_id,
		{
			"node_id": node.get("id") or LOCAL_NODE_ID,
			"chunk_host": node.get("chunk_host") or "",
			"chunk_port": int(node.get("chunk_port") or 0),
		},
	)


def relay_route(relay_id: str) -> dict | None:
	return _get_route(RELAY_ROUTE_PREFIX, relay_id)


def forget_relay_node(relay_id: str) -> None:
	_drop_route(RELAY_ROUTE_PREFIX, relay_id)


def agent_token() -> str:
	return (frappe.conf.get("domino_media_agent_token") or "").strip()


def agent_request(node: dict, method: str, path: str, payload: dict | None = None, timeout: int = _AGENT_TIMEOUT):
	"""Call a node's agent. Returns the JSON body, or None when the agent is unreachable."""
	base = (node.get("agent_url") or "").rstrip("/")
	if not base:
		return None
	try:
		response = requests.request(
			method,
			f"{base}{path}",
			json=payload,
			headers={AGENT_TOKEN_HEADER: agent_token()},
			timeout=timeout,
		)
	except requests.RequestException:
		return None
	try:
		data = response.json()
	except ValueError:
		data = {}
	if not isinstance(data, dict):
		data = {}
	data.setdefault("ok", response.ok)
	return data
