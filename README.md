# Domino Stream

Frappe control plane for Cloudflare Realtime SFU and Match Program YouTube forwarding.

- Site: `stream.domino101.com`
- In-app media: Cloudflare Realtime SFU (not Stream Live)
- YouTube: browser WHIP into MediaMTX, then ffmpeg RTMPS to YouTube or Cloudflare
- Publisher presence: Socket.IO on port 9003
- MVP: ingest → deliver (1 publisher, many spectators)

`pyproject.toml` lists no Python packages for this. MediaMTX and ffmpeg are binaries. `bench install-app` and `bench migrate` download the pinned builds into `apps/domino_stream/bin/` (gitignored) and write the process config. Run the same step by hand with:

```bash
bench domino-stream-setup
```

That command exits non-zero when a binary or a required process cannot be installed. A failed download during migrate is written to the Error Log and does not undo the app install.

The setup writes:

- `config/domino-stream.conf` for MediaMTX, the presence Socket.IO server, and the RQ scheduler. This file is separate from `config/supervisor.conf`, so `bench setup supervisor` does not delete it.
- Marker blocks in the bench `Procfile` for `bench start`.
- Marker blocks in `config/nginx.conf` for `/mediamtx/` and `/domino-stream-socket.io`, when that file exists.

`/mediamtx/` also reaches MediaMTX through the app `before_request` proxy. Presence does not: nginx has to send `/domino-stream-socket.io` to port 9003.

`bench setup nginx`, `bench setup supervisor`, and `bench setup procfile` regenerate those bench files and drop the marker blocks. Run `bench domino-stream-setup` again afterward. If the programs are not already defined in `config/supervisor.conf`, the command links `config/domino-stream.conf` into `/etc/supervisor/conf.d/`. When it cannot, it prints:

```bash
sudo ln -sfn /path/to/bench/config/domino-stream.conf /etc/supervisor/conf.d/domino-stream.conf
sudo supervisorctl reread && sudo supervisorctl update
```

## Still set on the host

- Open **UDP 8189** and **TCP 8190** on the public host. WebRTC media does not pass through nginx.
- Optional: raise `net.core.rmem_max` and `net.core.rmem_default` to at least `1000000`, then set `udpReadBufferSize: 1000000` in `mediamtx.yml` and restart MediaMTX. The shipped value `212992` is what lets MediaMTX start on a default kernel.
- Cloudflare app id and secret, and the YouTube destination, stay in Stream Settings and Domino Defaults. They are account secrets, not install artifacts.

See `docs/` on the Domino101 app for Connect / product guides once linked.
