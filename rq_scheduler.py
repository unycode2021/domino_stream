#!/usr/bin/env python3
"""Promote RQ ``enqueue_at`` jobs onto worker queues.

Frappe ``bench worker`` does not set ``with_scheduler=True``, so delayed jobs
(kill-challenge force-stop) would sit forever in ScheduledJobRegistry without
this process.

Procfile::

    domino_stream_rq_scheduler: .../env/bin/python apps/domino_stream/rq_scheduler.py
"""

from __future__ import annotations

import os
import sys


def main() -> int:
	# Bench root must be on path when run as apps/domino_stream/rq_scheduler.py
	bench_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
	if bench_root not in sys.path:
		sys.path.insert(0, bench_root)

	import frappe
	from frappe.utils.background_jobs import get_queue_list, get_redis_conn
	from rq.scheduler import RQScheduler

	with frappe.init_site():
		redis_connection = get_redis_conn()
		queues = get_queue_list(None, build_queue_name=True)

	scheduler = RQScheduler(queues, connection=redis_connection, interval=1)
	scheduler.acquire_locks()
	if not scheduler.acquired_locks:
		print(
			"domino_stream RQ scheduler: could not acquire locks "
			"(another RQ scheduler may already be running)",
			file=sys.stderr,
		)
		return 1

	print(f"domino_stream RQ scheduler listening queues={queues} interval=1s")
	scheduler.work()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
