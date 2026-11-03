const { Server } = require("socket.io");

const { get_conf, get_redis_subscriber } = require("../node_utils");
const conf = get_conf();

/** Defaults — Stream Settings may document overrides for clients; Node uses these. */
const SOCKET_PATH = "/domino-stream-socket.io";
const SOCKET_NAMESPACE = "domino-stream";

const io = new Server({
	path: SOCKET_PATH,
	pingInterval: 10000,
	pingTimeout: 10000,
	cors: {
		origin: true,
		credentials: true,
	},
	cleanupEmptyChildNamespaces: true,
});

const nsp = io.of(SOCKET_NAMESPACE);

const authenticate = require("./middlewares/authenticate");
nsp.use(authenticate);

const presence_handlers = require("./handlers/presence_handlers");
nsp.on("connection", (socket) => {
	presence_handlers(nsp, socket);
});

// Consume events from Python (kill_challenge, etc.) via Redis pub/sub.
const subscriber = get_redis_subscriber();

(async () => {
	await subscriber.connect();
	subscriber.subscribe("events", (message) => {
		try {
			message = JSON.parse(message);
		} catch (_) {
			return;
		}
		if (!message || !message.namespace) return;
		// Only handle stream namespace (and optional leading slash variants)
		const ns = String(message.namespace).replace(/^\//, "");
		if (ns !== SOCKET_NAMESPACE) return;

		const namespace = "/" + ns;
		if (message.room) {
			io.of(namespace).to(message.room).emit(message.event, message.message);
		} else {
			io.of(namespace).emit(message.event, message.message);
		}
	});
})();

const port = conf.domino_stream_io_port || 9003;
// Only nginx (127.0.0.1) needs this port. A dev box that connects to it directly can set
// "realtime_bind_host": "0.0.0.0" in sites/common_site_config.json.
const bind_host = conf.realtime_bind_host || "127.0.0.1";
const http_server = require("http").createServer((req, res) => {
	res.writeHead(404);
	res.end();
});
io.attach(http_server);
http_server.listen(port, bind_host);
console.log(
	`domino_stream socket service listening on ${bind_host}:${port} path=${SOCKET_PATH} nsp=/${SOCKET_NAMESPACE}`
);
