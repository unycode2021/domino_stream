app_name = "domino_stream"
app_title = "Domino Stream"
app_publisher = "Unycode Limited"
app_description = "Control plane for Cloudflare Realtime SFU table broadcasts"
app_email = "automate@unycode.net"
app_license = "mit"

after_install = "domino_stream.install.after_install"
after_migrate = "domino_stream.install.after_migrate"

website_route_rules = [
	{"from_route": "/domino-stream/<path:app_path>", "to_route": "domino_stream"},
	{"from_route": "/domino-stream", "to_route": "domino_stream"},
]

# Publisher force-kill: presence offline → kill_challenge → RQ delayed job
# (force_stop_if_challenge_expired). No minute reconcile cron — see install.py
# (legacy reconcile_live_rooms jobs are kept Stopped).

