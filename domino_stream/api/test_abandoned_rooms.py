"""Pure tests: abandoned-room safety net and the presence-token Room fallback (no DB writes)."""

from datetime import datetime, timedelta
from unittest import TestCase
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import frappe

from domino_stream.api import realtime_pub, reconcile

NOW = datetime(2026, 11, 11, 17, 0, 0)


def _row(**kw):
	row = {
		"name": "r1",
		"table_id": "TBL-1",
		"modified": NOW - timedelta(hours=1),
		"last_publisher_heartbeat": NOW - timedelta(minutes=10),
		"publisher_session_id": "sess",
		"kill_challenge_id": None,
		"kill_challenge_expires_at": None,
		"kill_challenge_reason": None,
	}
	row.update(kw)
	return row


def _dead(_sid):
	return True


class TestAbandonedRoomReason(TestCase):
	def setUp(self):
		self._patches = [
			patch("domino_stream.api.reconcile.now_datetime", return_value=NOW),
			patch("domino_stream.api.room.now_datetime", return_value=NOW),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()

	def test_stale_heartbeat_and_dead_session_is_challenged(self):
		reason = reconcile.abandoned_room_reason(_row(), 180, _dead)
		self.assertIn("abandoned", reason)
		self.assertIn("600s", reason)

	def test_recent_heartbeat_is_left_alone(self):
		row = _row(last_publisher_heartbeat=NOW - timedelta(seconds=60))
		self.assertIsNone(reconcile.abandoned_room_reason(row, 180, _dead))

	def test_alive_or_unknown_session_is_left_alone(self):
		self.assertIsNone(reconcile.abandoned_room_reason(_row(), 180, lambda _s: False))
		self.assertIsNone(reconcile.abandoned_room_reason(_row(), 180, lambda _s: None))

	def test_pending_challenge_is_not_reopened(self):
		row = _row(kill_challenge_id="c1", kill_challenge_expires_at=NOW + timedelta(seconds=20))
		self.assertIsNone(reconcile.abandoned_room_reason(row, 180, _dead))

	def test_expired_challenge_on_a_live_room_is_reopened(self):
		row = _row(kill_challenge_id="c1", kill_challenge_expires_at=NOW - timedelta(minutes=5))
		self.assertIsNotNone(reconcile.abandoned_room_reason(row, 180, _dead))

	def test_never_heartbeated_room_uses_its_last_change(self):
		row = _row(last_publisher_heartbeat=None, modified=NOW - timedelta(minutes=30))
		self.assertIsNotNone(reconcile.abandoned_room_reason(row, 180, _dead))
		row = _row(last_publisher_heartbeat=None, modified=NOW - timedelta(seconds=30))
		self.assertIsNone(reconcile.abandoned_room_reason(row, 180, _dead))


class _Cache:
	def __init__(self, data=None):
		self.data = data or {}

	def get_value(self, key):
		return self.data.get(key)


class _Row(dict):
	__getattr__ = dict.get


@contextmanager
def _fake_db():
	"""frappe.db is a proxy to frappe.local.db; bind a mock there (no site needed)."""
	had = hasattr(frappe.local, "db")
	old = getattr(frappe.local, "db", None)
	frappe.local.db = MagicMock()
	try:
		yield frappe.local.db
	finally:
		if had:
			frappe.local.db = old
		else:
			del frappe.local.db


class TestPresenceTokenFallback(TestCase):
	def test_redis_hit_wins(self):
		key = realtime_pub._presence_token_cache_key("tok")
		cache = _Cache({key: {"table_id": "TBL-1", "session_id": "s"}})
		with patch("domino_stream.api.realtime_pub.frappe.cache", return_value=cache):
			self.assertEqual(realtime_pub.resolve_presence_token("tok")["table_id"], "TBL-1")

	def test_missing_in_redis_falls_back_to_room_hash(self):
		calls = {}

		def get_value(doctype, filters, fields, as_dict=False):
			calls["filters"] = filters
			return _Row(table_id="TBL-2", publisher_session_id="s2")

		with (
			patch("domino_stream.api.realtime_pub.frappe.cache", return_value=_Cache()),
			_fake_db() as db,
		):
			db.has_column.return_value = True
			db.get_value.side_effect = get_value
			data = realtime_pub.resolve_presence_token("tok")
		self.assertEqual(data["table_id"], "TBL-2")
		self.assertEqual(data["source"], "room")
		self.assertEqual(calls["filters"], {"presence_token_hash": realtime_pub.presence_token_hash("tok")})

	def test_unknown_everywhere_is_none(self):
		with (
			patch("domino_stream.api.realtime_pub.frappe.cache", return_value=_Cache()),
			_fake_db() as db,
		):
			db.has_column.return_value = True
			db.get_value.return_value = None
			self.assertIsNone(realtime_pub.resolve_presence_token("tok"))

	def test_hash_is_stable_and_not_the_token(self):
		h = realtime_pub.presence_token_hash("tok")
		self.assertEqual(h, realtime_pub.presence_token_hash("tok"))
		self.assertNotIn("tok", h)
		self.assertEqual(len(h), 64)
