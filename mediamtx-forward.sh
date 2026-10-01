#!/bin/bash
# MediaMTX execs this instead of ffmpeg. A signal that kills ffmpeg stays in
# that child, so this shell can exit with the wait status. Go's ExitCode()
# is -1 only when the process MediaMTX launched is itself signaled.
set -u
log="/home/adowie/frappe-bench/logs/mediamtx-forward.log"
mkdir -p "$(dirname "$log")"
{
	printf '%s start\n' "$(date -Is)"
} >>"$log"
set +e
"$@" 2>&1 | sed -E 's#rtmps?://[^[:space:]]+#rtmps://[redacted]#g' >>"$log"
status=${PIPESTATUS[0]}
{
	printf 'wait status %s\n' "$status"
} >>"$log"
exit "$status"
