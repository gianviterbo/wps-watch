#!/bin/bash
# Keep the WPS Watch origin alive on this box (no systemd available).
# Safe to run from cron: exits 0 when healthy, restarts a dead/stuck process once.
set -u
APP=/data/workspace/wps-watch
LOG=/data/logs/wps-watch
mkdir -p "$LOG"
cd "$APP" || exit 1

if curl -fsS -m 6 http://127.0.0.1:8931/api/health >/dev/null 2>&1; then
  exit 0
fi

# Kill only an exact 'python3 server.py' whose cwd is this app (a substring
# match would also hit the wrapper shell running this very script).
python3 - <<'PY'
import os, signal
me = os.getpid()
for pid in os.listdir('/proc'):
    if not pid.isdigit() or int(pid) == me:
        continue
    try:
        cmd = open(f'/proc/{pid}/cmdline','rb').read().decode('utf8','replace').replace('\x00',' ').strip()
        cwd = os.readlink(f'/proc/{pid}/cwd')
    except Exception:
        continue
    if cmd in ('python3 server.py', '/usr/bin/python3 server.py') and cwd == '/data/workspace/wps-watch':
        try:
            os.kill(int(pid), signal.SIGTERM)
        except Exception:
            pass
PY
sleep 2
setsid nohup python3 server.py >> "$LOG/out.log" 2>&1 < /dev/null &
sleep 8
if curl -fsS -m 6 http://127.0.0.1:8931/api/health >/dev/null 2>&1; then
  echo "$(date -u +%FT%TZ) restarted ok" >> "$LOG/watchdog.log"
else
  echo "$(date -u +%FT%TZ) restart FAILED" >> "$LOG/watchdog.log"
  exit 1
fi
