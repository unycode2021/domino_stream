"""Install hooks for Domino Stream."""

import frappe

RECONCILE_METHOD = "domino_stream.api.reconcile.reconcile_live_rooms"
RECONCILE_CRON = "* * * * *"


def ensure_reconcile_minute_cron():
	"""Stop leftover reconcile Scheduled Job Types.

	Publisher force-kill is owned by kill_challenge + RQ delayed jobs
	(``force_stop_if_challenge_expired``), not ``reconcile_live_rooms``.
	Keep any legacy job Stopped so migrate/sync cannot revive abandon polling.
	"""
	for name in frappe.get_all(
		"Scheduled Job Type",
		filters={"method": RECONCILE_METHOD},
		pluck="name",
	):
		doc = frappe.get_doc("Scheduled Job Type", name)
		# Drop non-cron leftovers; always leave stopped
		if doc.frequency != "Cron" or (doc.cron_format or "") != RECONCILE_CRON:
			frappe.delete_doc("Scheduled Job Type", name, force=True, ignore_permissions=True)
			continue
		if not doc.stopped:
			frappe.db.set_value("Scheduled Job Type", name, "stopped", 1)


def ensure_kill_challenge_grace_default():
	"""Prefill the old shipped default of 150 with 20.

	Any other saved value is left alone — Stream Settings wins after that.
	"""
	if not frappe.db.exists("DocType", "Stream Settings"):
		return
	current = frappe.db.get_single_value("Stream Settings", "kill_challenge_grace_seconds")
	if str(current) == "150":
		frappe.db.set_single_value("Stream Settings", "kill_challenge_grace_seconds", 20)


def after_install():
	try:
		from domino_stream.api.pipeline import ensure_default_passthrough_stage

		ensure_default_passthrough_stage()
		ensure_reconcile_minute_cron()
		ensure_kill_challenge_grace_default()
		frappe.db.commit()
	except Exception as e:
		# DocType controllers may not be importable mid-install; migrate/after_migrate covers this.
		frappe.log_error(f"domino_stream after_install: {e}", "Domino Stream Install")


def after_migrate():
	try:
		from domino_stream.api.pipeline import ensure_default_passthrough_stage

		ensure_default_passthrough_stage()
		ensure_reconcile_minute_cron()
		ensure_kill_challenge_grace_default()
		frappe.db.commit()
	except Exception as e:
		frappe.log_error(f"domino_stream after_migrate: {e}", "Domino Stream Migrate")
