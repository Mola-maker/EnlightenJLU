#!/bin/sh
# Stop the MaixCAM2 web console pipeline and send ALL_OFF via the service.
set -u

ROOT=/root/JLU_LEDLAB
PIDFILE="$ROOT/run/web.pid"

if [ -f "$PIDFILE" ]; then
    PID="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        kill -TERM "-$PID" 2>/dev/null || kill -TERM "$PID" 2>/dev/null || true
        i=0
        while [ "$i" -lt 30 ]; do
            kill -0 "$PID" 2>/dev/null || break
            sleep 0.1
            i=$((i + 1))
        done
        kill -KILL "-$PID" 2>/dev/null || kill -KILL "$PID" 2>/dev/null || true
    fi
fi
rm -f "$PIDFILE"
echo "MaixCAM2 web console stopped"
