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
- Open **UDP 8890** on the public host for station pull feeds (SRT). On a dev bench with its own MediaMTX config, add `srt: true`, a free `srtAddress` (for example `:18890`) and the same `pull-` auth rules as `mediamtx.yml`, then open that port.
- Optional: raise `net.core.rmem_max` and `net.core.rmem_default` to at least `1000000`, then run `bench domino-stream-setup` again and restart MediaMTX. `mediamtx.yml` ships with `udpReadBufferSize: 1000000`; setup lowers it to `212992` when the host limit is smaller, because MediaMTX will not start above `rmem_max`.
- `writeQueueSize: 4096` in `mediamtx.yml` lets the forward FFmpeg ride out a few seconds of slow RTMP upload. With the old 512, MediaMTX discarded video mid-GOP ("reader is too slow") and every frame until the next keyframe decoded corrupted.
- For the forward to YouTube on a lossy, long round-trip link, enable BBR: `modprobe tcp_bbr`, then `net.core.default_qdisc=fq` and `net.ipv4.tcp_congestion_control=bbr` in `/etc/sysctl.d/`.
- Cloudflare app id and secret, and the YouTube destination, stay in Stream Settings and Domino Defaults. They are account secrets, not install artifacts.

See `docs/` on the Domino101 app for Connect / product guides once linked.

## Station feeds (extra outputs)

`api/stream_outputs.py` adds up to 4 extra outputs to a source, on top of the YouTube owner forward:

- **Sources:** `program`, a match's Match Program (the owner session's composite with overlays), or `table`, a Domino101 (Platform) table stream (the clean camera).
- **Push:** to an `rtmp://`, `rtmps://` or `srt://` ingest the station gives you.
- **Pull:** the station reads `srt://<host>:8890?streamid=read:pull-<token>&passphrase=<passphrase>`. Each output has its own passphrase, and removing the output revokes it.

**Profiles:**
- `broadcast` (default): 1080p at 25/30/50/60 fps CFR, H.264 High at 8 Mbit/s CBR, AAC 192k 48 kHz stereo, loudness at -23 LUFS (EBU R128) or -24 (ATSC A/85).
- `youtube`: the owner forward's encode.

A `broadcast` output re-encodes on the media node and needs about 2 CPU cores.

**How it works:**
- Each output is an `out-` path on the same node. It's a proxy of the source with a `runOnReady` FFmpeg.
- Outputs live in Redis for 12 hours and follow the source across go-lives. The pull address stays the same.
- Closing the source stops them, and the next go-live restarts them.

**API:** `domino_stream.api.room.add_stream_output` / `remove_stream_output` / `list_stream_outputs`. These need a System Manager or Domino Manager session, or the inbound API key. Presence tokens and publisher grants are refused. Push keys are never returned.

`mediamtx.yml` changes take effect when MediaMTX restarts, which drops live streams on that node, so schedule it.
