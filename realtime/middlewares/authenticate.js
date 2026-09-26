const cookie = require("cookie");
const request = require("superagent");
const { get_stream_site_url } = require("../utils");

/**
 * Authenticate publisher presence sockets.
 *
 * - Cookie / sid session (co-located DCMS on stream site)
 * - Presence token from prepare_publish (auth.token / X-Domino-Stream-Presence-Token)
 * - Or inbound stream API key (Authorization Bearer / X-Domino-Stream-Key)
 */
function authenticate(socket, next) {
	const headers = socket.request.headers || {};
	const authToken =
		(socket.handshake && socket.handshake.auth && socket.handshake.auth.token) ||
		null;
	const presenceTokenHeader = (
		headers["x-domino-stream-presence-token"] ||
		""
	).trim();
	const presenceToken =
		presenceTokenHeader ||
		(authToken && String(authToken).trim()) ||
		"";

	let streamKey = (headers["x-domino-stream-key"] || "").trim();
	const authorization = (headers.authorization || "").trim();
	if (!streamKey && authorization.toLowerCase().startsWith("bearer ") && !presenceToken) {
		streamKey = authorization.slice(7).trim();
	}

	if (streamKey && !presenceToken) {
		socket.stream_api_key = streamKey;
		socket.authorization_header = authorization || `Bearer ${streamKey}`;
		socket.user = "Stream Key";
		next();
		return;
	}

	if (presenceToken) {
		request
			.get(
				get_stream_site_url(
					socket,
					"/api/method/domino_stream.api.realtime_pub.validate_presence_token"
				)
			)
			.query({ token: presenceToken })
			.type("form")
			.then((res) => {
				const msg = (res.body && res.body.message) || {};
				socket.presence_token = presenceToken;
				socket.user = msg.user || "Publisher";
				socket.user_type = msg.user_type || "Website User";
				next();
			})
			.catch((e) => {
				next(new Error(`Unauthorized presence token: ${e}`));
			});
		return;
	}

	const cookies = cookie.parse(headers.cookie || "");
	if (!cookies.sid && !authorization) {
		next(
			new Error(
				"No authentication method used. Use cookie, presence token, or stream API key."
			)
		);
		return;
	}

	let auth_req = request.get(
		get_stream_site_url(socket, "/api/method/frappe.realtime.get_user_info")
	);
	if (cookies.sid) {
		auth_req = auth_req.query({ sid: cookies.sid });
	} else {
		auth_req = auth_req.set("Authorization", authorization);
	}

	auth_req
		.type("form")
		.then((res) => {
			socket.user = res.body.message.user;
			socket.user_type = res.body.message.user_type;
			socket.sid = cookies.sid;
			socket.authorization_header = authorization;
			next();
		})
		.catch((e) => {
			next(new Error(`Unauthorized: ${e}`));
		});
}

module.exports = authenticate;
