const fs = require("fs");
const path = require("path");
const redis = require("@redis/client");
const dns = require("dns");

const bench_path = path.resolve(__dirname, "..", "..");

dns.setDefaultResultOrder("ipv4first");

function get_conf() {
	const conf = {
		socketio_port: 9000,
		domino_stream_io_port: 9003,
	};

	const read_config = (file_path) => {
		const full_path = path.resolve(bench_path, file_path);
		if (fs.existsSync(full_path)) {
			const bench_config = JSON.parse(fs.readFileSync(full_path));
			for (const key in bench_config) {
				if (bench_config[key]) {
					conf[key] = bench_config[key];
				}
			}
		}
	};

	read_config("config.json");
	read_config("sites/common_site_config.json");

	if (process.env.FRAPPE_SITE) {
		conf.default_site = process.env.FRAPPE_SITE;
	}
	if (process.env.FRAPPE_REDIS_QUEUE) {
		conf.redis_queue = process.env.FRAPPE_REDIS_QUEUE;
	}
	if (process.env.DOMINO_STREAM_IO_PORT) {
		conf.domino_stream_io_port = process.env.DOMINO_STREAM_IO_PORT;
	}
	return conf;
}

function get_redis_subscriber(kind = "redis_queue", options = {}) {
	const conf = get_conf();
	const host = conf[kind];
	return redis.createClient({ url: host, ...options });
}

module.exports = {
	get_conf,
	get_redis_subscriber,
};
