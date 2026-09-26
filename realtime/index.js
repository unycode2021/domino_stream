const { Server } = require("socket.io");

const { get_conf, get_redis_subscriber } = require("../node_utils");
const conf = get_conf();

/** Defaults — Stream Settings may document overrides for clients; Node uses these. */
const SOCKET_PATH = "/domino-stream-socket.io";
const SOCKET_NAMESPACE = "domino-stream";

const io = new Server({
	path: SOCKET_PATH,
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
io.listen(port);
console.log(
	`domino_stream socket service listening on :${port} path=${SOCKET_PATH} nsp=/${SOCKET_NAMESPACE}`
);
