# Copyright (c) 2026, Unycode Limited and contributors
# License: MIT

"""Unit tests for Domino Stream publisher liveness / reconcile."""

from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime


class TestPublisherHeartbeatHelpers(FrappeTestCase):
	def test_fresh_heartbeat_within_grace(self):
		from domino_stream.api.room import publisher_heartbeat_fresh

		room = frappe._dict(last_publisher_heartbeat=now_datetime())
		self.assertTrue(publisher_heartbeat_fresh(room, grace=35))

	def test_stale_heartbeat_outside_grace(self):
		from domino_stream.api.room import publisher_heartbeat_fresh

		room = frappe._dict(
			last_publisher_heartbeat=now_datetime() - timedelta(seconds=60)
		)
		self.assertFalse(publisher_heartbeat_fresh(room, grace=35))

	def test_missing_heartbeat_not_fresh(self):
		from domino_stream.api.room import publisher_heartbeat_fresh

		self.assertFalse(publisher_heartbeat_fresh(frappe._dict(), grace=35))


class TestLocalTracksState(FrappeTestCase):
	def test_missing_session_is_dead(self):
		from domino_stream.api.session_health import local_tracks_state

		self.assertTrue(local_tracks_state({"_missing": True}))
		self.assertTrue(local_tracks_state({}))

	def test_active_local_track_is_alive(self):
		from domino_stream.api.session_health import local_tracks_state

		payload = {
			"tracks": [
				{"location": "local", "status": "active", "trackName": "v"},
			]
		}
		self.assertFalse(local_tracks_state(payload))

	def test_active_status_case_insensitive(self):
		from domino_stream.api.session_health import local_tracks_state

		payload = {
			"tracks": [
				{"location": "Local", "status": "Active", "trackName": "v"},
			]
		}
		self.assertFalse(local_tracks_state(payload))

	def test_no_location_local_with_active_track_alive(self):
		from domino_stream.api.session_health import local_tracks_state

		# Cloudflare may omit location — must not false-dead when a track is active
		payload = {"tracks": [{"status": "active", "trackName": "v"}]}
		self.assertFalse(local_tracks_state(payload))

	def test_no_location_local_without_active_is_indeterminate(self):
		from domino_stream.api.session_health import local_tracks_state

		payload = {"tracks": [{"status": "inactive", "trackName": "v"}]}
		self.assertIsNone(local_tracks_state(payload))

	def test_empty_tracks_indeterminate(self):
		from domino_stream.api.session_health import local_tracks_state

		self.assertIsNone(local_tracks_state({"tracks": []}))

	def test_all_local_inactive_is_dead(self):
		from domino_stream.api.session_health import local_tracks_state

		payload = {
			"tracks": [
				{"location": "local", "status": "inactive", "trackName": "v"},
				{"location": "local", "status": "inactive", "trackName": "a"},
			]
		}
		self.assertTrue(local_tracks_state(payload))


