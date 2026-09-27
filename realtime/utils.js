const request = require("superagent");

/**
 * Call Frappe on the stream site.
 *
 * Prefer browser Origin (https) when nginx terminates TLS — Domino101 parity.
 * Falling back to Host + X-Forwarded-Proto without Origin can POST http://…
 * and Certbot 301→https turns POST into GET, dropping the JSON body (table_id).
 */
function get_stream_site_url(socket, path) {
	if (!path) path = "";
	const origin = (socket.request.headers.origin || "").trim().replace(/\/$/, "");
	if (origin) {
		return `${origin}${path}`;
	}
	const host = socket.request.headers.host;
	const xfProto = (socket.request.headers["x-forwarded-proto"] || "")
		.split(",")[0]
		.trim();
	const proto =
		xfProto ||
		(socket.request.connection && socket.request.connection.encrypted
			? "https"
			: "http");
	return `${proto}://${host}${path}`;
}

function stream_post(path, socket, body) {
	const partial_req = request.post(get_stream_site_url(socket, path));
	if (socket.sid) {
		partial_req.query({ sid: socket.sid });
	}
	if (socket.authorization_header) {
		partial_req.set("Authorization", socket.authorization_header);
	}
	if (socket.stream_api_key) {
		partial_req.set("X-Domino-Stream-Key", socket.stream_api_key);
		if (!socket.authorization_header) {
			partial_req.set("Authorization", `Bearer ${socket.stream_api_key}`);
		}
	}
	if (socket.presence_token) {
		partial_req.set("X-Domino-Stream-Presence-Token", socket.presence_token);
	}
	const siteHost =
		socket.request.headers["x-frappe-site-name"] ||
		socket.request.headers.host;
	if (siteHost) {
		partial_req.set("X-Frappe-Site-Name", siteHost);
		partial_req.set("Host", siteHost);
	}
	return partial_req.type("json").send(body || {});
}

module.exports = {
	get_stream_site_url,
	stream_post,
};
