const { stream_post } = require("../utils");

const PUBLISHER_ROOM_PREFIX = "publisher:";
const PUBLISHER_JOIN_EVENT = "publisher_join";
const PUBLISHER_PING_EVENT = "publisher_ping";
const PUBLISHER_LEAVE_EVENT = "publisher_leave";
const KEEP_ALIVE_EVENT = "keep_alive";
const KEEP_ALIVE_OK_EVENT = "keep_alive_ok";

const HB_METHOD = "/api/method/domino_stream.api.room.publisher_heartbeat";
const OFFLINE_METHOD = "/api/method/domino_stream.api.room.publisher_presence_offline";
const KEEP_ALIVE_METHOD = "/api/method/domino_stream.api.room.publisher_keep_alive";

const log = console.log;

function publisher_room_id(tableId) {
	if (!tableId || typeof tableId !== "string") return null;
	return `${PUBLISHER_ROOM_PREFIX}${tableId}`;
}

function http_fail_detail(err, res) {
	const status = (res && res.status) || (err && err.status) || null;
	let bodySnippet = "";
	try {
		const raw =
			(res && res.text) ||
			(res && res.body && JSON.stringify(res.body)) ||
			(err && err.response && err.response.text) ||
			"";
		bodySnippet = String(raw).slice(0, 200);
	} catch (_e) {
		bodySnippet = "";
	}
	return { status, bodySnippet, message: (err && err.message) || "" };
}

function call_heartbeat(socket, tableId, sessionId) {
	const body = { table_id: tableId, via: "socket" };
	if (sessionId) body.session_id = sessionId;
	stream_post(HB_METHOD, socket, body).end((err, res) => {
		if (err || !res || res.status >= 400) {
			const d = http_fail_detail(err, res);
			log(
				"stream publisher heartbeat failed",
				tableId,
				d.status,
				d.message,
				d.bodySnippet
			);
		}
	});
}

function call_presence_offline(socket, tableId) {
	if (!tableId) return;
	stream_post(OFFLINE_METHOD, socket, { table_id: tableId }).end((err, res) => {
		if (err || !res || res.status >= 400) {
			const d = http_fail_detail(err, res);
			log(
				"stream publisher presence offline failed",
				tableId,
				d.status,
				d.message,
				d.bodySnippet
			);
		}
	});
}

function leave_publisher_presence(socket, tableId) {
	if (!tableId) return;
	const room = publisher_room_id(tableId);
	if (!room) return;
	socket.leave(room);
	if (socket.data) {
		if (socket.data.publisherTableId === tableId) {
			delete socket.data.publisherTableId;
			delete socket.data.publisherSessionId;
		}
		if (socket.data.publisherRooms) {
			socket.data.publisherRooms.delete(room);
		}
	}
	call_presence_offline(socket, tableId);
}

function presence_handlers(_nsp, socket) {
	socket.on(PUBLISHER_JOIN_EVENT, ({ table_id, session_id } = {}) => {
		if (!table_id || typeof table_id !== "string") return;
		const room = publisher_room_id(table_id);
		if (!room) return;
		if (
			socket.data &&
			socket.data.publisherTableId &&
			socket.data.publisherTableId !== table_id
		) {
			leave_publisher_presence(socket, socket.data.publisherTableId);
		}
		socket.join(room);
		if (!socket.data) socket.data = {};
		socket.data.publisherTableId = table_id;
		socket.data.publisherSessionId = session_id || null;
		socket.data.publisherRooms = socket.data.publisherRooms || new Set();
		socket.data.publisherRooms.add(room);
		call_heartbeat(socket, table_id, session_id);
	});

	socket.on(PUBLISHER_PING_EVENT, ({ table_id, session_id } = {}) => {
		const tid = table_id || (socket.data && socket.data.publisherTableId);
		if (!tid) return;
		const room = publisher_room_id(tid);
		if (!room || !socket.rooms.has(room)) return;
		const sid =
			session_id || (socket.data && socket.data.publisherSessionId) || null;
		call_heartbeat(socket, tid, sid);
	});

	socket.on(PUBLISHER_LEAVE_EVENT, ({ table_id } = {}) => {
		const tid = table_id || (socket.data && socket.data.publisherTableId);
		if (!tid) return;
		leave_publisher_presence(socket, tid);
	});

	socket.on(KEEP_ALIVE_EVENT, ({ table_id, challenge_id, session_id } = {}) => {
		const tid = table_id || (socket.data && socket.data.publisherTableId);
		if (!tid || !challenge_id) return;
		const sid =
			session_id || (socket.data && socket.data.publisherSessionId) || null;
		const body = { table_id: tid, challenge_id };
		if (sid) body.session_id = sid;
		stream_post(KEEP_ALIVE_METHOD, socket, body).end((err, res) => {
			if (err || !res || res.status >= 400) {
				const d = http_fail_detail(err, res);
				log(
					"stream keep_alive failed",
					tid,
					d.status,
					d.message,
					d.bodySnippet
				);
				socket.emit(KEEP_ALIVE_OK_EVENT, {
					table_id: tid,
					challenge_id,
					success: false,
					error: (err && err.message) || "keep_alive failed",
				});
				return;
			}
			const data = res.body || {};
			const ok = data.message || data;
			socket.emit(KEEP_ALIVE_OK_EVENT, {
				table_id: tid,
				challenge_id,
				success: !data.exc_type && !!(ok && ok.success !== false),
				result: ok,
			});
		});
	});

	socket.on("disconnect", () => {
		const tid = socket.data && socket.data.publisherTableId;
		if (tid) {
			leave_publisher_presence(socket, tid);
		}
	});
}

module.exports = presence_handlers;