class TestReconcileRoomShouldStop(FrappeTestCase):
	@patch("domino_stream.api.reconcile.session_looks_dead", return_value=True)
	def test_stale_heartbeat_and_sfu_dead_stops(self, _dead):
		from domino_stream.api.reconcile import room_should_stop

		row = frappe._dict(
			name="SR-1",
			table_id="TBL-1",
			publisher_session_id="sess-1",
			last_publisher_heartbeat=now_datetime() - timedelta(seconds=90),
			tracks_inactive_since=None,
		)
		should, reason = room_should_stop(row, grace=35)
		self.assertTrue(should)
		self.assertIn("stale_heartbeat_and_sfu_dead", reason)

	@patch("domino_stream.api.reconcile.session_looks_dead", return_value=False)
	def test_stale_heartbeat_but_sfu_alive_does_not_stop(self, _dead):
		from domino_stream.api.reconcile import room_should_stop

		row = frappe._dict(
			name="SR-1",
			table_id="TBL-1",
			publisher_session_id="sess-1",
			last_publisher_heartbeat=now_datetime() - timedelta(seconds=90),
			tracks_inactive_since=None,
		)
		should, reason = room_should_stop(row, grace=35)
		self.assertFalse(should)
		self.assertIn("stale_heartbeat_but_sfu_alive", reason)

	@patch("domino_stream.api.reconcile.session_looks_dead", return_value=False)
	def test_fresh_heartbeat_healthy(self, _dead):
		from domino_stream.api.reconcile import room_should_stop

		row = frappe._dict(
			name="SR-1",
			table_id="TBL-1",
			publisher_session_id="sess-1",
			last_publisher_heartbeat=now_datetime(),
			tracks_inactive_since=None,
		)
		should, reason = room_should_stop(row, grace=35)
		self.assertFalse(should)
		self.assertEqual(reason, "healthy")

	@patch("domino_stream.api.reconcile.session_looks_dead", return_value=None)
	def test_stale_heartbeat_indeterminate_sfu_does_not_stop(self, _dead):
		from domino_stream.api.reconcile import room_should_stop

		row = frappe._dict(
			name="SR-1",
			table_id="TBL-1",
			publisher_session_id="sess-1",
			last_publisher_heartbeat=now_datetime() - timedelta(seconds=90),
			tracks_inactive_since=None,
		)
		should, reason = room_should_stop(row, grace=35)
		self.assertFalse(should)
		self.assertIn("indeterminate", reason)

	@patch("domino_stream.api.reconcile.session_looks_dead", return_value=True)
	@patch("domino_stream.api.reconcile._mark_tracks_inactive")
	def test_inactive_tracks_mark_then_stop(self, mock_mark, _dead):
		from domino_stream.api.reconcile import room_should_stop

		row = frappe._dict(
			name="SR-1",
			table_id="TBL-1",
			publisher_session_id="sess-1",
			last_publisher_heartbeat=now_datetime(),
			tracks_inactive_since=None,
		)
		should, reason = room_should_stop(row, grace=35)
		self.assertFalse(should)
		self.assertEqual(reason, "tracks_inactive_marked")
		mock_mark.assert_called_once()

		row.tracks_inactive_since = now_datetime() - timedelta(seconds=90)
		should2, reason2 = room_should_stop(row, grace=35)
		self.assertTrue(should2)
		self.assertIn("tracks_inactive", reason2)

	@patch("domino_stream.api.reconcile.session_looks_dead", return_value=None)
	def test_fresh_hb_no_local_tracks_does_not_stop(self, _dead):
		"""Indeterminate SFU (e.g. no location=local) must not kill a fresh-HB room."""
		from domino_stream.api.reconcile import room_should_stop

		row = frappe._dict(
			name="SR-1",
			table_id="TBL-1",
			publisher_session_id="sess-1",
			last_publisher_heartbeat=now_datetime(),
			tracks_inactive_since=None,
		)
		should, reason = room_should_stop(row, grace=35)
		self.assertFalse(should)
		self.assertEqual(reason, "session_check_indeterminate")


