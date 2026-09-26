const request = require("superagent");

/**
 * Call Frappe on the *stream site* (socket Host), not the browser Origin.
 * Presence sockets terminate where Stream Room lives.
 */
function get_stream_site_url(socket, path) {
	if (!path) path = "";
	const host = socket.request.headers.host;
	const xfProto = (socket.request.headers["x-forwarded-proto"] || "").split(",")[0].trim();
	const proto =
		xfProto ||
		(socket.request.connection && socket.request.connection.encrypted ? "https" : "http");
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
	return partial_req.type("json").send(body || {});
}

module.exports = {
	get_stream_site_url,
	stream_post,
};
