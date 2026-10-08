"""Download pinned MediaMTX and ffmpeg, and wire bench processes.

Binaries stay in ``apps/domino_stream/bin/`` (gitignored). ``bench setup supervisor``
does not rewrite ``config/domino-stream.conf``.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

MEDIAMTX_VERSION = "1.21.1"
MEDIAMTX_SHA256 = {
	"x86_64": "653abc672a3e693f8d3b2717752492fdcfb8072291ec108d03d3dd857411b0ee",
	"amd64": "653abc672a3e693f8d3b2717752492fdcfb8072291ec108d03d3dd857411b0ee",
	"aarch64": "6a3aa635fb60ea9b8d566ec306f0a42ff1b6b52a3942bc2baffbe55880d4c3dd",
	"arm64": "6a3aa635fb60ea9b8d566ec306f0a42ff1b6b52a3942bc2baffbe55880d4c3dd",
}
MEDIAMTX_ARCHIVE_ARCH = {
	"x86_64": "amd64",
	"amd64": "amd64",
	"aarch64": "arm64",
	"arm64": "arm64",
}

FFMPEG_RELEASE = "autobuild-2026-10-07-13-07"
FFMPEG_ASSETS = {
	"x86_64": {
		"name": "ffmpeg-N-127233-g452820cba6-linux64-gpl.tar.xz",
		"sha256": "6e0f8201935c78c47542f9e53e8a0209ee963b36b674d03bb67cd777fffd5ce1",
	},
	"amd64": {
		"name": "ffmpeg-N-127233-g452820cba6-linux64-gpl.tar.xz",
		"sha256": "6e0f8201935c78c47542f9e53e8a0209ee963b36b674d03bb67cd777fffd5ce1",
	},
	"aarch64": {
		"name": "ffmpeg-N-127233-g452820cba6-linuxarm64-gpl.tar.xz",
		"sha256": "89f30f8fe1acc39f69e934b19cfcf094fdb02704b7bbfcf601c7493e33a7f44e",
	},
	"arm64": {
		"name": "ffmpeg-N-127233-g452820cba6-linuxarm64-gpl.tar.xz",
		"sha256": "89f30f8fe1acc39f69e934b19cfcf094fdb02704b7bbfcf601c7493e33a7f44e",
	},
}

STREAM_IO_PORT = 9003
MEDIAMTX_WHIP_PORT = 8889

PROCFILE_BEGIN = "# DOMINO_STREAM_BEGIN"
PROCFILE_END = "# DOMINO_STREAM_END"
UPSTREAM_BEGIN = "# DOMINO_STREAM_UPSTREAM_BEGIN"
UPSTREAM_END = "# DOMINO_STREAM_UPSTREAM_END"
LOCATIONS_BEGIN = "# DOMINO_STREAM_LOCATIONS_BEGIN"
LOCATIONS_END = "# DOMINO_STREAM_LOCATIONS_END"
PROCFILE_KEYS = (
	"domino_stream_io",
	"domino_stream_mediamtx",
	"domino_stream_rq_scheduler",
)
RQ_COMMENT = "# Promote RQ enqueue_at jobs (kill-challenge force-stop) onto worker queues"

HOST_NOTES = (
	"Open UDP 8189 and TCP 8190 on the public host. WebRTC media does not pass through nginx.",
	"Optional: raise net.core.rmem_max and net.core.rmem_default to at least 1000000, "
	"then set udpReadBufferSize: 1000000 in mediamtx.yml and restart MediaMTX.",
	"Cloudflare app credentials and the YouTube destination stay in Stream Settings and Domino Defaults.",
	"After bench setup nginx, bench setup supervisor, or bench setup procfile, run bench domino-stream-setup again.",
)

SUPERVISOR_LINK = "/etc/supervisor/conf.d/domino-stream.conf"


class ChecksumError(Exception):
	"""Archive bytes do not match the pinned SHA256."""


class RuntimeResult:
	def __init__(self):
		self.errors: list[str] = []
		self.notes: list[str] = []

	@property
	def ok(self) -> bool:
		return not self.errors


def app_root() -> Path:
	return Path(__file__).resolve().parents[1]


def bench_root() -> Path:
	return app_root().parents[1]


def verify_sha256(path: Path, expected: str) -> None:
	"""Raise ChecksumError and delete the archive when the digest does not match."""
	digest = hashlib.sha256()
	with open(path, "rb") as handle:
		for chunk in iter(lambda: handle.read(1024 * 1024), b""):
			digest.update(chunk)
	actual = digest.hexdigest()
	if actual != expected.lower():
		path.unlink(missing_ok=True)
		raise ChecksumError(f"checksum mismatch for {path.name}: expected {expected} got {actual}")


def extract_archive_member(archive: Path, suffix: str, dest: Path) -> None:
	"""Write one archive member to dest. Other members, including mediamtx.yml, stay put."""
	mode = _tar_mode(archive)
	with tarfile.open(archive, mode) as tar:
		member = _matching_member(tar, suffix)
		source = tar.extractfile(member)
		if source is None:
			raise RuntimeError(f"{suffix} was not readable in {archive.name}")
		dest.parent.mkdir(parents=True, exist_ok=True)
		partial = dest.with_name(dest.name + ".partial")
		try:
			with source, open(partial, "wb") as out:
				shutil.copyfileobj(source, out)
			os.chmod(partial, 0o755)
			os.replace(partial, dest)
		finally:
			if partial.exists():
				partial.unlink()


def render_procfile_block(*, node: str, app_dir: str, mediamtx: str, python: str) -> str:
	return "\n".join(
		[
			PROCFILE_BEGIN,
			f"domino_stream_io: {node} {app_dir}/socketio.js",
			f"domino_stream_mediamtx: {mediamtx} {app_dir}/mediamtx.yml",
			f"domino_stream_rq_scheduler: {python} {app_dir}/rq_scheduler.py",
			PROCFILE_END,
		]
	)


def apply_procfile(text: str, block: str) -> str:
	"""Replace any Domino Stream process lines with one marker block at the end."""
	text = re.sub(
		rf"(?:^|\n){re.escape(PROCFILE_BEGIN)}.*?{re.escape(PROCFILE_END)}(?:\n|$)",
		"\n",
		text,
		count=1,
		flags=re.S,
	)
	kept: list[str] = []
	for line in text.splitlines():
		key = line.split(":", 1)[0].strip()
		if key in PROCFILE_KEYS or line.strip() == RQ_COMMENT:
			continue
		if line.strip() == "" and (not kept or kept[-1] == ""):
			continue
		kept.append(line)
	body = "\n".join(kept).strip()
	block = block.strip()
	if body:
		return body + "\n\n" + block + "\n"
	return block + "\n"


def render_upstreams(bench_name: str, io_port: int = STREAM_IO_PORT) -> str:
	return "\n".join(
		[
			UPSTREAM_BEGIN,
			f"upstream {bench_name}-domino-stream-io-server {{",
			f"\tserver 127.0.0.1:{io_port} fail_timeout=0;",
			"}",
			f"upstream {bench_name}-mediamtx {{",
			f"\tserver 127.0.0.1:{MEDIAMTX_WHIP_PORT} fail_timeout=0;",
			"}",
			UPSTREAM_END,
		]
	)


def render_locations(bench_name: str) -> str:
	return "\n".join(
		[
			LOCATIONS_BEGIN,
			"location /mediamtx/ {",
			"\tproxy_http_version 1.1;",
			"\tproxy_set_header Host $host;",
			"\tproxy_set_header X-Forwarded-For $remote_addr;",
			"\tproxy_set_header X-Forwarded-Proto $scheme;",
			"\tproxy_set_header X-Frappe-Site-Name $host;",
			"\tproxy_buffering off;",
			"\tproxy_read_timeout 600s;",
			f"\tproxy_pass http://{bench_name}-mediamtx/;",
			"}",
			"",
			"location /domino-stream-socket.io {",
			"\tproxy_http_version 1.1;",
			"\tproxy_set_header Upgrade $http_upgrade;",
			'\tproxy_set_header Connection "upgrade";',
			"\tproxy_set_header X-Forwarded-For $remote_addr;",
			"\tproxy_set_header X-Forwarded-Proto $scheme;",
			"\tproxy_set_header X-Frappe-Site-Name $host;",
			"\tproxy_set_header Origin $scheme://$http_host;",
			"\tproxy_set_header Host $host;",
			f"\tproxy_pass http://{bench_name}-domino-stream-io-server;",
			"}",
			LOCATIONS_END,
		]
	)


def apply_nginx_config(text: str, bench_name: str, io_port: int = STREAM_IO_PORT) -> str:
	"""Insert marker blocks. Leave an nginx file that already proxies these paths."""
	upstreams = render_upstreams(bench_name, io_port)
	locations = render_locations(bench_name)
	if UPSTREAM_BEGIN in text:
		text = _replace_marked(text, UPSTREAM_BEGIN, UPSTREAM_END, upstreams)
	elif f"upstream {bench_name}-mediamtx " in text or f"upstream {bench_name}-mediamtx {{" in text:
		pass
	else:
		text = upstreams + "\n" + text
	if LOCATIONS_BEGIN in text:
		text = _replace_marked(text, LOCATIONS_BEGIN, LOCATIONS_END, locations)
	elif "location /mediamtx/" in text and "location /domino-stream-socket.io" in text:
		pass
	else:
		text = _insert_before_location_root(text, locations)
	return text


def render_supervisor_config(
	*,
	bench_name: str,
	bench_dir: str,
	app_dir: str,
	user: str,
	node: str | None,
	site: str,
) -> str:
	"""Supervisor programs that bench setup supervisor does not regenerate."""
	logs = f"{bench_dir}/logs"
	mediamtx = f"{app_dir}/bin/mediamtx"
	blocks = [
		"; Domino Stream. bench setup supervisor does not rewrite this file.",
		f"; Install: sudo ln -sfn {bench_dir}/config/domino-stream.conf {SUPERVISOR_LINK}",
		"; then sudo supervisorctl reread && sudo supervisorctl update",
		"",
		f"[program:{bench_name}-domino-stream-mediamtx]",
		f"command={mediamtx} {app_dir}/mediamtx.yml",
		"priority=4",
		"autostart=true",
		"autorestart=true",
		f"stdout_logfile={logs}/mediamtx.log",
		f"stderr_logfile={logs}/mediamtx.error.log",
		f"user={user}",
		f"directory={bench_dir}",
		"startretries=10",
		"",
	]
	if node:
		blocks.extend(
			[
				f"[program:{bench_name}-domino-stream-io]",
				f"command={node} {app_dir}/socketio.js",
				"priority=4",
				"autostart=true",
				"autorestart=true",
				f"stdout_logfile={logs}/node-domino-stream-socketio.log",
				f"stderr_logfile={logs}/node-domino-stream-socketio.error.log",
				f"user={user}",
				f"directory={bench_dir}",
				"startretries=10",
				"",
			]
		)
	environment = 'PYTHONUNBUFFERED="1"'
	if site and '"' not in site:
		environment = f'FRAPPE_SITE="{site}",{environment}'
	blocks.extend(
		[
			f"[program:{bench_name}-domino-stream-rq-scheduler]",
			f"command={bench_dir}/env/bin/python -u {app_dir}/rq_scheduler.py",
			"priority=4",
			"autostart=true",
			"autorestart=true",
			f"stdout_logfile={logs}/domino-stream-rq-scheduler.log",
			f"stderr_logfile={logs}/domino-stream-rq-scheduler.error.log",
			f"user={user}",
			f"directory={bench_dir}",
			f"environment={environment}",
			"killasgroup=true",
			"startretries=10",
			"",
		]
	)
	return "\n".join(blocks)


def ensure_runtime() -> RuntimeResult:
	"""Install pinned binaries and local process config. Does not raise."""
	result = RuntimeResult()
	root = app_root()
	bench = bench_root()
	try:
		binary = _ensure_mediamtx(root)
		result.notes.append(f"MediaMTX {MEDIAMTX_VERSION} is {binary}")
	except Exception as exc:
		result.errors.append(f"MediaMTX: {exc}")
	try:
		binary = _ensure_ffmpeg(root)
		result.notes.append(f"ffmpeg with libx264 is {binary}")
	except Exception as exc:
		result.errors.append(f"ffmpeg: {exc}")
	try:
		_ensure_forward_script(root)
	except Exception as exc:
		result.errors.append(f"forward script: {exc}")
	try:
		_wire_processes(root, bench, result)
	except Exception as exc:
		result.errors.append(f"process config: {exc}")
	result.notes.extend(HOST_NOTES)
	return result


def _ensure_mediamtx(root: Path) -> Path:
	binary = root / "bin" / "mediamtx"
	if _installed_mediamtx_version(binary) == MEDIAMTX_VERSION:
		return binary
	machine = platform.machine()
	archive_arch = MEDIAMTX_ARCHIVE_ARCH.get(machine)
	expected = MEDIAMTX_SHA256.get(machine)
	if not archive_arch or not expected:
		raise RuntimeError(f"no pinned MediaMTX build for {machine or 'this machine'}")
	url = (
		"https://github.com/bluenviron/mediamtx/releases/download/"
		f"v{MEDIAMTX_VERSION}/mediamtx_v{MEDIAMTX_VERSION}_linux_{archive_arch}.tar.gz"
	)
	archive = root / "bin" / f".mediamtx-{archive_arch}.tar.gz"
	_fetch_verified(url, expected, archive)
	try:
		extract_archive_member(archive, "mediamtx", binary)
	finally:
		archive.unlink(missing_ok=True)
	if _installed_mediamtx_version(binary) != MEDIAMTX_VERSION:
		raise RuntimeError(f"MediaMTX {binary} is not v{MEDIAMTX_VERSION}")
	return binary


def _ensure_ffmpeg(root: Path) -> Path:
	from domino_stream.api.mediamtx_bridge import ffmpeg_supports_libx264

	binary = root / "bin" / "ffmpeg"
	machine = platform.machine()
	asset = FFMPEG_ASSETS.get(machine)
	if not asset:
		raise RuntimeError(f"no pinned ffmpeg build for {machine or 'this machine'}")
	expected = asset["sha256"]
	stamp = root / "bin" / ".ffmpeg-sha256"
	if (
		binary.is_file()
		and os.access(binary, os.X_OK)
		and stamp.is_file()
		and stamp.read_text(encoding="utf-8").strip() == expected
		and ffmpeg_supports_libx264(str(binary))
	):
		return binary
	url = (
		"https://github.com/BtbN/FFmpeg-Builds/releases/download/"
		f"{FFMPEG_RELEASE}/{asset['name']}"
	)
	archive = root / "bin" / ".ffmpeg.tar.xz"
	_fetch_verified(url, expected, archive)
	try:
		extract_archive_member(archive, "bin/ffmpeg", binary)
	finally:
		archive.unlink(missing_ok=True)
	if not ffmpeg_supports_libx264(str(binary)):
		raise RuntimeError(f"{binary} does not include libx264")
	stamp.write_text(expected + "\n", encoding="utf-8")
	return binary


def _ensure_forward_script(root: Path) -> None:
	script = root / "mediamtx-forward.sh"
	if not script.is_file():
		raise RuntimeError(f"{script} is missing")
	script.chmod(script.stat().st_mode | 0o111)


def _wire_processes(root: Path, bench: Path, result: RuntimeResult) -> None:
	(bench / "logs").mkdir(exist_ok=True)
	(bench / "config").mkdir(exist_ok=True)
	node = _resolve_node_bin()
	if not node:
		result.errors.append("node is not installed; publisher presence Socket.IO cannot start")
	site = _default_site(bench)
	user = getpass.getuser()
	bench_name = bench.name
	supervisor = render_supervisor_config(
		bench_name=bench_name,
		bench_dir=str(bench),
		app_dir=str(root),
		user=user,
		node=node,
		site=site,
	)
	supervisor_path = bench / "config" / "domino-stream.conf"
	_write_if_changed(supervisor_path, supervisor)
	result.notes.append(f"Supervisor programs are in {supervisor_path}")
	_link_supervisor(supervisor_path, bench, bench_name, result)

	python = str(bench / "env" / "bin" / "python")
	block = render_procfile_block(
		node=node or "node",
		app_dir=str(root),
		mediamtx=str(root / "bin" / "mediamtx"),
		python=python,
	)
	procfile = bench / "Procfile"
	current = procfile.read_text(encoding="utf-8") if procfile.is_file() else ""
	updated_procfile = apply_procfile(current, block)
	if updated_procfile != current:
		procfile.write_text(updated_procfile, encoding="utf-8")
		result.notes.append(f"Wrote Domino Stream processes into {procfile}")
	else:
		result.notes.append(f"{procfile} already has Domino Stream processes")

	nginx = bench / "config" / "nginx.conf"
	io_port = _stream_io_port(bench)
	if not nginx.is_file():
		result.notes.append("No config/nginx.conf yet. After bench setup nginx, run bench domino-stream-setup again.")
		return
	current_nginx = nginx.read_text(encoding="utf-8")
	updated = apply_nginx_config(current_nginx, bench_name, io_port)
	if LOCATIONS_BEGIN not in updated and "location /mediamtx/" not in updated:
		result.errors.append("Could not add Domino Stream locations to config/nginx.conf")
		return
	if updated != current_nginx:
		nginx.write_text(updated, encoding="utf-8")
		result.notes.append(f"Wrote Domino Stream locations into {nginx}")
	else:
		result.notes.append(f"{nginx} already proxies /mediamtx/ and /domino-stream-socket.io")


def _write_if_changed(path: Path, text: str) -> None:
	if path.is_file() and path.read_text(encoding="utf-8") == text:
		return
	path.write_text(text, encoding="utf-8")


def _link_supervisor(source: Path, bench: Path, bench_name: str, result: RuntimeResult) -> None:
	names = (
		f"{bench_name}-domino-stream-mediamtx",
		f"{bench_name}-domino-stream-io",
		f"{bench_name}-domino-stream-rq-scheduler",
	)
	existing = bench / "config" / "supervisor.conf"
	install = f"sudo ln -sfn {source} {SUPERVISOR_LINK} && sudo supervisorctl reread && sudo supervisorctl update"
	if existing.is_file():
		current = existing.read_text(encoding="utf-8", errors="replace")
		if any(f"[program:{name}]" in current for name in names):
			result.notes.append(
				"config/supervisor.conf already defines one of these programs, so the extra file was not linked. "
				f"{source} is the copy that survives bench setup supervisor. After that command, run: {install}"
			)
			return
	dest = Path(SUPERVISOR_LINK)
	try:
		if dest.is_symlink() and dest.resolve() == source.resolve():
			result.notes.append(f"Supervisor snippet already linked at {dest}")
		else:
			if dest.is_symlink() or dest.exists():
				dest.unlink()
			dest.symlink_to(source)
			result.notes.append(f"Linked {dest}")
	except OSError as exc:
		result.errors.append(f"Could not link {dest} ({exc}). Run: {install}")
		return
	_supervisor_reread(result, install)


def _supervisor_reread(result: RuntimeResult, install: str) -> None:
	reread = subprocess.run(["supervisorctl", "reread"], capture_output=True, text=True, check=False)
	update = subprocess.run(["supervisorctl", "update"], capture_output=True, text=True, check=False)
	if reread.returncode != 0 or update.returncode != 0:
		detail = (reread.stderr or reread.stdout or update.stderr or update.stdout or "").strip()
		result.errors.append(f"supervisorctl did not reload ({detail}). Run: {install}")


def _fetch_verified(url: str, expected: str, dest: Path) -> None:
	dest.parent.mkdir(parents=True, exist_ok=True)
	partial = dest.with_suffix(dest.suffix + ".partial")
	request = urllib.request.Request(url, headers={"User-Agent": "domino-stream-setup"})
	try:
		with urllib.request.urlopen(request, timeout=180) as response, open(partial, "wb") as out:
			shutil.copyfileobj(response, out)
		os.replace(partial, dest)
	except Exception:
		partial.unlink(missing_ok=True)
		raise
	verify_sha256(dest, expected)


def _installed_mediamtx_version(binary: Path) -> str:
	if not binary.is_file() or not os.access(binary, os.X_OK):
		return ""
	try:
		completed = subprocess.run(
			[str(binary), "--version"],
			capture_output=True,
			text=True,
			timeout=10,
			check=False,
		)
	except (OSError, subprocess.TimeoutExpired):
		return ""
	match = re.search(r"(\d+\.\d+\.\d+)", f"{completed.stdout}\n{completed.stderr}")
	return match.group(1) if match else ""


def _resolve_node_bin() -> str | None:
	found = shutil.which("node")
	if found:
		return found
	nvm = Path.home() / ".nvm" / "versions" / "node"
	if nvm.is_dir():
		candidates = sorted(path for path in nvm.glob("*/bin/node") if os.access(path, os.X_OK))
		if candidates:
			return str(candidates[-1])
	for candidate in ("/usr/bin/node", "/usr/local/bin/node"):
		if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
			return candidate
	return None


def _default_site(bench: Path) -> str:
	data = _read_common_config(bench)
	site = str(data.get("default_site") or "").strip()
	if '"' in site:
		return ""
	return site


def _stream_io_port(bench: Path) -> int:
	raw = _read_common_config(bench).get("domino_stream_io_port") or STREAM_IO_PORT
	try:
		return int(raw)
	except (TypeError, ValueError):
		return STREAM_IO_PORT


def _read_common_config(bench: Path) -> dict:
	path = bench / "sites" / "common_site_config.json"
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return {}
	return data if isinstance(data, dict) else {}


def _tar_mode(path: Path) -> str:
	name = path.name
	if name.endswith(".tar.xz") or name.endswith(".txz"):
		return "r:xz"
	if name.endswith(".tar.gz") or name.endswith(".tgz"):
		return "r:gz"
	raise RuntimeError(f"unsupported archive {name}")


def _matching_member(tar: tarfile.TarFile, suffix: str) -> tarfile.TarInfo:
	matches = []
	for member in tar.getmembers():
		if not member.isfile():
			continue
		name = member.name.lstrip("./")
		if ".." in name.split("/"):
			continue
		if name == suffix or name.endswith("/" + suffix):
			matches.append(member)
	if not matches:
		raise RuntimeError(f"{suffix} was not in the archive")
	return matches[0]


def _replace_marked(text: str, begin: str, end: str, block: str) -> str:
	pattern = re.compile(
		rf"^[ \t]*{re.escape(begin)}.*?^[ \t]*{re.escape(end)}[ \t]*$",
		re.M | re.S,
	)

	def replacer(match: re.Match) -> str:
		first = match.group(0).splitlines()[0]
		indent = first[: len(first) - len(first.lstrip(" \t"))]
		return "\n".join((indent + line) if line else "" for line in block.splitlines())

	return pattern.sub(replacer, text)


def _insert_before_location_root(text: str, block: str) -> str:
	pattern = re.compile(r"^([ \t]*)location / \{", re.M)

	def replacer(match: re.Match) -> str:
		indent = match.group(1)
		indented = "\n".join((indent + line) if line else "" for line in block.splitlines())
		return indented + "\n\n" + match.group(0)

	return pattern.sub(replacer, text)