class TestReconcileKillChallenge(FrappeTestCase):
	@patch("domino_stream.api.reconcile.session_looks_dead", return_value=True)
	def test_candidate_with_socket_opens_challenge(self, _dead):
		from domino_stream.api.reconcile import _room_decision

		row = frappe._dict(
			name="SR-1",
			table_id="TBL-1",
			publisher_session_id="sess-1",
			last_publisher_heartbeat=now_datetime() - timedelta(seconds=90),
			tracks_inactive_since=None,
			publisher_socket_present=1,
			kill_challenge_id=None,
			kill_challenge_expires_at=None,
			kill_challenge_reason=None,
		)
		decision = _room_decision(row, grace=35)
		self.assertFalse(decision["should_stop"])
		self.assertTrue(decision["open_challenge"])
		self.assertIn("challenge_candidate", decision["reason"])

	@patch("domino_stream.api.reconcile.session_looks_dead", return_value=True)
	def test_candidate_without_socket_stops_immediately(self, _dead):
		from domino_stream.api.reconcile import _room_decision

		row = frappe._dict(
			name="SR-1",
			table_id="TBL-1",
			publisher_session_id="sess-1",
			last_publisher_heartbeat=now_datetime() - timedelta(seconds=90),
			tracks_inactive_since=None,
			publisher_socket_present=0,
			kill_challenge_id=None,
			kill_challenge_expires_at=None,
			kill_challenge_reason=None,
		)
		decision = _room_decision(row, grace=35)
		self.assertTrue(decision["should_stop"])
		self.assertFalse(decision["open_challenge"])
		self.assertIn("stale_heartbeat_and_sfu_dead", decision["reason"])

	def test_pending_challenge_does_not_stop(self):
		from domino_stream.api.reconcile import _room_decision

		row = frappe._dict(
			name="SR-1",
			table_id="TBL-1",
			publisher_session_id="sess-1",
			last_publisher_heartbeat=now_datetime() - timedelta(seconds=90),
			tracks_inactive_since=None,
			publisher_socket_present=1,
			kill_challenge_id="chal-1",
			kill_challenge_expires_at=now_datetime() + timedelta(seconds=60),
			kill_challenge_reason="stale",
		)
		decision = _room_decision(row, grace=35)
		self.assertFalse(decision["should_stop"])
		self.assertFalse(decision["open_challenge"])
		self.assertIn("challenge_pending", decision["reason"])

	def test_expired_challenge_stops(self):
		from domino_stream.api.reconcile import _room_decision

		row = frappe._dict(
			name="SR-1",
			table_id="TBL-1",
			publisher_session_id="sess-1",
			last_publisher_heartbeat=now_datetime() - timedelta(seconds=90),
			tracks_inactive_since=None,
			publisher_socket_present=1,
			kill_challenge_id="chal-1",
			kill_challenge_expires_at=now_datetime() - timedelta(seconds=5),
			kill_challenge_reason="stale",
		)
		decision = _room_decision(row, grace=35)
		self.assertTrue(decision["should_stop"])
		self.assertFalse(decision["open_challenge"])
		self.assertIn("kill_challenge_expired", decision["reason"])


class TestBuildCloseTrackEntries(FrappeTestCase):
	def test_includes_mids_when_present(self):
		from domino_stream.api.room import _build_close_track_entries

		room = frappe._dict(
			video_track_name="table-video-x",
			audio_track_name="table-audio-x",
			video_mid="0",
			audio_mid="1",
		)
		entries = _build_close_track_entries(room)
		self.assertEqual(
			entries,
			[
				{"trackName": "table-video-x", "mid": "0"},
				{"trackName": "table-audio-x", "mid": "1"},
			],
		)

	def test_omits_mid_when_missing(self):
		from domino_stream.api.room import _build_close_track_entries

		room = frappe._dict(
			video_track_name="table-video-x",
			audio_track_name=None,
			video_mid=None,
			audio_mid=None,
		)
		entries = _build_close_track_entries(room)
		self.assertEqual(entries, [{"trackName": "table-video-x"}])


class TestLiveNowWebhookResolve(FrappeTestCase):
	@patch("domino_stream.api.live_now._www_credentials")
	def test_explicit_webhook_url(self, mock_creds):
		from domino_stream.api.live_now import resolve_www_webhook_url

		mock_creds.return_value = {
			"base": "https://www.domino101.com",
			"key": "",
			"webhook": "https://www.domino101.com/api/method/domino101.api.streamer.hooks",
			"is_self": False,
			"configured": True,
		}
		self.assertIn("streamer.hooks", resolve_www_webhook_url())

	@patch("domino_stream.api.live_now._www_credentials")
	def test_self_skips_webhook(self, mock_creds):
		from domino_stream.api.live_now import resolve_www_webhook_url

		mock_creds.return_value = {
			"base": "self",
			"key": "",
			"webhook": "",
			"is_self": True,
			"configured": True,
		}
		self.assertEqual(resolve_www_webhook_url(), "")


