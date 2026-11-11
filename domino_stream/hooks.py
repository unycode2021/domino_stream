app_name = "domino_stream"
app_title = "Domino Stream"
app_publisher = "Unycode Limited"
app_description = "Control plane for Cloudflare Realtime SFU table broadcasts"
app_email = "automate@unycode.net"
app_license = "mit"

after_install = "domino_stream.install.after_install"
after_migrate = "domino_stream.install.after_migrate"

before_request = ["domino_stream.api.mediamtx_bridge.proxy_whip_request"]

website_route_rules = [
	{"from_route": "/domino-stream/<path:app_path>", "to_route": "domino_stream"},
	{"from_route": "/domino-stream", "to_route": "domino_stream"},
]

# Publisher force-kill: presence offline → kill_challenge → RQ delayed job
# (force_stop_if_challenge_expired). No minute reconcile cron — see install.py
# (legacy reconcile_live_rooms jobs are kept Stopped).
# Safety net only: a Live room with no heartbeat for minutes and a dead SFU session gets
# a kill challenge (never an immediate stop), for when the offline call never lands.
scheduler_events = {
	"cron": {
		"*/2 * * * *": ["domino_stream.api.reconcile.challenge_abandoned_rooms"],
	},
}

