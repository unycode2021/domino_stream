# Copyright (c) 2026, Unycode Limited and contributors
# License: MIT
"""Simulcast layer choice for spectator pulls (Cloudflare Realtime SFU).

Publishers send RIDs ``f`` (720p), ``h`` (360p) and ``q`` (180p) through ``sendEncodings``.
A remote (pull) track picks a layer with Cloudflare's ``simulcast`` object on
``tracks/new`` / ``tracks/update``:

    {"preferredRid": "h", "priorityOrdering": "asciibetical", "ridNotAvailable": "asciibetical"}

``asciibetical`` sorts RIDs a-z with ``a`` most desirable, so ``f`` < ``h`` < ``q`` falls back
from high to low when bandwidth is short or the preferred layer stops. See
https://developers.cloudflare.com/realtime/sfu/simulcast/ and the OpenAPI schema
https://developers.cloudflare.com/realtime/static/realtime-api-2024-05-21.yaml.

Older publishers send one layer and advertise no RIDs; their pulls carry no ``simulcast``
object, so every viewer (guests included) gets that one layer.

Pure helpers, no database.
"""

from __future__ import annotations

import re

GUEST_RID = "h"
USER_RID = "f"
ORDERING = "asciibetical"

_RID_LINE = re.compile(r"^a=rid:([A-Za-z0-9_-]+)\s+send\b")


def parse_send_rids(sdp: str | None) -> list[str]:
	"""RIDs the publisher's offer advertises for its video section (``a=rid:<id> send``)."""
	if not sdp:
		return []
	rids: list[str] = []
	in_video = False
	for raw in str(sdp).splitlines():
		line = raw.strip()
		if line.startswith("m="):
			in_video = line.startswith("m=video")
			continue
		if not in_video:
			continue
		match = _RID_LINE.match(line)
		if match and match.group(1) not in rids:
			rids.append(match.group(1))
	return rids


def rids_to_field(rids) -> str:
	return ",".join(r for r in (rids or []) if r)


def rids_from_field(value) -> list[str]:
	return [part.strip() for part in str(value or "").split(",") if part.strip()]


def preferred_rid(viewer_kind: str | None, advertised) -> str | None:
	"""The layer to ask for. None when the publisher is single-layer.

	Guests want ``h``; everyone else wants ``f``. When that RID is not advertised, a guest
	takes the next lower layer (or the lowest-quality one there is), a signed-in viewer the
	best one there is.
	"""
	rids = sorted({str(r) for r in (advertised or []) if r})
	if not rids:
		return None
	want = GUEST_RID if viewer_kind == "guest" else USER_RID
	if want in rids:
		return want
	if viewer_kind == "guest":
		lower = [r for r in rids if r > want]
		return lower[0] if lower else rids[-1]
	return rids[0]


def simulcast_for(viewer_kind: str | None, advertised) -> dict | None:
	"""Cloudflare ``simulcast`` object for the video pull entry, or None for single-layer."""
	rid = preferred_rid(viewer_kind, advertised)
	if not rid:
		return None
	return {"preferredRid": rid, "priorityOrdering": ORDERING, "ridNotAvailable": ORDERING}