class TestNotifyKillChallengeStreamSocket(FrappeTestCase):
	@patch("domino_stream.api.realtime_pub.publish_stream_event")
	@patch(
		"domino_stream.api.realtime_pub.get_presence_socket_namespace",
		return_value="domino-stream",
	)
	def test_notify_uses_stream_redis_not_dcms(self, _ns, mock_publish):
		from domino_stream.api.live_now import notify_kill_challenge

		payload = {
			"table_id": "TBL-1",
			"challenge_id": "chal-x",
			"reason": "stale",
			"grace_seconds": 150,
		}
		result = notify_kill_challenge(payload)
		self.assertTrue(result.get("success"))
		self.assertEqual(result.get("mode"), "stream_socket")
		self.assertEqual(result.get("room"), "publisher:TBL-1")
		mock_publish.assert_called_once()
		args, kwargs = mock_publish.call_args
		self.assertEqual(args[0], "kill_challenge")
		self.assertEqual(kwargs.get("room") or args[2], "publisher:TBL-1")


class TestGetSessionMissingStatuses(FrappeTestCase):
	@patch("domino_stream.api.sfu_client.requests.get")
	@patch("domino_stream.api.sfu_client.get_sfu_credentials")
	def test_410_disconnected_is_missing(self, mock_creds, mock_get):
		from domino_stream.api.sfu_client import get_session
		from domino_stream.api.session_health import local_tracks_state

		mock_creds.return_value = {
			"app_id": "app",
			"app_secret": "sec",
			"configured": True,
		}
		resp = mock_get.return_value
		resp.status_code = 410
		resp.content = b'{"errorDescription":"Session appears to be disconnected"}'
		resp.json.return_value = {
			"errorDescription": "Session appears to be disconnected"
		}
		payload = get_session("sess-gone")
		self.assertTrue(payload.get("_missing"))
		self.assertEqual(payload.get("_http_status"), 410)
		self.assertTrue(local_tracks_state(payload))

	@patch("domino_stream.api.sfu_client.requests.get")
	@patch("domino_stream.api.sfu_client.get_sfu_credentials")
	def test_404_still_missing(self, mock_creds, mock_get):
		from domino_stream.api.sfu_client import get_session

		mock_creds.return_value = {
			"app_id": "app",
			"app_secret": "sec",
			"configured": True,
		}
		resp = mock_get.return_value
		resp.status_code = 404
		resp.content = b"{}"
		resp.json.return_value = {}
		payload = get_session("sess-missing")
		self.assertTrue(payload.get("_missing"))
		self.assertEqual(payload.get("_http_status"), 404)


class TestKillChallengeSelfEnforce(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.flags.domino_stream_kill_jobs = []

	def tearDown(self):
		frappe.flags.domino_stream_kill_jobs = []
		super().tearDown()

	def test_schedule_records_job_in_test(self):
		from domino_stream.api.room import _schedule_kill_challenge_expiry

		expires = now_datetime() + timedelta(seconds=150)
		_schedule_kill_challenge_expiry("TBL-1", "chal-abc", expires)
		jobs = frappe.flags.domino_stream_kill_jobs
		self.assertEqual(len(jobs), 1)
		self.assertEqual(jobs[0]["table_id"], "TBL-1")
		self.assertEqual(jobs[0]["challenge_id"], "chal-abc")
		self.assertIn("force_stop_if_challenge_expired", jobs[0]["method"])

	@patch("domino_stream.api.live_now.notify_kill_challenge")
	def test_open_kill_challenge_schedules_expiry(self, mock_notify):
		from domino_stream.api.room import open_kill_challenge

		frappe.flags.domino_stream_kill_jobs = []
		with patch("domino_stream.api.room.frappe.db.set_value"), patch(
			"domino_stream.api.room.frappe.db.commit"
		), patch("domino_stream.api.room.frappe.generate_hash", return_value="chal-test1"):
			room = frappe._dict(name="SR-1", table_id="TBL-1")
			payload = open_kill_challenge(room, "publisher_socket_gone")
		self.assertEqual(payload["challenge_id"], "chal-test1")
		self.assertEqual(payload["reason"], "publisher_socket_gone")
		jobs = frappe.flags.domino_stream_kill_jobs
		self.assertEqual(len(jobs), 1)
		self.assertEqual(jobs[0]["challenge_id"], "chal-test1")
		mock_notify.assert_called_once()

	@patch("domino_stream.api.room.open_kill_challenge")
	@patch("domino_stream.api.room.require_stream_access")
	def test_presence_offline_opens_challenge_when_live(self, _auth, mock_open):
		from domino_stream.api.room import publisher_presence_offline

		mock_open.return_value = {"challenge_id": "c1", "reason": "publisher_socket_gone"}
		with patch(
			"domino_stream.api.room.frappe.db.get_value",
			return_value=frappe._dict(
				name="SR-1",
				status="Live",
				table_id="TBL-1",
				kill_challenge_id=None,
				kill_challenge_expires_at=None,
			),
		), patch("domino_stream.api.room.frappe.db.set_value"), patch(
			"domino_stream.api.room.frappe.db.commit"
		):
			result = publisher_presence_offline("TBL-1")
		self.assertTrue(result["cleared"])
		self.assertTrue(result["challenge_opened"])
		mock_open.assert_called_once()
		self.assertEqual(mock_open.call_args[0][1], "publisher_socket_gone")

	@patch("domino_stream.api.room.open_kill_challenge")
	@patch("domino_stream.api.room.require_stream_access")
	def test_presence_offline_does_not_reset_pending_challenge(self, _auth, mock_open):
		from domino_stream.api.room import publisher_presence_offline

		with patch(
			"domino_stream.api.room.frappe.db.get_value",
			return_value=frappe._dict(
				name="SR-1",
				status="Live",
				table_id="TBL-1",
				kill_challenge_id="chal-pending",
				kill_challenge_expires_at=now_datetime() + timedelta(seconds=60),
			),
		), patch("domino_stream.api.room.frappe.db.set_value"), patch(
			"domino_stream.api.room.frappe.db.commit"
		):
			result = publisher_presence_offline("TBL-1")
		self.assertTrue(result["cleared"])
		self.assertFalse(result["challenge_opened"])
		mock_open.assert_not_called()

	@patch("domino_stream.api.room._stop_room_internal")
	def test_force_stop_when_challenge_still_pending(self, mock_stop):
		from domino_stream.api.room import force_stop_if_challenge_expired

		doc = frappe._dict(name="SR-1", table_id="TBL-1", status="Live")
		with patch(
			"domino_stream.api.room.frappe.db.get_value",
			return_value=frappe._dict(
				name="SR-1",
				status="Live",
				kill_challenge_id="chal-1",
			),
		), patch("domino_stream.api.room.frappe.get_doc", return_value=doc):
			result = force_stop_if_challenge_expired("TBL-1", "chal-1")
		self.assertTrue(result["stopped"])
		mock_stop.assert_called_once()
		self.assertEqual(mock_stop.call_args[1].get("reason"), "kill_challenge_expired")

	@patch("domino_stream.api.room._stop_room_internal")
	def test_force_stop_noops_when_keep_alive_cleared_challenge(self, mock_stop):
		from domino_stream.api.room import force_stop_if_challenge_expired

		with patch(
			"domino_stream.api.room.frappe.db.get_value",
			return_value=frappe._dict(
				name="SR-1",
				status="Live",
				kill_challenge_id=None,
			),
		):
			result = force_stop_if_challenge_expired("TBL-1", "chal-1")
		self.assertFalse(result["stopped"])
		self.assertEqual(result["reason"], "challenge_cleared")
		mock_stop.assert_not_called()
